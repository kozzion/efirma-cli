"""Tests for the efirma CLI and client.

Network is mocked with httpx's MockTransport so these run offline and fast.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from typer.testing import CliRunner

from efirma_cli.cli import app
from efirma_cli.client import (
    DEFAULT_BASE_URL,
    EfirmaClient,
    EfirmaError,
    build_invoice_payload,
    build_line_item,
    company_matches,
    display_number,
    filename_from_headers,
    invoice_total,
    localized,
    unwrap,
)

runner = CliRunner()


def _client_with(handler, **kwargs) -> EfirmaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=DEFAULT_BASE_URL)
    return EfirmaClient(client=http, **kwargs)


def _error_body(status: int, code: str, message: str) -> httpx.Response:
    """The API's standard error envelope."""
    return httpx.Response(
        status,
        json={
            "error": True,
            "url": "https://app.efirma.bg/api/v1/x",
            "statusCode": status,
            "statusMessage": message,
            "message": message,
            "data": {"code": code, "message": message},
        },
    )


# -- localized ------------------------------------------------------------
#
# Shapes below mirror real API responses: every human-readable string is split
# per language, and the EIK is `uic`. There is no plain `name` key anywhere.

COMPANY = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name_bg": "АКМЕ БЪЛГАРИЯ ЕООД",
    "name_en": "ACME BULGARIA LTD",
    "slug": "akme-bulgariya-eood-123456789",
    "uic": "123456789",
    "vat": "BG123456789",
}

ROLE = {
    "id": "22222222-2222-2222-2222-222222222222",
    "name_bg": "Управител",
    "name_en": "Manager",
    "description_bg": "Управителят има пълни права.",
    "description_en": "The manager has full rights.",
}


def test_localized_picks_the_requested_language():
    assert localized(COMPANY, locale="BG") == "АКМЕ БЪЛГАРИЯ ЕООД"
    assert localized(COMPANY, locale="EN") == "ACME BULGARIA LTD"


def test_localized_reads_non_name_fields():
    assert localized(ROLE, "description", "EN") == "The manager has full rights."


def test_localized_falls_back_to_the_other_language():
    assert localized({"name_bg": "Само БГ"}, locale="EN") == "Само БГ"
    assert localized({"name_en": "EN only"}, locale="BG") == "EN only"


def test_localized_returns_empty_when_absent():
    assert localized({}, locale="BG") == ""
    assert localized({"name": "wrong shape"}, locale="BG") == ""


# -- company_matches ------------------------------------------------------


def test_company_matches_by_uuid_slug_uic_and_vat():
    assert company_matches(COMPANY, COMPANY["id"])
    assert company_matches(COMPANY, COMPANY["slug"])
    assert company_matches(COMPANY, "123456789")
    assert company_matches(COMPANY, "BG123456789")


def test_company_matches_name_fragment_in_either_language():
    assert company_matches(COMPANY, "acme")
    assert company_matches(COMPANY, "ACME")
    assert company_matches(COMPANY, "БЪЛГАРИЯ")


def test_company_matches_rejects_unrelated_ref():
    assert company_matches(COMPANY, "acme-ood") is False


def test_company_matches_tolerates_missing_fields():
    assert company_matches({"id": "x"}, "x")
    assert company_matches({}, "anything") is False


# -- error envelope -------------------------------------------------------


def test_error_parses_standard_envelope():
    err = EfirmaError.from_response(
        _error_body(401, "invalid_access_token", "Invalid access token")
    )
    assert err.status_code == 401
    assert err.code == "invalid_access_token"
    assert "Invalid access token" in str(err)


def test_error_parses_zod_validation_issues():
    response = httpx.Response(
        400,
        json={
            "error": True,
            "statusCode": 400,
            "statusMessage": "Validation Error",
            "data": {
                "name": "ZodError",
                "issues": [
                    {"code": "invalid_type", "path": ["email"], "message": "Required"},
                    {
                        "code": "invalid_type",
                        "path": ["password"],
                        "message": "Required",
                    },
                ],
            },
        },
    )
    err = EfirmaError.from_response(response)
    assert err.status_code == 400
    assert "email: Required" in str(err)
    assert "password: Required" in str(err)
    assert len(err.issues) == 2


def test_error_falls_back_to_body_when_not_json():
    # What you get when a path isn't an API route: the Nuxt SPA's HTML.
    err = EfirmaError.from_response(httpx.Response(200, text="<!DOCTYPE html><html>"))
    assert "DOCTYPE" in str(err)


# -- auth ----------------------------------------------------------------


def test_login_stores_tokens_and_sends_bearer(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(
                200, json={"accessToken": "acc-1", "refreshToken": "ref-1"}
            )
        return httpx.Response(200, json={"email": "a@b.c", "companies": []})

    session = tmp_path / "session.json"
    client = _client_with(handler, session_path=session)
    client.login("a@b.c", "pw")

    assert client.access_token == "acc-1"
    assert json.loads(session.read_text())["refreshToken"] == "ref-1"
    # 0600 — these are live credentials.
    assert session.stat().st_mode & 0o777 == 0o600

    client.me()
    assert seen[-1].headers["Authorization"] == "Bearer acc-1"
    assert seen[-1].headers["X-App-Locale"] == "BG"


def test_expired_access_token_refreshes_and_retries_once(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/auth/refresh"):
            return httpx.Response(
                200, json={"accessToken": "acc-2", "refreshToken": "ref-2"}
            )
        # First /me fails with an expired token, the retry succeeds.
        if request.headers.get("Authorization") == "Bearer acc-2":
            return httpx.Response(200, json={"email": "a@b.c"})
        return _error_body(401, "invalid_access_token", "Invalid access token")

    session = tmp_path / "session.json"
    session.write_text(json.dumps({"accessToken": "acc-1", "refreshToken": "ref-1"}))
    client = _client_with(handler, session_path=session)

    assert client.me()["email"] == "a@b.c"
    assert calls == ["/api/v1/me", "/api/v1/auth/refresh", "/api/v1/me"]
    assert client.access_token == "acc-2"


def test_other_401_does_not_trigger_refresh(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _error_body(401, "forbidden_company", "No access to this company")

    session = tmp_path / "session.json"
    session.write_text(json.dumps({"accessToken": "acc-1", "refreshToken": "ref-1"}))
    client = _client_with(handler, session_path=session)

    with pytest.raises(EfirmaError) as excinfo:
        client.me()
    assert excinfo.value.code == "forbidden_company"
    assert calls == ["/api/v1/me"]  # no refresh attempted


def test_refresh_without_token_raises():
    client = _client_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(EfirmaError, match="not logged in"):
        client.refresh()


def test_forget_removes_stored_session(tmp_path):
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"accessToken": "a", "refreshToken": "r"}))
    client = _client_with(lambda r: httpx.Response(200, json={}), session_path=session)

    client.forget()
    assert client.access_token is None
    assert not session.exists()


def test_corrupt_session_file_is_ignored(tmp_path):
    session = tmp_path / "session.json"
    session.write_text("not json{{{")
    client = _client_with(lambda r: httpx.Response(200, json={}), session_path=session)
    assert client.access_token is None


# -- companies & users ----------------------------------------------------


def test_resolve_company_finds_by_uic_from_me():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me")
        return httpx.Response(200, json={"companies": [COMPANY]})

    client = _client_with(handler)
    assert client.resolve_company("123456789")["id"] == COMPANY["id"]


def test_resolve_company_rejects_ambiguous_ref():
    other = {**COMPANY, "id": "other", "uic": "999", "slug": "s2"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"companies": [COMPANY, other]})

    client = _client_with(handler)
    with pytest.raises(EfirmaError, match="matches several companies"):
        client.resolve_company("acme")


def test_resolve_company_raises_when_absent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"companies": []})

    client = _client_with(handler)
    with pytest.raises(EfirmaError, match="no company matching"):
        client.resolve_company("nope")


def test_users_unwraps_a_wrapped_collection():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/companies/{COMPANY['id']}/users"
        return httpx.Response(200, json={"data": [{"id": "u1", "email": "a@b.c"}]})

    client = _client_with(handler)
    users = client.users(COMPANY["id"])
    assert [u["email"] for u in users] == ["a@b.c"]


def test_users_accepts_a_bare_list():
    # This is what the live API actually returns for /users.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "u1",
                    "userId": "u1",
                    "email": "a@b.c",
                    "status": "ACTIVE",
                    "role": ROLE,
                }
            ],
        )

    client = _client_with(handler)
    users = client.users(COMPANY["id"])
    assert len(users) == 1
    assert localized(users[0]["role"], locale="EN") == "Manager"


def test_cancel_invitation_uses_the_invitations_subpath():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(204)

    client = _client_with(handler)
    client.cancel_invitation(COMPANY["id"], "inv-1")
    assert seen == [
        ("DELETE", f"/api/v1/companies/{COMPANY['id']}/users/invitations/inv-1")
    ]


def test_invite_user_passes_resend_as_a_query_flag():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "u1"})

    client = _client_with(handler)
    client.invite_user(COMPANY["id"], {"email": "a@b.c"}, resend=True)
    assert seen[0].url.params["resend"] == "true"
    assert json.loads(seen[0].content) == {"email": "a@b.c"}


def test_204_returns_none():
    client = _client_with(lambda r: httpx.Response(204))
    assert client.remove_user(COMPANY["id"], "u1") is None


# -- collection unwrapping ------------------------------------------------


def test_unwrap_handles_a_bare_list():
    # /users, /units, /accounts return these.
    assert unwrap([{"id": "a"}]) == [{"id": "a"}]


def test_unwrap_handles_the_paginated_envelope():
    # /invoices, /contacts, /items return these.
    envelope = {
        "data": [{"id": "a"}],
        "count": 1,
        "pagination": {"total": 1, "page": 1, "pageSize": 20, "pages": 1},
    }
    assert unwrap(envelope) == [{"id": "a"}]


def test_unwrap_of_nothing_is_empty():
    assert unwrap(None) == []
    assert unwrap({}) == []


# -- pdf filename ---------------------------------------------------------


def test_filename_comes_from_content_disposition():
    headers = httpx.Headers(
        {"content-disposition": 'attachment; filename="Invoice-0000000001.pdf"'}
    )
    assert filename_from_headers(headers, "id-1") == "Invoice-0000000001.pdf"


def test_filename_falls_back_to_the_id():
    assert filename_from_headers(httpx.Headers({}), "id-1") == "id-1.pdf"


# -- invoice numbering ----------------------------------------------------


def test_display_number_pads_to_ten_digits_including_prefix():
    assert display_number("0", 1) == "0000000001"
    assert display_number("", 42) == "0000000042"
    assert len(display_number("0", 1)) == 10


# -- invoice payload ------------------------------------------------------


def _line(**over):
    base = dict(
        name_bg="Консултация",
        name_en="Consulting",
        quantity=10,
        unit_price=120,
        vat_rate=20,
        item_id="i1",
        unit_id="u1",
        account_id="a1",
    )
    base.update(over)
    return build_line_item(**base)


def test_build_line_item_uses_the_servers_field_names():
    line = _line()
    assert set(line) == {
        "name_bg",
        "name_en",
        "quantity",
        "unitPrice",
        "vatRate",
        "itemId",
        "unitId",
        "accountId",
    }


def test_build_invoice_payload_has_every_required_field():
    payload = build_invoice_payload(
        contact_id="c1", line_items=[_line()], number=1, number_prefix="0"
    )
    # Exactly the eighteen keys the server's Zod schema demands.
    assert set(payload) == {
        "contactId",
        "numberPrefix",
        "number",
        "displayNumber",
        "issueDate",
        "taxableEventDate",
        "dueDate",
        "currencyCode",
        "status",
        "locale",
        "isVatRegistered",
        "taxMethod",
        "vatNonChargeReasonId",
        "lineItems",
        "paymentMethod",
        "paymentMethodOther",
        "bankAccountId",
    }


def test_build_invoice_payload_defaults_dates_and_number():
    payload = build_invoice_payload(
        contact_id="c1",
        line_items=[_line()],
        number=7,
        number_prefix="0",
        issue_date=date(2026, 9, 17),
        due_days=14,
    )
    assert payload["issueDate"] == "2026-09-17"
    assert payload["taxableEventDate"] == "2026-09-17"  # defaults to issue date
    assert payload["dueDate"] == "2026-10-01"
    assert payload["displayNumber"] == "0000000007"


def test_build_invoice_payload_keeps_nullable_refs_present():
    # The server requires the keys to exist; null is an acceptable value.
    payload = build_invoice_payload(contact_id="c1", line_items=[_line()], number=1)
    assert payload["vatNonChargeReasonId"] is None
    assert payload["bankAccountId"] is None


def test_build_invoice_payload_rejects_bad_enums_locally():
    for kwargs in (
        {"status": "DRAFT"},
        {"payment_method": "BITCOIN"},
        {"tax_method": "SOMEHOW"},
    ):
        with pytest.raises(EfirmaError):
            build_invoice_payload(
                contact_id="c1", line_items=[_line()], number=1, **kwargs
            )


def test_build_invoice_payload_rejects_an_empty_invoice():
    with pytest.raises(EfirmaError, match="at least one line item"):
        build_invoice_payload(contact_id="c1", line_items=[], number=1)


def test_invoice_total_sums_net_and_gross():
    payload = build_invoice_payload(
        contact_id="c1",
        line_items=[_line(), _line(quantity=1, unit_price=100)],
        number=1,
    )
    net, gross = invoice_total(payload)
    assert net == pytest.approx(1300.0)
    assert gross == pytest.approx(1560.0)


# -- numbering / reference data -------------------------------------------


def test_numberings_sends_the_required_document_type():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=[{"id": "n1", "prefix": "0", "nextNumber": 1, "isActive": True}]
        )

    client = _client_with(handler)
    client.numberings(COMPANY["id"], "PROFORMA_INVOICE")
    assert seen[0].url.params["documentType"] == "PROFORMA_INVOICE"


def test_next_number_picks_the_active_series():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "old", "prefix": "0", "nextNumber": 99, "isActive": False},
                {"id": "cur", "prefix": "0", "nextNumber": 5, "isActive": True},
            ],
        )

    client = _client_with(handler)
    assert client.next_number(COMPANY["id"])["id"] == "cur"


def test_next_number_raises_when_no_series_is_active():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "old", "isActive": False}])

    client = _client_with(handler)
    with pytest.raises(EfirmaError, match="no active"):
        client.next_number(COMPANY["id"])


def test_invoices_returns_the_paginated_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["page"] == "2"
        return httpx.Response(
            200,
            json={"data": [], "count": 0, "pagination": {"page": 2, "pages": 0}},
        )

    client = _client_with(handler)
    assert client.invoices(COMPANY["id"], page=2)["pagination"]["page"] == 2


def test_invoice_pdf_returns_content_and_filename():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4 ...",
            headers={"content-disposition": 'attachment; filename="INV-1.pdf"'},
        )

    client = _client_with(handler)
    content, name = client.invoice_pdf(COMPANY["id"], "inv-1")
    assert content.startswith(b"%PDF")
    assert name == "INV-1.pdf"


# -- users write paths ----------------------------------------------------


def test_update_user_sends_only_the_role():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "u1"})

    client = _client_with(handler)
    client.update_user(COMPANY["id"], "u1", {"roleId": "r1"})
    assert seen[0].method == "PUT"
    assert json.loads(seen[0].content) == {"roleId": "r1"}


# -- CLI smoke ------------------------------------------------------------


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("login", "logout", "whoami", "companies", "users", "invoices"):
        assert command in result.stdout


def test_cli_users_help():
    result = runner.invoke(app, ["users", "--help"])
    assert result.exit_code == 0
    for command in ("list", "invite", "set-role", "remove", "cancel-invite"):
        assert command in result.stdout


def test_cli_invoices_help():
    result = runner.invoke(app, ["invoices", "--help"])
    assert result.exit_code == 0
    for command in ("list", "show", "create", "pdf", "refs"):
        assert command in result.stdout


def test_cli_invoice_create_dry_run_issues_nothing(tmp_path, monkeypatch):
    """--dry-run must not reach the network beyond resolving the company."""
    payload = build_invoice_payload(
        contact_id="c1", line_items=[_line()], number=1, number_prefix="0"
    )
    payload_file = tmp_path / "invoice.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(str(request.url))
        return httpx.Response(200, json={"companies": [COMPANY]})

    session = tmp_path / "session.json"
    session.write_text(json.dumps({"accessToken": "a", "refreshToken": "r"}))
    monkeypatch.setenv("EFIRMA_SESSION_FILE", str(session))
    monkeypatch.setenv("EFIRMA_COMPANY", COMPANY["uic"])
    monkeypatch.setattr(
        "efirma_cli.cli._client",
        lambda locale="BG": _client_with(handler, session_path=session),
    )

    result = runner.invoke(
        app, ["invoices", "create", "--from", str(payload_file), "--dry-run"]
    )
    assert result.exit_code == 0
    assert posted == []  # nothing was issued
