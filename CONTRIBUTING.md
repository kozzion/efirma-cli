# Contributing

Thanks for taking a look. This is an unofficial client for a private API, so the
most valuable contributions are usually **confirmed facts about the API**, not
just code.

This is a small, main-only hobby project — the maintainer commits straight to
`main`. Outside contributions come as pull requests simply because that's the
only route in; don't read it as a heavyweight process. Conventions live in
[AGENTS.md](AGENTS.md).

## Ground rules

- **Map payloads, don't guess them.** The server validates with Zod and returns
  the raw errors, so `POST`ing an empty `{}` tells you every required field, its
  type, and every enum's permitted values:

  ```bash
  curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
    https://app.efirma.bg/api/v1/auth/login
  ```

  Probe first, then write the code.

- **Update [docs/efirma-api-notes.md](docs/efirma-api-notes.md) in the same PR.**
  If you confirm something, move it out of "Open questions" and mark the section
  ✅ confirmed. If you discover a new quirk, write it down — that document is as
  much the point of this repo as the code.

- **Never probe destructive endpoints against a live company.** Use a
  non-existent UUID so validation runs but nothing real can be touched, and
  don't create test invoices on a real account — they are sequentially numbered
  accounting documents.

- **Keep confirmations on side-effectful commands.** `users invite` sends email
  and `invoices create` issues a legal document. Both must keep asking by
  default; `--yes` exists for scripting and shouldn't become the default.

## Code

- Python 3.11+, formatted and linted with `ruff`.
- Network and endpoint logic lives in `efirma_cli/client.py`; `efirma_cli/cli.py`
  is presentation only. Keep that split.
- Add tests for anything non-trivial. The suite mocks all network with
  `httpx.MockTransport`, so it must keep running offline with no credentials.
- Prefer pure functions for payload building (see `build_invoice_payload`) so
  they can be tested without a server.

Before opening a PR:

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest -q
```

## Reporting API changes

If eFirma ships a change that breaks something, an issue with the actual request
and the actual error envelope is far more useful than a description. Redact your
tokens, company UUIDs and any personal data first.
