# efirma-cli

An unofficial command-line client for [efirma.bg](https://efirma.bg), the
Bulgarian cloud accounting platform — invoices, expenses, contacts, bank
reconciliation, multi-company and accounting-office features.

efirma.bg publishes no public API, so this drives the same internal REST API the
web app uses: headlessly, over plain HTTP, no browser and no Node. The API turned
out to be clean and consistently RESTful, so this is much closer to "undocumented
API client" than to "scraper".

```console
$ efirma users list
                      Users — ACME BULGARIA LTD
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Name             ┃ Email            ┃ Role      ┃ Status ┃ ID                ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ Ivan Petrov      │ ivan@example.com │ Управител │ ACTIVE │ 3b429251-d183-4d… │
└──────────────────┴──────────────────┴───────────┴────────┴───────────────────┘
```

> **Unofficial and unaffiliated.** This project is not made, endorsed or
> supported by eFirma. It talks to a private API that can change without notice,
> so expect breakage. Use it on accounts you control, and check eFirma's terms of
> service before pointing it at anything. No warranty — see [LICENSE](LICENSE).

## Install

```bash
poetry install
```

Python 3.11+.

## Quick start

```bash
cp .env.example .env    # add your credentials, or let the CLI prompt
poetry run efirma login
poetry run efirma whoami
poetry run efirma users list
```

Sign in once and the session is reused: the JWT pair lands in
`~/.efirma/session.json` (mode `0600`) and is refreshed automatically when the
access token expires.

## Commands

| Command | What it does |
| --- | --- |
| `efirma login` / `logout` | Sign in and store the session token / discard it |
| `efirma whoami` | Show the account and its companies |
| `efirma companies` | List accessible companies |
| `efirma users list` | List a company's members and pending invitations |
| `efirma users show <id>` | Show one member in full |
| `efirma users roles` | List the company's roles |
| `efirma users invite <email> --role <r>` | Invite someone (asks first — sends email) |
| `efirma users set-role <id> --role <r>` | Change a member's role |
| `efirma users remove <id>` | Remove a member (asks first) |
| `efirma users cancel-invite <id>` | Withdraw a pending invitation |
| `efirma invoices list` | List invoices |
| `efirma invoices show <id>` | Show one invoice in full |
| `efirma invoices pdf <id>` | Download an invoice PDF |
| `efirma invoices refs` | Show the contacts/items/units/accounts invoicing needs |
| `efirma invoices create` | Issue an invoice (asks first) |

Add `--json` to most commands for raw JSON instead of a table, and `--locale EN`
to render names in English.

### Picking a company

`--company` / `-c` takes whichever identifier you have to hand — the company
UUID, the URL slug from the web app (`akme-bulgariya-eood-123456789`), a bare
UIC/EIK (`123456789`), the VAT number, or a fragment of the name in either
language (`acme`, `АКМЕ`). Without it the CLI uses `$EFIRMA_COMPANY`, then the
account's currently-selected company.

The same goes for `--role`, `--contact`, `--item`, `--unit` and `--account`:
UUID or name fragment, either language.

### Issuing an invoice

An invoice needs a contact, and **every line needs a catalog item, a unit and a
chart-of-accounts entry**. A fresh company ships with 11 units and 44 accounts
but no items and no contacts, so check what exists first:

```bash
poetry run efirma invoices refs
```

Then build one from flags:

```bash
poetry run efirma invoices create \
  --contact "Acme" --item "Consulting" --unit hr --account "Services" \
  --line "Consulting:10:120:20" --due-days 14 --dry-run
```

or hand it a complete JSON payload:

```bash
poetry run efirma invoices create --from invoice.json --dry-run
```

`--dry-run` builds the payload, prints it with computed net/gross totals, and
issues nothing. Drop it and the command shows the same summary and asks before
creating the document; `--yes` skips the prompt for scripting.

> ⚠️ **Issuing has never been run end-to-end.** The payload schema is confirmed
> against the server's own validation, but no invoice has actually been created
> through this client. The invoice number format in particular is *inferred* —
> check the first one against the web app. Always `--dry-run` first.

## Configuration

Copy `.env.example` to `.env` (git-ignored). Real environment variables take
precedence.

| Variable | Purpose |
| --- | --- |
| `EFIRMA_EMAIL` | Login email (else prompted) |
| `EFIRMA_PASSWORD` | Login password (else prompted, hidden) |
| `EFIRMA_COMPANY` | Default company for commands that take `--company` |
| `EFIRMA_SESSION_FILE` | Override the session path (default `~/.efirma/session.json`) |

Your password is sent straight to efirma and never stored — only the returned
tokens are. Note the refresh token is valid for about a year, so
`~/.efirma/session.json` is a real credential: it's written `0600`, and
`efirma logout` deletes it.

## What we learned reverse-engineering it

The full writeup — architecture, auth model, error envelope, the endpoint
catalog and every confirmed payload — is in
[docs/efirma-api-notes.md](docs/efirma-api-notes.md). The highlights:

**There is no public API.** No developer portal, no OpenAPI/Swagger, no
`api.efirma.bg`. (The API docs at `efirma.com` / `efirma.es` belong to unrelated
Spanish e-signature products — a red herring worth knowing about.)

**The app is a Nuxt 3 SPA and the API is same-origin at `/api/v1`**, backed by
what looks like Nitro with Zod validation. The whole API layer sits in one JS
chunk as small per-resource modules, which is how the endpoint catalog was
recovered.

**🔑 Zod validation errors are a free schema oracle.** The server validates with
Zod and returns the raw `ZodError` issues, so `POST`ing an empty `{}` makes it
list every required field, its type, and the permitted values of every enum:

```bash
curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
  https://app.efirma.bg/api/v1/auth/login
# -> issues: email (string, Required), password (string, Required)
```

Every payload this client sends was mapped that way rather than guessed. If you
extend it, do the same before writing code.

**Auth is JWT bearer, not cookie sessions.** `POST /auth/login` returns
`{accessToken, refreshToken}`. The refresh rule is specific and worth copying
exactly: refresh **only** on `401` *and* `data.code == "invalid_access_token"`,
then retry once. Any other 401 means the session is genuinely gone — don't loop.

**A `200` does not mean the endpoint exists.** The SPA serves its HTML shell for
every unmatched path, so `/openapi.json`, `/api/docs` and friends all return
`200 text/html`. Check `content-type` before believing a response.

**Every human-readable string is bilingual.** There is no plain `name` field
anywhere — it's `name_bg`/`name_en`, `description_bg`/`description_en`,
`city_bg`/`city_en`. Reaching for `obj["name"]` yields `None` and renders as a
blank column, which is exactly the bug the first live run produced. Hence the
`localized()` helper.

**Field names don't always match the domain language.** The Bulgarian EIK is
`uic`; `vat` is the separate `BG`-prefixed number. There is no `eik` key.

**Collections come in two shapes.** Reference data (`/users`, `/units`,
`/accounts`) returns a bare array; documents (`/invoices`, `/contacts`,
`/items`) return a paginated envelope `{data, count, pagination, order,
filters}`. Hence `unwrap()`.

**Some fields are nullable but still required.** On invoice create,
`vatNonChargeReasonId` and `bankAccountId` are reported `Required` when *absent*
but accept an explicit `null`. Omitting the key is an error; sending `null` is
not.

## Status

Verified against a live account: login/refresh/logout, `/me`, company listing
and resolution, the full users surface, and invoice reading. Invoice *issuing*
is built and schema-confirmed but unexercised (see the warning above).

Not built yet — endpoints are catalogued in the notes, payloads unmapped:
contacts and items CRUD, expenses, credit/debit notes, recurring invoices,
banking and bank-transaction matching, exports.

## Development

```bash
poetry run pytest        # offline: network is mocked with httpx.MockTransport
poetry run ruff check .
poetry run ruff format .
```

The test suite never touches the network — `httpx.MockTransport` stands in — so
it runs fast and needs no credentials.

Layout: `efirma_cli/client.py` holds all HTTP and endpoint logic,
`efirma_cli/cli.py` is presentation only. Keep that split so the client stays
unit-testable without spawning a subprocess.

Project conventions are in [AGENTS.md](AGENTS.md); it's a main-only hobby
project, so there's deliberately very little process.

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). If you confirm
one of the open questions in the notes, please update that document in the same
change: it's as much the point of this repo as the code is.

## Scope

Reading and routine team administration are automated. Changing Stripe
subscriptions and submitting the NRA (Bulgarian revenue agency) audit file are
deliberately left to the browser: anything that spends money or files with the
revenue agency should be a conscious, manual act.

## License

MIT — see [LICENSE](LICENSE).
