# AGENTS.md

Guidance for coding agents working in this repository. Humans are welcome to
read it too — it's just the project's conventions written down.

## What this is

`efirma` — an unofficial command-line client for
[efirma.bg](https://efirma.bg), a Bulgarian cloud accounting / invoicing
platform (invoices, expenses, bank reconciliation, multi-company, accounting-
office features). It drives the site's **internal** REST API — **no public API
exists** — over plain HTTP, headlessly: no browser, no Node.

The frontend is a **Nuxt 3 SPA** on Render behind Cloudflare; the API is
**same-origin at `/api/v1`**. Auth is **JWT bearer** (`accessToken` /
`refreshToken`), *not* cookie sessions. The full reverse-engineering writeup —
architecture, auth model, error envelope and the endpoint catalog — is in
[docs/efirma-api-notes.md](docs/efirma-api-notes.md). Keep that doc updated as
endpoints are confirmed.

Working today, verified against a live account: login/refresh/logout, `/me`,
company listing and resolution, the full `settings/users` surface (list, invite,
set-role, remove, cancel-invite, roles) and invoice **reading** (list, show,
pdf, refs). Invoice **issuing** is built and its payload schema is confirmed,
but it has never been run end-to-end — see the warning in the notes. Not yet
built: expenses, contacts/items CRUD, banking.

## API quirks (learned the hard way)

- **A `200` does not mean the endpoint exists.** The Nuxt SPA serves its HTML
  shell for every unmatched path, so `/openapi.json`, `/api/docs`, `/api/v1`
  and friends all return `200 text/html`. **Check `content-type` before
  believing a response.** A real API route returns `application/json`.
- **Zod validation errors are a free schema oracle.** The backend validates with
  Zod server-side and returns the raw `ZodError` issues. `POST` an empty `{}`
  and it tells you every required field and its type. Use this to map a request
  body *before* writing code for it:

  ```bash
  curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
    https://app.efirma.bg/api/v1/auth/login
  # -> issues: email (string, Required), password (string, Required)
  ```

- **Refresh exactly like the app does.** Refresh **only** on `401` *and*
  `data.code == "invalid_access_token"`, then retry **once**. Any other 401, or
  a failed refresh, means the session is gone — don't loop. `client.py` mirrors
  the bundle's `_isRetry` guard.
- **Token lifetimes:** access token ~24 h (cookie `maxAge` 86 400), refresh
  token ~1 year (31 536 000). So a stored session stays usable for a long time —
  which is exactly why `~/.efirma/session.json` is written `0600`.
- **🔑 There is no plain `name` field — every human-readable string is split
  `_bg` / `_en`.** Companies, roles, currencies and addresses all use
  `name_bg`/`name_en`, `description_bg`/`description_en`,
  `city_bg`/`city_en`. Reaching for `obj["name"]` yields `None` and renders as
  a blank column — that's exactly what the first live run did. Use
  `localized(obj, field, locale)` from `client.py`. `X-App-Locale` does *not*
  make the server pick for you; it returns both languages.
- **The EIK field is `uic`, not `eik`.** `vat` is the separate `BG`-prefixed
  VAT number.
- **Slug ≠ id.** The web app routes by `<name-slug>-<uic>` (e.g.
  `akme-bulgariya-eood-123456789`) but the API keys everything by
  company **UUID**. The frontend resolves this client-side from the `/me`
  company list; so does `resolve_company()`, which also accepts a bare UIC, the
  VAT number, or a name fragment in either language.
- **Company users: `id == userId`**, and the `role` object is embedded in the
  listing (so no second request to resolve role names).
- **Two collection shapes.** Reference data (`/users`, `/units`, `/accounts`,
  `/numberings`, `/bank-accounts`) returns a **bare array**; documents
  (`/invoices`, `/contacts`, `/items`) return a **paginated envelope**
  `{data, count, pagination, order, filters}`. Always go through `unwrap()`.
- **Nullable-but-required fields.** On invoice create, `vatNonChargeReasonId`
  and `bankAccountId` are reported `Required` when *absent* but accept an
  explicit `null`. Omitting the key is an error; sending `null` is not.
- **`numberings` needs `?documentType=`** — omitting it is a 400, not a default.
- **Invoice lines can't be free-hand:** each needs `itemId`, `unitId` *and*
  `accountId`. A fresh company has 11 units and 44 accounts but zero items and
  zero contacts, so both must exist before any invoice can be issued.
- **`/me` already carries the company list**, so resolving a company is one
  request, not two. Keep it that way — `resolve_company()` only falls back to
  `GET /companies` if `/me` didn't match.
- **Cancelling an invite is not deleting a user:** it's
  `DELETE /companies/{id}/users/invitations/{invitationId}`, a different
  sub-path from `DELETE .../users/{userId}`.
- **`X-App-Locale` is `BG`/`EN`** — uppercase Nuxt i18n codes, not IETF language
  tags. The API is not `Accept-Language`-driven.
- **PostHog noise:** the bundle is full of `/api/surveys/`, `/api/early_access_features/`
  etc. Those are PostHog's (proxied first-party via `/_relay`), **not** eFirma
  endpoints. Don't catalogue them.
- **PDF endpoints** return a blob whose real filename is in `Content-Disposition`.
  Read the header rather than inventing a name.

## Stack & conventions

- **Language:** Python 3.11+
- **Style:** PEP 8; format with `ruff format`; lint with `ruff check`
- **Type hints:** use them on all public functions and CLI entry points
- **Tests:** `pytest` (network mocked via `httpx.MockTransport`, runs offline).
  Run with `poetry run pytest`.
- **CLI framework:** `typer` + `rich`
- **HTTP client:** `httpx` (sync `httpx.Client`). If Cloudflare ever starts
  challenging the scripted client, the fallback is `curl_cffi` (TLS-fingerprint
  matching) — still pure Python, no browser.
- **Dependency management:** [Poetry](https://python-poetry.org/). Metadata and
  runtime deps live in the PEP 621 `[project]` table of `pyproject.toml`; dev
  tooling (pytest, ruff) is in `[tool.poetry.group.dev.dependencies]`;
  `poetry.lock` pins exact versions and **is committed**. The venv is created
  in-project (`.venv`, via `poetry.toml`). Use `poetry add <pkg>` /
  `poetry add --group dev <pkg>` — don't hand-edit the lock. There is no
  `requirements.txt`.

## Repo layout

```
efirma-cli/
├── efirma_cli/               # Importable package
│   ├── __init__.py
│   ├── client.py             # EfirmaClient: HTTP/JWT/refresh + endpoint methods
│   └── cli.py                # typer CLI (presentation only)
├── tests/
│   └── test_cli.py
├── docs/
│   └── efirma-api-notes.md   # reverse-engineering writeup
├── pyproject.toml            # Poetry project: metadata, deps, console script `efirma`
├── poetry.lock               # pinned dependency versions (committed)
├── poetry.toml               # local Poetry config (in-project .venv)
├── README.md
├── AGENTS.md                 # this file — agent/contributor conventions
├── CLAUDE.md                 # pointer to AGENTS.md
├── CONTRIBUTING.md
└── .gitignore
```

## When adding code

- Put the entry point under `efirma_cli/cli.py` and expose it via the
  `pyproject.toml` console script (`efirma = "efirma_cli.cli:app"`).
- Keep network calls in `client.py`, separate from CLI parsing, so the logic can
  be unit-tested without spawning a subprocess.
- **Probe a new endpoint with the Zod oracle before coding it**, and record what
  you learn in `docs/efirma-api-notes.md` — move the item out of "Open questions"
  and mark the section ✅ confirmed.
- Never commit secrets (tokens, passwords). Read them from environment
  variables and document the names in the README. `~/.efirma/session.json`
  holds live JWTs — keep it `0600` and never log token values.
- Add a corresponding `tests/test_*.py` for every non-trivial change.

## Boundary

Reading and routine team administration are fine to automate. Do **not** build
paths that change Stripe subscriptions or submit the NRA audit file — anything
that spends money or files with the revenue agency stays a deliberate,
in-browser action.

Two commands have real-world side effects and must keep their confirmation
prompts: `users invite` (sends email) and `invoices create` (issues a
sequentially-numbered accounting document). Both support `--yes` for scripting
and `invoices create` has `--dry-run`; never make skipping confirmation the
default, and never issue a test invoice against a live company.

## Workflow — keep it simple

This is a **single-repo, main-only hobby project**. Treat that as a deliberate
choice, not an oversight:

- **Commit straight to `main` and push.** No feature branches, no PRs, no
  release branches, no merge commits to engineer around.
- **Don't open a PR** unless explicitly asked. Outside contributors have to use
  one (they can't push to `main`) — see [CONTRIBUTING.md](CONTRIBUTING.md) — but
  that doesn't apply to work done in this repo directly.
- **No changelog, no version bumps, no release ceremony.** The version in
  `pyproject.toml` moves when there's a reason, not on a schedule.
- **No issue templates, no labels, no project boards.** Open questions live in
  [docs/efirma-api-notes.md](docs/efirma-api-notes.md), not in a tracker.

Don't add process that nobody asked for. If some workflow tooling would
genuinely help, suggest it first rather than committing it.

### Before every push

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest -q
```

CI runs the same three on push, so a red build means one of them was skipped.

### Commits

- Conventional prefixes are nice but not required: `feat:`, `fix:`, `chore:`,
  `docs:`, `test:`.
- One logical change per commit. Explain *why* in the body when the reason isn't
  obvious from the diff — this repo's commit messages carry a lot of the
  reverse-engineering reasoning.

## Owner

`kozzion` (Jaap Oosterbroek).
