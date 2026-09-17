"""``efirma`` command-line interface.

Presentation layer only: argument parsing and rendering. All network logic
lives in :mod:`efirma_cli.client`.
"""

from __future__ import annotations

import getpass
import json as jsonlib
import os
from pathlib import Path

import typer
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table

from efirma_cli.client import (
    INVOICE_STATUSES,
    PAYMENT_METHODS,
    TAX_METHODS,
    EfirmaClient,
    EfirmaError,
    build_invoice_payload,
    build_line_item,
    default_session_path,
    invoice_total,
    localized,
    unwrap,
)

app = typer.Typer(
    help="Unofficial command-line client for the efirma.bg accounting platform.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _bootstrap() -> None:
    """Run before every command: load a local ``.env`` if present.

    Searches the current working directory (and upwards) so the ``.env`` sits
    where the user runs ``efirma``, not next to the installed package. Real
    environment variables take precedence (``override=False``).
    """
    load_dotenv(find_dotenv(usecwd=True), override=False)


users_app = typer.Typer(
    help="Manage company members and invitations.", no_args_is_help=True
)
app.add_typer(users_app, name="users")

invoices_app = typer.Typer(help="Read and issue invoices.", no_args_is_help=True)
app.add_typer(invoices_app, name="invoices")

console = Console()
err_console = Console(stderr=True)

# Shared options ----------------------------------------------------------

LocaleOpt = typer.Option("BG", "--locale", help="API locale: 'BG' or 'EN'.")
CompanyOpt = typer.Option(
    None,
    "--company",
    "-c",
    help="Company UUID, URL slug, UIC/EIK, or name fragment. "
    "Defaults to $EFIRMA_COMPANY, else the selected company.",
)
JsonOpt = typer.Option(False, "--json", help="Emit raw JSON instead of a table.")
LineOpt = typer.Option(
    None,
    "--line",
    "-l",
    help="Line as 'name:qty:unitPrice:vatRate', repeatable. "
    "Needs --item/--unit/--account for the references.",
)


def _client(locale: str = "BG") -> EfirmaClient:
    return EfirmaClient(locale=locale, session_path=default_session_path())


def _fail(message: str) -> None:
    err_console.print(f"[bold red]error:[/] {message}")
    raise typer.Exit(code=1)


def _resolve_company(client: EfirmaClient, ref: str | None) -> dict:
    """Pick the company to act on: explicit ref, env var, or the selected one."""
    ref = ref or os.environ.get("EFIRMA_COMPANY")
    if ref:
        return client.resolve_company(ref)
    companies = client.me().get("companies") or []
    if not companies:
        raise EfirmaError("this account has no companies")
    selected = next((c for c in companies if c.get("selected")), None)
    if selected:
        return selected
    if len(companies) == 1:
        return companies[0]
    names = ", ".join(localized(c) for c in companies)
    raise EfirmaError(f"several companies available, pass --company: {names}")


def _pick(rows: list[dict], ref: str, what: str, locale: str = "BG") -> dict:
    """Find a reference-data row by UUID or by a name fragment in either language."""
    needle = ref.casefold()
    exact = [r for r in rows if str(r.get("id", "")).casefold() == needle]
    if exact:
        return exact[0]
    named = [
        r
        for r in rows
        if needle in str(r.get("name_bg") or "").casefold()
        or needle in str(r.get("name_en") or "").casefold()
    ]
    if len(named) == 1:
        return named[0]
    if not named:
        raise EfirmaError(f"no {what} matching {ref!r}")
    options = ", ".join(localized(r, locale=locale) for r in named[:8])
    raise EfirmaError(f"{ref!r} matches several {what} options: {options}")


def _echo_json(data: object) -> None:
    console.print_json(jsonlib.dumps(data, ensure_ascii=False, default=str))


# -- auth -----------------------------------------------------------------


@app.command()
def login(
    email: str = typer.Option(None, "--email", help="Defaults to $EFIRMA_EMAIL."),
    password: str = typer.Option(
        None, "--password", help="Defaults to $EFIRMA_PASSWORD, else prompts."
    ),
    locale: str = LocaleOpt,
) -> None:
    """Sign in and store the session token."""
    email = email or os.environ.get("EFIRMA_EMAIL") or typer.prompt("Email")
    password = (
        password or os.environ.get("EFIRMA_PASSWORD") or getpass.getpass("Password: ")
    )
    with _client(locale) as client:
        try:
            client.login(email, password)
            profile = client.me()
        except EfirmaError as exc:
            _fail(str(exc))
        name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
        console.print(f"[green]signed in[/] as {name or email}")


@app.command()
def logout() -> None:
    """Discard the stored session token."""
    with _client() as client:
        client.forget()
    console.print("[green]signed out[/] (local token discarded)")


@app.command()
def whoami(as_json: bool = JsonOpt, locale: str = LocaleOpt) -> None:
    """Show the signed-in account and its companies."""
    with _client(locale) as client:
        try:
            profile = client.me()
        except EfirmaError as exc:
            _fail(str(exc))
    if as_json:
        _echo_json(profile)
        return
    name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
    console.print(f"[bold]{name}[/] <{profile.get('email', '')}>")
    companies = profile.get("companies") or []
    if not companies:
        console.print("[dim]no companies[/]")
        return
    table = Table(title="Companies")
    table.add_column("Name")
    table.add_column("UIC")
    table.add_column("Plan")
    table.add_column("ID", style="dim")
    table.add_column("Selected", justify="center")
    for c in companies:
        table.add_row(
            localized(c, locale=locale),
            str(c.get("uic", "") or ""),
            str(c.get("subscriptionPlan", "") or ""),
            str(c.get("id", "")),
            "✓" if c.get("selected") else "",
        )
    console.print(table)


@app.command()
def companies(as_json: bool = JsonOpt, locale: str = LocaleOpt) -> None:
    """List the companies this account can access."""
    with _client(locale) as client:
        try:
            data = client.companies()
        except EfirmaError as exc:
            _fail(str(exc))
    if as_json:
        _echo_json(data)
        return
    table = Table(title="Companies")
    table.add_column("Name")
    table.add_column("UIC")
    table.add_column("ID", style="dim")
    for c in data:
        table.add_row(
            localized(c, locale=locale),
            str(c.get("uic", "") or ""),
            str(c.get("id", "")),
        )
    console.print(table)


# -- users ----------------------------------------------------------------


@users_app.command("list")
def users_list(
    company: str = CompanyOpt, as_json: bool = JsonOpt, locale: str = LocaleOpt
) -> None:
    """List the company's members and pending invitations."""
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            data = client.users(target["id"])
        except EfirmaError as exc:
            _fail(str(exc))
    if as_json:
        _echo_json(data)
        return
    table = Table(title=f"Users — {localized(target, locale=locale) or target['id']}")
    table.add_column("Name")
    table.add_column("Email")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("ID", style="dim")
    for u in data:
        name = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
        role = u.get("role") or {}
        table.add_row(
            name,
            str(u.get("email", "")),
            localized(role, locale=locale),
            str(u.get("status", "") or ""),
            str(u.get("id", "")),
        )
    console.print(table)


@users_app.command("show")
def users_show(
    user_id: str = typer.Argument(..., help="User UUID."),
    company: str = CompanyOpt,
    locale: str = LocaleOpt,
) -> None:
    """Show one member in full."""
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            _echo_json(client.user(target["id"], user_id))
        except EfirmaError as exc:
            _fail(str(exc))


@users_app.command("roles")
def users_roles(
    company: str = CompanyOpt, as_json: bool = JsonOpt, locale: str = LocaleOpt
) -> None:
    """List the roles available in the company."""
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            data = client.roles(target["id"])
        except EfirmaError as exc:
            _fail(str(exc))
    if as_json:
        _echo_json(data)
        return
    table = Table(title=f"Roles — {localized(target, locale=locale) or target['id']}")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("ID", style="dim")
    for r in data:
        table.add_row(
            localized(r, locale=locale),
            localized(r, "description", locale),
            str(r.get("id", "")),
        )
    console.print(table)


@users_app.command("invite")
def users_invite(
    email: str = typer.Argument(..., help="Email address to invite."),
    role: str = typer.Option(..., "--role", "-r", help="Role UUID or name."),
    locale: str = typer.Option(
        "BG", "--user-locale", help="Invitee's locale: BG or EN."
    ),
    resend: bool = typer.Option(False, "--resend", help="Re-send an existing invite."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    company: str = CompanyOpt,
    api_locale: str = LocaleOpt,
) -> None:
    """Invite someone to the company.

    This sends them an email, so it asks for confirmation first.
    """
    with _client(api_locale) as client:
        try:
            target = _resolve_company(client, company)
            chosen = _pick(client.roles(target["id"]), role, "role", api_locale)
            if not yes:
                console.print(
                    f"Invite [bold]{email}[/] to "
                    f"[bold]{localized(target, locale=api_locale)}[/] "
                    f"as [bold]{localized(chosen, locale=api_locale)}[/]"
                )
                if not typer.confirm("Send the invitation?"):
                    console.print("[dim]cancelled[/]")
                    raise typer.Exit()
            client.invite_user(
                target["id"],
                {"email": email, "roleId": chosen["id"], "locale": locale.upper()},
                resend=resend,
            )
        except EfirmaError as exc:
            _fail(str(exc))
    console.print(f"[green]invited[/] {email}")


@users_app.command("set-role")
def users_set_role(
    user_id: str = typer.Argument(..., help="User UUID (from `efirma users list`)."),
    role: str = typer.Option(..., "--role", "-r", help="Role UUID or name."),
    company: str = CompanyOpt,
    api_locale: str = LocaleOpt,
) -> None:
    """Change a member's role.

    Role is the only thing this endpoint updates — the server's schema for
    ``PUT users/{id}`` accepts ``roleId`` and nothing else.
    """
    with _client(api_locale) as client:
        try:
            target = _resolve_company(client, company)
            chosen = _pick(client.roles(target["id"]), role, "role", api_locale)
            client.update_user(target["id"], user_id, {"roleId": chosen["id"]})
        except EfirmaError as exc:
            _fail(str(exc))
    console.print(f"[green]updated[/] role -> {localized(chosen, locale=api_locale)}")


@users_app.command("remove")
def users_remove(
    user_id: str = typer.Argument(..., help="User UUID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    company: str = CompanyOpt,
    api_locale: str = LocaleOpt,
) -> None:
    """Remove a member from the company."""
    with _client(api_locale) as client:
        try:
            target = _resolve_company(client, company)
            if not yes:
                match = next(
                    (u for u in client.users(target["id"]) if u.get("id") == user_id),
                    None,
                )
                who = match.get("email", user_id) if match else user_id
                if not typer.confirm(f"Remove {who} from this company?"):
                    console.print("[dim]cancelled[/]")
                    raise typer.Exit()
            client.remove_user(target["id"], user_id)
        except EfirmaError as exc:
            _fail(str(exc))
    console.print("[green]removed[/]")


@users_app.command("cancel-invite")
def users_cancel_invite(
    invitation_id: str = typer.Argument(..., help="Invitation UUID."),
    company: str = CompanyOpt,
    api_locale: str = LocaleOpt,
) -> None:
    """Withdraw a pending invitation."""
    with _client(api_locale) as client:
        try:
            target = _resolve_company(client, company)
            client.cancel_invitation(target["id"], invitation_id)
        except EfirmaError as exc:
            _fail(str(exc))
    console.print("[green]invitation cancelled[/]")


# -- invoices -------------------------------------------------------------


@invoices_app.command("list")
def invoices_list(
    page: int = typer.Option(1, "--page", help="Page number (1-based)."),
    company: str = CompanyOpt,
    as_json: bool = JsonOpt,
    locale: str = LocaleOpt,
) -> None:
    """List invoices."""
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            envelope = client.invoices(target["id"], page=page)
        except EfirmaError as exc:
            _fail(str(exc))
    if as_json:
        _echo_json(envelope)
        return
    rows = unwrap(envelope)
    meta = envelope.get("pagination") or {}
    if not rows:
        console.print("[dim]no invoices[/]")
        return
    table = Table(
        title=f"Invoices — {localized(target, locale=locale) or target['id']} "
        f"(page {meta.get('page', page)}/{meta.get('pages', '?')}, "
        f"{meta.get('total', len(rows))} total)"
    )
    table.add_column("Number")
    table.add_column("Issued")
    table.add_column("Due")
    table.add_column("Contact")
    table.add_column("Total", justify="right")
    table.add_column("Status")
    table.add_column("ID", style="dim")
    for inv in rows:
        contact = inv.get("contact") or {}
        total = inv.get("totalAmount") or inv.get("total") or ""
        table.add_row(
            str(inv.get("displayNumber", "")),
            str(inv.get("issueDate", ""))[:10],
            str(inv.get("dueDate", ""))[:10],
            localized(contact, locale=locale) or str(contact.get("email", "")),
            f"{total} {inv.get('currencyCode', '')}".strip(),
            str(inv.get("status", "")),
            str(inv.get("id", "")),
        )
    console.print(table)


@invoices_app.command("show")
def invoices_show(
    invoice_id: str = typer.Argument(..., help="Invoice UUID."),
    company: str = CompanyOpt,
    locale: str = LocaleOpt,
) -> None:
    """Show one invoice in full."""
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            _echo_json(client.invoice(target["id"], invoice_id))
        except EfirmaError as exc:
            _fail(str(exc))


@invoices_app.command("pdf")
def invoices_pdf(
    invoice_id: str = typer.Argument(..., help="Invoice UUID."),
    out: str = typer.Option(None, "--out", "-o", help="Output path or directory."),
    company: str = CompanyOpt,
    locale: str = LocaleOpt,
) -> None:
    """Download an invoice as PDF."""
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            content, name = client.invoice_pdf(target["id"], invoice_id)
        except EfirmaError as exc:
            _fail(str(exc))
    destination = Path(out) if out else Path(name)
    if destination.is_dir():
        destination = destination / name
    destination.write_bytes(content)
    console.print(f"[green]saved[/] {destination} ({len(content):,} bytes)")


@invoices_app.command("refs")
def invoices_refs(
    company: str = CompanyOpt,
    locale: str = LocaleOpt,
) -> None:
    """Show the reference data an invoice line has to point at.

    An invoice needs a contact, and every line needs an item, a unit and a
    chart-of-accounts entry. This lists what the company currently has, so you
    can see what's missing before trying to issue one.
    """
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            cid = target["id"]
            groups = [
                ("Contacts", unwrap(client.contacts(cid))),
                ("Items", unwrap(client.items(cid))),
                ("Units", client.units(cid)),
                ("Accounts", client.accounts(cid)),
                ("Bank accounts", client.bank_accounts(cid)),
            ]
            series = client.numberings(cid, "INVOICE")
        except EfirmaError as exc:
            _fail(str(exc))
    table = Table(title=f"Invoice references — {localized(target, locale=locale)}")
    table.add_column("Kind")
    table.add_column("Count", justify="right")
    table.add_column("Examples")
    for name, rows in groups:
        examples = ", ".join(
            localized(r, locale=locale) or str(r.get("id", "")) for r in rows[:3]
        )
        table.add_row(name, str(len(rows)), examples or "[dim]none[/]")
    for n in series:
        table.add_row(
            "Numbering",
            "",
            f"prefix={n.get('prefix')!r} next={n.get('nextNumber')} "
            f"active={n.get('isActive')}",
        )
    console.print(table)


@invoices_app.command("create")
def invoices_create(
    contact: str = typer.Option(None, "--contact", help="Contact UUID or name."),
    line: list[str] = LineOpt,
    item: str = typer.Option(None, "--item", help="Catalog item UUID or name."),
    unit: str = typer.Option(None, "--unit", help="Unit UUID or name (e.g. 'hr')."),
    account: str = typer.Option(None, "--account", help="Account UUID or name."),
    from_file: str = typer.Option(
        None, "--from", help="Read a complete JSON payload from a file instead."
    ),
    due_days: int = typer.Option(14, "--due-days", help="Days until due."),
    status: str = typer.Option(
        "ISSUED", "--status", help=f"One of: {', '.join(INVOICE_STATUSES)}."
    ),
    payment_method: str = typer.Option(
        "BANK_TRANSFER",
        "--payment-method",
        help=f"One of: {', '.join(PAYMENT_METHODS)}.",
    ),
    tax_method: str = typer.Option(
        "PER_LINE", "--tax-method", help=f"One of: {', '.join(TAX_METHODS)}."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the payload and exit without issuing."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    company: str = CompanyOpt,
    locale: str = LocaleOpt,
) -> None:
    """Issue an invoice.

    This creates a real, sequentially-numbered accounting document, so it shows
    the payload and asks before sending. Use --dry-run to build and inspect it
    without issuing anything.

    The number comes from the company's active numbering series; currency and
    VAT registration come from the company record.
    """
    with _client(locale) as client:
        try:
            target = _resolve_company(client, company)
            cid = target["id"]

            if from_file:
                payload = jsonlib.loads(Path(from_file).read_text(encoding="utf-8"))
            else:
                if not contact or not line:
                    _fail("need --contact and at least one --line (or use --from FILE)")
                if not (item and unit and account):
                    _fail(
                        "every line needs references: pass --item, --unit "
                        "and --account (see `efirma invoices refs`)"
                    )
                chosen_contact = _pick(
                    unwrap(client.contacts(cid)), contact, "contact", locale
                )
                chosen_item = _pick(unwrap(client.items(cid)), item, "item", locale)
                chosen_unit = _pick(client.units(cid), unit, "unit", locale)
                chosen_account = _pick(client.accounts(cid), account, "account", locale)
                series = client.next_number(cid, "INVOICE")
                lines = []
                for spec in line:
                    parts = spec.split(":")
                    if len(parts) != 4:
                        _fail(
                            f"bad --line {spec!r}: expected "
                            "'name:qty:unitPrice:vatRate'"
                        )
                    name, qty, price, vat = parts
                    lines.append(
                        build_line_item(
                            name_bg=name,
                            name_en=name,
                            quantity=float(qty),
                            unit_price=float(price),
                            vat_rate=float(vat),
                            item_id=chosen_item["id"],
                            unit_id=chosen_unit["id"],
                            account_id=chosen_account["id"],
                        )
                    )
                payload = build_invoice_payload(
                    contact_id=chosen_contact["id"],
                    line_items=lines,
                    number=series["nextNumber"],
                    number_prefix=series.get("prefix") or "",
                    due_days=due_days,
                    currency_code=target.get("baseCurrencyCode") or "EUR",
                    status=status,
                    locale=locale.upper(),
                    is_vat_registered=bool(target.get("isVatRegistered")),
                    tax_method=tax_method,
                    payment_method=payment_method,
                )

            net, gross = invoice_total(payload)
            console.print(
                f"[bold]{payload['displayNumber']}[/] → "
                f"{payload['issueDate']}, due {payload['dueDate']}, "
                f"{len(payload['lineItems'])} line(s), "
                f"net {net:,.2f} / gross {gross:,.2f} {payload['currencyCode']}"
            )
            if dry_run:
                _echo_json(payload)
                console.print("[dim]dry run — nothing was issued[/]")
                raise typer.Exit()
            if not yes:
                console.print(
                    "[yellow]This issues a real, numbered accounting document.[/]"
                )
                if not typer.confirm("Issue this invoice?"):
                    console.print("[dim]cancelled[/]")
                    raise typer.Exit()
            created = client.create_invoice(cid, payload)
        except EfirmaError as exc:
            _fail(str(exc))
        except (OSError, ValueError) as exc:
            _fail(f"could not read payload: {exc}")
    console.print(f"[green]issued[/] {created.get('displayNumber', '')}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
