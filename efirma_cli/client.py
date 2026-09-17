"""HTTP client for efirma.bg's internal JSON API.

efirma.bg publishes no public API. This talks to the same REST endpoints the
Nuxt 3 single-page app calls from the browser (see
``docs/efirma-api-notes.md``). Everything is same-origin under
``https://app.efirma.bg/api/v1`` — there is no separate API host.

Auth is **JWT bearer**, not cookies-as-session: ``POST /auth/login`` returns
``{accessToken, refreshToken}``, the access token goes out as
``Authorization: Bearer <token>``, and a 401 carrying
``data.code == "invalid_access_token"`` is the signal to call
``POST /auth/refresh`` and retry once. The browser stores the pair in the
``efirma-app-access-token`` / ``efirma-app-refresh-token`` cookies; we store
them in a JSON file instead.

This module is deliberately free of any CLI / presentation concerns so the
logic can be unit-tested without spawning a subprocess.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

DEFAULT_BASE_URL = "https://app.efirma.bg/api/v1"

# The app sends the UI language on every request. The backend accepts "BG" and
# "EN" (uppercase — the Nuxt i18n locale codes, not IETF tags).
LOCALE_HEADER = "X-App-Locale"

# The error code the API returns in ``data.code`` when the access token has
# expired. It is the *only* 401 that should trigger a refresh-and-retry; other
# 401s (bad refresh token, revoked session) mean the login is genuinely gone.
INVALID_ACCESS_TOKEN = "invalid_access_token"


def default_session_path() -> Path:
    """Where to persist the JWT pair between CLI invocations.

    Override with the ``EFIRMA_SESSION_FILE`` environment variable.
    """
    override = os.environ.get("EFIRMA_SESSION_FILE")
    if override:
        return Path(override)
    return Path.home() / ".efirma" / "session.json"


class EfirmaError(RuntimeError):
    """Raised when the efirma API returns an error or unexpected response.

    The API wraps every failure in a consistent envelope::

        {"error": true, "url": ..., "statusCode": 401,
         "statusMessage": "Invalid access token",
         "message": "Invalid access token",
         "data": {"code": "invalid_access_token", "message": ...}}

    Validation failures (Zod, on the server) put the field errors in
    ``data.issues`` instead, with ``data.name == "ZodError"``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.issues = issues or []

    @classmethod
    def from_response(cls, response: httpx.Response) -> EfirmaError:
        """Build an error from the API's standard error envelope.

        Falls back to the raw body when the response isn't the expected JSON
        (e.g. a Cloudflare error page, or the Nuxt SPA's HTML catch-all — the
        latter is what you get when a path isn't an API route at all).
        """
        try:
            body = response.json()
        except ValueError:
            snippet = response.text[:200].replace("\n", " ")
            return cls(
                f"HTTP {response.status_code}: {snippet}",
                status_code=response.status_code,
            )

        data = body.get("data") or {}
        issues = data.get("issues") if isinstance(data, dict) else None
        if issues:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in i.get('path', []))}: {i.get('message')}"
                for i in issues
            )
            message = f"validation failed: {detail}"
        else:
            message = (
                body.get("message") or body.get("statusMessage") or response.text[:200]
            )
        return cls(
            message,
            status_code=response.status_code,
            code=data.get("code") if isinstance(data, dict) else None,
            issues=issues,
        )


def localized(obj: dict[str, Any], field: str = "name", locale: str = "BG") -> str:
    """Read a bilingual field off an API object.

    **Every human-readable string in this API is split per language** —
    ``name_bg`` / ``name_en``, ``description_bg`` / ``description_en``,
    ``city_bg`` / ``city_en`` and so on. There is no plain ``name`` key
    anywhere, on companies, roles or addresses. Falls back to the other
    language when the requested one is blank, so something always renders.
    """
    suffixes = ("en", "bg") if locale.upper() == "EN" else ("bg", "en")
    for suffix in suffixes:
        value = obj.get(f"{field}_{suffix}")
        if value:
            return str(value)
    return ""


def company_matches(company: dict[str, Any], ref: str) -> bool:
    """Whether ``ref`` identifies this company.

    The web app routes by a ``<name-slug>-<uic>`` path segment (e.g.
    ``akme-bulgariya-eood-123456789``) while the API keys everything by
    a UUID ``id``. Accept any of them — plus a bare UIC/EIK or a case-insensitive
    substring of either language's name — so the CLI takes whatever the user has
    to hand.

    Note the company id fields: the EIK is ``uic`` (Unified Identification Code)
    and there is no ``eik`` key; ``vat`` is the separate ``BG``-prefixed number.
    """
    needle = ref.casefold()
    exact = [
        str(company.get("id") or ""),
        str(company.get("slug") or ""),
        str(company.get("uic") or ""),
        str(company.get("vat") or ""),
    ]
    if any(needle == c.casefold() for c in exact if c):
        return True
    names = (company.get("name_bg") or "", company.get("name_en") or "")
    return any(needle in str(n).casefold() for n in names if n)


# -- server-side enums ----------------------------------------------------
#
# Read straight out of the Zod validation errors (POST an empty body and the
# server lists the permitted values), so these are exact, not guesses.

INVOICE_STATUSES = (
    "ISSUED",
    "SENT",
    "PARTIALLY_PAID",
    "PAID",
    "OVERDUE",
    "UNCOLLECTIBLE",
    "VOID",
)
PAYMENT_METHODS = (
    "CASH",
    "BANK_TRANSFER",
    "CARD",
    "CASH_ON_DELIVERY",
    "SET_OFF",
    "COMBINED",
    "POSTAL_MONEY_TRANSFER",
    "OTHER",
)
TAX_METHODS = ("NO_TAX", "PER_LINE", "PER_TOTAL")
DOCUMENT_TYPES = (
    "INVOICE",
    "PROFORMA_INVOICE",
    "CASH_RECEIPT_VOUCHER",
    "CASH_DISBURSEMENT_VOUCHER",
    "JOURNAL_VOUCHER",
)

# Bulgarian invoice numbers are ten digits wide, prefix included.
DISPLAY_NUMBER_WIDTH = 10


def unwrap(data: Any) -> list[dict[str, Any]]:
    """Return the rows from a collection response.

    The API is inconsistent about this: ``/users``, ``/units`` and ``/accounts``
    return a **bare array**, while document collections (``/invoices``,
    ``/contacts``, ``/items``) return a paginated envelope
    ``{data, count, pagination, order, filters}``. Accept either.
    """
    if isinstance(data, dict):
        for key in ("data", "items", "results"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
        return []
    return data or []


def filename_from_headers(headers: Any, fallback_id: str) -> str:
    """Pull the filename out of a ``Content-Disposition`` header."""
    disposition = headers.get("content-disposition") or ""
    for part in disposition.split(";"):
        part = part.strip()
        for prefix in ("filename*=UTF-8''", "filename="):
            if part.startswith(prefix):
                return unquote(part[len(prefix) :].strip('"')) or f"{fallback_id}.pdf"
    return f"{fallback_id}.pdf"


def display_number(prefix: str, number: int) -> str:
    """Render an invoice's human-facing number.

    Bulgarian invoice numbers are a fixed ten digits, so the sequence number is
    zero-padded to fill whatever the numbering series' ``prefix`` leaves.

    >>> display_number("0", 1)
    '0000000001'
    >>> display_number("", 42)
    '0000000042'

    **Inferred, not confirmed** — derived from the numbering series' shape
    (``prefix="0"``, ``nextNumber=1``) and the ten-digit convention, not from a
    real issued invoice. Check it against the first invoice this issues, and
    override with an explicit ``display_number`` if it turns out different.
    """
    return f"{prefix}{str(number).zfill(max(DISPLAY_NUMBER_WIDTH - len(prefix), 1))}"


def build_line_item(
    *,
    name_bg: str,
    name_en: str,
    quantity: float,
    unit_price: float,
    vat_rate: float,
    item_id: str,
    unit_id: str,
    account_id: str,
) -> dict[str, Any]:
    """One invoice line, in the exact shape the server's Zod schema requires.

    Every one of these is mandatory — including the three references
    (``itemId``, ``unitId``, ``accountId``), so a line can't be written
    free-hand without a catalog item, a unit and a chart-of-accounts entry.
    """
    return {
        "name_bg": name_bg,
        "name_en": name_en,
        "quantity": quantity,
        "unitPrice": unit_price,
        "vatRate": vat_rate,
        "itemId": item_id,
        "unitId": unit_id,
        "accountId": account_id,
    }


def build_invoice_payload(
    *,
    contact_id: str,
    line_items: list[dict[str, Any]],
    number: int,
    number_prefix: str = "",
    display: str | None = None,
    issue_date: date | None = None,
    taxable_event_date: date | None = None,
    due_date: date | None = None,
    due_days: int = 14,
    currency_code: str = "EUR",
    status: str = "ISSUED",
    locale: str = "BG",
    is_vat_registered: bool = True,
    tax_method: str = "PER_LINE",
    vat_non_charge_reason_id: str | None = None,
    payment_method: str = "BANK_TRANSFER",
    payment_method_other: str = "",
    bank_account_id: str | None = None,
) -> dict[str, Any]:
    """Assemble a complete invoice payload.

    Pure function — no network — so the payload can be built, shown to the user
    and unit-tested before anything is issued. Dates default to today, with
    ``due_date`` at ``due_days`` out. Validates the enum arguments locally so a
    typo fails here rather than as a server round-trip.

    The server requires all eighteen fields; the optional-looking ones
    (``vatNonChargeReasonId``, ``bankAccountId``) must be *present* but accept
    ``null``.
    """
    for value, allowed, label in (
        (status, INVOICE_STATUSES, "status"),
        (payment_method, PAYMENT_METHODS, "payment method"),
        (tax_method, TAX_METHODS, "tax method"),
    ):
        if value not in allowed:
            raise EfirmaError(
                f"unknown {label} {value!r}; choose from {', '.join(allowed)}"
            )
    if not line_items:
        raise EfirmaError("an invoice needs at least one line item")

    issued = issue_date or date.today()
    due = due_date or (issued + timedelta(days=due_days))
    return {
        "contactId": contact_id,
        "numberPrefix": number_prefix,
        "number": number,
        "displayNumber": display or display_number(number_prefix, number),
        "issueDate": issued.isoformat(),
        "taxableEventDate": (taxable_event_date or issued).isoformat(),
        "dueDate": due.isoformat(),
        "currencyCode": currency_code,
        "status": status,
        "locale": locale,
        "isVatRegistered": is_vat_registered,
        "taxMethod": tax_method,
        "vatNonChargeReasonId": vat_non_charge_reason_id,
        "lineItems": line_items,
        "paymentMethod": payment_method,
        "paymentMethodOther": payment_method_other,
        "bankAccountId": bank_account_id,
    }


def invoice_total(payload: dict[str, Any]) -> tuple[float, float]:
    """``(net, gross)`` for a payload's lines, for showing before issuing."""
    net = sum(li["quantity"] * li["unitPrice"] for li in payload["lineItems"])
    gross = sum(
        li["quantity"] * li["unitPrice"] * (1 + li.get("vatRate", 0) / 100)
        for li in payload["lineItems"]
    )
    return net, gross


class EfirmaClient:
    """Thin synchronous client over efirma.bg's JSON API.

    Usage::

        with EfirmaClient(session_path=default_session_path()) as client:
            company = client.resolve_company("123456789")
            for user in client.users(company["id"]):
                print(user["email"])
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        locale: str = "BG",
        timeout: float = 30.0,
        session_path: Path | str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.locale = locale
        self._session_path = Path(session_path) if session_path else None
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
        )
        if self._session_path:
            self._load_tokens()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> EfirmaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- token persistence -------------------------------------------------

    def _load_tokens(self) -> None:
        path = self._session_path
        if not path or not path.exists():
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # corrupt/unreadable store — start fresh rather than crash
        self.access_token = saved.get("accessToken")
        self.refresh_token = saved.get("refreshToken")

    def _save_tokens(self) -> None:
        """Persist the JWT pair, readable only by the current user.

        These are live credentials, so the file is created 0600 rather than
        whatever the umask happens to allow.
        """
        path = self._session_path
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)

    def forget(self) -> None:
        """Drop the stored tokens (local sign-out)."""
        self.access_token = None
        self.refresh_token = None
        if self._session_path and self._session_path.exists():
            self._session_path.unlink()

    # -- low-level ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            LOCALE_HEADER: self.locale,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _request(
        self, method: str, path: str, *, _retry: bool = True, **kwargs: Any
    ) -> Any:
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:  # network/transport-level failure
            raise EfirmaError(f"request to {path} failed: {exc}") from exc

        if response.status_code >= 400:
            error = EfirmaError.from_response(response)
            # An expired access token is recoverable: refresh once, then retry
            # the original call. Any other 401 means the session is really gone.
            if (
                _retry
                and response.status_code == 401
                and error.code == INVALID_ACCESS_TOKEN
                and self.refresh_token
            ):
                self.refresh()
                return self._request(method, path, _retry=False, **kwargs)
            raise error

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise EfirmaError(
                f"{method} {path} did not return JSON "
                f"(content-type={response.headers.get('content-type')!r})"
            ) from exc

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params or None)

    def post(self, path: str, payload: Any | None = None, **params: Any) -> Any:
        return self._request("POST", path, json=payload, params=params or None)

    def put(self, path: str, payload: Any | None = None) -> Any:
        return self._request("PUT", path, json=payload)

    def patch(self, path: str, payload: Any | None = None) -> Any:
        return self._request("PATCH", path, json=payload)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # -- auth --------------------------------------------------------------

    def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate and store the resulting JWT pair.

        The password is sent straight to efirma and never stored by this
        client; only the returned tokens are persisted.
        """
        tokens = self.post("/auth/login", {"email": email, "password": password})
        return self._store_tokens(tokens)

    def refresh(self) -> dict[str, Any]:
        """Exchange the refresh token for a fresh pair.

        Called automatically by :meth:`_request` when the access token expires,
        so callers rarely need this directly.
        """
        if not self.refresh_token:
            raise EfirmaError("not logged in: no refresh token stored")
        # Bypass _request's retry path — a failure here is terminal.
        tokens = self._request(
            "POST",
            "/auth/refresh",
            _retry=False,
            json={"refreshToken": self.refresh_token},
        )
        return self._store_tokens(tokens)

    def _store_tokens(self, tokens: Any) -> dict[str, Any]:
        if not isinstance(tokens, dict) or not tokens.get("accessToken"):
            raise EfirmaError(f"unexpected auth response: {tokens!r}")
        self.access_token = tokens["accessToken"]
        self.refresh_token = tokens.get("refreshToken") or self.refresh_token
        self._save_tokens()
        return tokens

    def is_authenticated(self) -> bool:
        """Whether a stored token still gets us a profile."""
        if not (self.access_token or self.refresh_token):
            return False
        try:
            self.me()
        except EfirmaError:
            return False
        return True

    # -- profile & companies ----------------------------------------------

    def me(self) -> dict[str, Any]:
        """Return the signed-in user's profile.

        Includes ``companies`` (each with ``id``, ``name``, ``slug``, and a
        ``selected`` flag) and ``invitations``, which is how the web app builds
        its company switcher without a second request.
        """
        return self.get("/me")

    def companies(self) -> list[dict[str, Any]]:
        """Return the companies the account can access."""
        return unwrap(self.get("/companies"))

    def resolve_company(self, ref: str) -> dict[str, Any]:
        """Find a company by UUID, URL slug, EIK, or name fragment.

        Looks in ``/me`` first (it already carries the company list, so this is
        one request instead of two) and falls back to ``/companies``.
        """

        def from_me() -> list[dict[str, Any]]:
            try:
                return self.me().get("companies") or []
            except EfirmaError:
                return []

        # Lazily, so /companies is only fetched when /me didn't already answer.
        for pool in (from_me, self.companies):
            matches = [c for c in pool() if company_matches(c, ref)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                names = ", ".join(str(m.get("name")) for m in matches)
                raise EfirmaError(f"{ref!r} matches several companies: {names}")
        raise EfirmaError(f"no company matching {ref!r}")

    # -- users (settings/users) -------------------------------------------
    #
    # Mirrors the web app's user-management screen at
    # /<company-slug>/settings/users.

    def users(self, company_id: str) -> list[dict[str, Any]]:
        """List the company's members and pending invitations."""
        return unwrap(self.get(f"/companies/{company_id}/users"))

    def user(self, company_id: str, user_id: str) -> dict[str, Any]:
        """Return a single company member."""
        return self.get(f"/companies/{company_id}/users/{user_id}")

    def invite_user(
        self, company_id: str, payload: dict[str, Any], *, resend: bool = False
    ) -> dict[str, Any]:
        """Invite someone to the company.

        ``payload`` is passed through to the API; ``resend=True`` re-sends an
        existing invitation instead of creating a new one. The exact required
        fields are enforced server-side by Zod — an incomplete payload comes
        back as an :class:`EfirmaError` naming the missing ones.
        """
        return self.post(
            f"/companies/{company_id}/users", payload, resend=str(resend).lower()
        )

    def update_user(
        self, company_id: str, user_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a member (role assignment lives here)."""
        return self.put(f"/companies/{company_id}/users/{user_id}", payload)

    def remove_user(self, company_id: str, user_id: str) -> Any:
        """Remove a member from the company."""
        return self.delete(f"/companies/{company_id}/users/{user_id}")

    def cancel_invitation(self, company_id: str, invitation_id: str) -> Any:
        """Withdraw a pending invitation.

        Note the different sub-path: invitations are cancelled at
        ``users/invitations/<id>``, not ``users/<id>``.
        """
        return self.delete(f"/companies/{company_id}/users/invitations/{invitation_id}")

    # -- roles -------------------------------------------------------------

    def roles(self, company_id: str) -> list[dict[str, Any]]:
        """List the company's roles (built-in and custom)."""
        return unwrap(self.get(f"/companies/{company_id}/roles"))

    # -- reference data ----------------------------------------------------
    #
    # The lookup tables an invoice line has to point at. All are per-company.

    def units(self, company_id: str) -> list[dict[str, Any]]:
        """Units of measure (бр./pc., час/hr, кг/kg, ...)."""
        return unwrap(self.get(f"/companies/{company_id}/units"))

    def accounts(self, company_id: str) -> list[dict[str, Any]]:
        """Chart of accounts. Each line item must reference one."""
        return unwrap(self.get(f"/companies/{company_id}/accounts"))

    def bank_accounts(self, company_id: str) -> list[dict[str, Any]]:
        return unwrap(self.get(f"/companies/{company_id}/bank-accounts"))

    def numberings(
        self, company_id: str, document_type: str = "INVOICE"
    ) -> list[dict[str, Any]]:
        """Document numbering series.

        ``document_type`` is **required** by the server — omitting it is a 400,
        not a default. Each row carries ``prefix`` and ``nextNumber``, which is
        where an invoice's number comes from.
        """
        return unwrap(
            self.get(f"/companies/{company_id}/numberings", documentType=document_type)
        )

    def next_number(self, company_id: str, document_type: str = "INVOICE") -> dict:
        """The active numbering series for ``document_type``."""
        series = [
            n for n in self.numberings(company_id, document_type) if n.get("isActive")
        ]
        if not series:
            raise EfirmaError(f"no active {document_type} numbering series")
        return series[0]

    def vat_non_charge_reasons(
        self, company_id: str, *, is_vat_registered: bool = True
    ) -> list[dict[str, Any]]:
        """Legal grounds for not charging VAT (codes like ``ART113_9_ART99``)."""
        return unwrap(
            self.get(
                f"/companies/{company_id}/vat-non-charge-reasons",
                isVatRegistered=str(is_vat_registered).lower(),
            )
        )

    def contacts(self, company_id: str, **params: Any) -> dict[str, Any]:
        """One page of contacts (customers/suppliers)."""
        return self.get(f"/companies/{company_id}/contacts", **params)

    def items(self, company_id: str, **params: Any) -> dict[str, Any]:
        """One page of catalog items."""
        return self.get(f"/companies/{company_id}/items", **params)

    # -- invoices ----------------------------------------------------------

    def invoices(self, company_id: str, **params: Any) -> dict[str, Any]:
        """One page of invoices.

        Returns the paginated envelope: ``{data, count, pagination, order,
        filters}``. Unlike ``/users``, document collections are **wrapped**.
        """
        return self.get(f"/companies/{company_id}/invoices", **params)

    def invoice(self, company_id: str, invoice_id: str) -> dict[str, Any]:
        return self.get(f"/companies/{company_id}/invoices/{invoice_id}")

    def create_invoice(
        self, company_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue an invoice.

        This creates a real, sequentially-numbered accounting document. Build
        the payload with :func:`build_invoice_payload` and show it to the user
        before calling this.
        """
        return self.post(f"/companies/{company_id}/invoices", payload)

    def invoice_pdf(self, company_id: str, invoice_id: str) -> tuple[bytes, str]:
        """Download an invoice PDF as ``(content, filename)``.

        The real filename is in ``Content-Disposition`` — read it rather than
        inventing one from the id.
        """
        path = f"/companies/{company_id}/invoices/{invoice_id}/pdf"
        try:
            response = self._client.get(path, headers=self._headers())
        except httpx.HTTPError as exc:
            raise EfirmaError(f"request to {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise EfirmaError.from_response(response)
        return response.content, filename_from_headers(response.headers, invoice_id)
