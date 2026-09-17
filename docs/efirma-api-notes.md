# efirma.bg API — reverse-engineering notes

Status of this document: **the transport, auth, error model, response shapes
and write payloads are confirmed against a live account.** The endpoint catalog
is extracted verbatim from the production JS bundle, so paths and HTTP methods
are reliable. The one thing *not* verified end-to-end is issuing an invoice —
its schema is confirmed, its semantics are not (see *Write payloads*).

## Is there a public API?

**No.** Checked, and all negative:

- `efirma.bg` markets no API, developer portal, webhooks or integrations.
- No `openapi.json` / `swagger.json` / `/api/docs` / `/api/docs-json`. Every one
  of those paths returns the Nuxt SPA's HTML shell with `200` — a catch-all, not
  an endpoint. **Always check `content-type` before believing a `200` here.**
- No `api.efirma.bg` host (DNS does not resolve).
- Unrelated: `efirma.com`, `efirma.es`, `api.efirma.es` are Spanish e-signature
  products that *do* publish APIs. Different company — do not follow their docs.

So: we drive the same internal REST API the web app uses. It is clean,
consistently RESTful and pleasant to work against — this is much closer to
"undocumented API" than to "scrape the HTML".

## Architecture

- **Frontend:** Nuxt 3 SPA (`x-powered-by: Nuxt`), client-rendered
  (`data-ssr="false"`), hosted on **Render**, behind **Cloudflare**.
- **Backend:** same origin, mounted at **`/api/v1`**. There is no separate API
  host — `window.__NUXT__.config.public` contains only `EFIRMA_APP_URL`,
  `EFIRMA_WEBSITE_URL`, Stripe and PostHog keys, and no API base.
- **Validation:** **Zod**, server-side. Error envelopes are h3/Nitro-shaped
  (`statusCode` + `statusMessage`), consistent with a Nitro server backend.
- **Analytics:** PostHog, proxied first-party through `/_relay` (so PostHog's own
  `/api/...` strings appear in the bundle — they are *not* eFirma endpoints).
- **Billing:** Stripe (live publishable key + four price ids in the public
  config — standard/pro × monthly/yearly).

The API base is therefore:

```
https://app.efirma.bg/api/v1
```

## Auth (✅ confirmed)

**JWT bearer, not cookie sessions.**

```
POST /api/v1/auth/login   {"email": "...", "password": "..."}
  -> {"accessToken": "...", "refreshToken": "..."}
```

Every subsequent request carries:

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <accessToken>` |
| `X-App-Locale` | `BG` or `EN` (uppercase Nuxt i18n codes, **not** IETF tags) |
| `x-posthog-distinct-id`, `x-posthog-session-id` | analytics only — safe to omit |

The browser keeps the pair in cookies `efirma-app-access-token`
(`maxAge` 86 400 → **access token lives ~24 h**) and `efirma-app-refresh-token`
(`maxAge` 31 536 000 → **refresh token ~1 year**), both `path=/`,
`sameSite=lax`. We store them in `~/.efirma/session.json` (mode `0600`) instead.

### Refresh-and-retry (✅ confirmed)

```
POST /api/v1/auth/refresh  {"refreshToken": "..."}
  -> {"accessToken": "...", "refreshToken": "..."}
```

The app's own rule, which `client.py` reproduces exactly: refresh **only** when
the response is `401` **and** `data.code == "invalid_access_token"`, then retry
the original request **once**. Any other 401 (and a failed refresh) means the
session is genuinely gone — log out rather than looping. The bundle guards this
with an `_isRetry` flag and a single-flight promise so concurrent 401s trigger
one refresh, not N.

### Other auth endpoints (from the bundle)

`POST /auth/register`, `/auth/register-by-invite`, `/auth/confirm-email`,
`/auth/resend-confirmation-link`, `/auth/forgot-password`, `/auth/reset-password`,
`/auth/accept-invite`; `GET /auth/invitation/{token}`.

## Error envelope (✅ confirmed)

Every failure, same shape:

```json
{
  "error": true,
  "url": "https://app.efirma.bg/api/v1/companies",
  "statusCode": 401,
  "statusMessage": "Invalid access token",
  "message": "Invalid access token",
  "data": { "code": "invalid_access_token", "message": "Invalid access token" }
}
```

Validation failures replace `data` with the raw Zod error:

```json
{ "statusCode": 400, "statusMessage": "Validation Error",
  "data": { "name": "ZodError", "issues": [
    { "code": "invalid_type", "expected": "string", "received": "undefined",
      "path": ["email"], "message": "Required" } ] } }
```

### 🔑 The Zod errors are a free schema oracle

`POST` an empty body `{}` to any endpoint and the server replies with the list
of required fields and their types. This is how `/auth/login` and `/auth/refresh`
were confirmed without ever logging in:

```bash
curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
  https://app.efirma.bg/api/v1/auth/login
# -> issues: email (string, Required), password (string, Required)
```

**Use this to map request bodies before writing any code for an endpoint.** It
needs no auth for the `/auth/*` routes; for company routes it needs a session,
but it still beats guessing.

## Endpoint catalog

Extracted from the production bundle (`_nuxt/CcQSOFtv.js`), which contains the
whole API layer as small per-resource modules. Paths are relative to
`https://app.efirma.bg/api/v1`.

Almost every resource follows the same five-method shape, so assume this unless
noted:

```
POST   /companies/{companyId}/<resource>          create
GET    /companies/{companyId}/<resource>          findAll
GET    /companies/{companyId}/<resource>/{id}     findOne
PUT    /companies/{companyId}/<resource>/{id}     update
DELETE /companies/{companyId}/<resource>/{id}     remove
```

### Account & companies

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/me` | profile; **includes `companies[]` (with `id`, `name`, `slug`, `selected`) and `invitations[]`** |
| `PATCH` | `/me` | update profile |
| `PUT` | `/me/credentials` | change password |
| `PUT` | `/me/select-company` | `{companyId}` |
| `PUT` | `/me/select-locale` | `{locale}` |
| `POST` | `/me/leave-company` | `{companyId}` |
| `POST` | `/me/leave-firm` | `{firmId}` |
| `GET` | `/companies` | company list |
| `GET`/`POST`/`PUT`/`DELETE` | `/firms`, `/firms/{id}`, `/firms/{id}/clients` | accounting-office layer above companies |

### Users & roles — the `settings/users` screen

| Method | Path | Bundle name |
| --- | --- | --- |
| `GET` | `/companies/{id}/users` | `findAll` |
| `GET` | `/companies/{id}/users/{userId}` | `findOne` |
| `POST` | `/companies/{id}/users?resend=<bool>` | `invite` |
| `PUT` | `/companies/{id}/users/{userId}` | `update` (role assignment) |
| `DELETE` | `/companies/{id}/users/{userId}` | `remove` |
| `DELETE` | `/companies/{id}/users/invitations/{invitationId}` | `cancelInvite` — **note the different sub-path** |
| `GET`/`POST`/`PUT`/`DELETE` | `/companies/{id}/roles[/{roleId}]` | custom roles, full CRUD |

Roles are **database rows with UUIDs**, not an enum — which is what makes
custom roles possible. A fresh company has two: Управител/Manager (full rights,
including the subscription plan) and Счетоводител/Accountant (limited, mainly
accounting). The `OWNER`/`MEMBER` constants in the bundle are a separate
membership-level concept. Payloads are in *Write payloads* below.

### Documents & accounting

All under `/companies/{companyId}/`, all standard CRUD:

`invoices`, `proforma-invoices`, `credit-notes`, `debit-notes`, `revenues`,
`other-revenues`, `expenses` (+ `expenses/batch`), `recurring-invoices`,
`contacts`, `items`, `units`, `accounts`, `numberings`,
`vat-non-charge-reasons` (`findAll` takes `?isVatRegistered=`), `dashboard`,
`drive`, `exports`, `activity-logs`, `subscriptions`, `ai-scan-packages`,
`settings`.

### Banking

| Path | Notes |
| --- | --- |
| `/companies/{id}/bank-accounts` | CRUD + `POST .../set-default` `{bankAccountId}`, `setActive` |
| `/companies/{id}/bank-transactions` | CRUD |
| `/companies/{id}/bank-transactions/{txId}/category` | categorise |
| `/companies/{id}/bank-transactions/{txId}/contact` + `/contact-suggestion` | AI contact matching |
| `/companies/{id}/bank-transactions/{txId}/documents[/{docId}]` + `/document-suggestions` | AI document matching |

### Settings & misc

| Method | Path | Notes |
| --- | --- | --- |
| `PUT`/`DELETE` | `/companies/{id}/settings/logo` | `multipart/form-data`, field `logo` |
| `GET`/`PUT` | `/companies/{id}/settings/vat-export-accounts` | |
| `GET` | `/check-vat-number?vat=` | VAT validation |
| — | `/nra/efirma-audit-file` | NRA (Bulgarian revenue agency) audit export |
| — | `/companies/{id}/subscription-invoices/{id}/pdf?documentLabel=` | returns a blob; **filename comes from `Content-Disposition`**, not the body |

## Response shapes (✅ confirmed against a live account)

### 🔑 Every human-readable string is bilingual

There is **no plain `name` field anywhere.** Names, descriptions and addresses
are all split per language with `_bg` / `_en` suffixes:

```json
{ "name_bg": "АКМЕ БЪЛГАРИЯ ЕООД",
  "name_en": "ACME BULGARIA LTD",
  "description_bg": "...", "description_en": "...",
  "city_bg": "Банско", "city_en": "Bansko" }
```

This applies to companies, roles, currencies and addresses alike. Use
`localized(obj, field, locale)` in `client.py` rather than reaching for
`obj["name"]` — which silently yields `None` and renders as a blank column.
(That is exactly the bug the first live run produced.)

Note also that `X-App-Locale` does **not** make the server pick a language for
you: it returns both, and the client chooses.

### Company identity fields

| Field | Value | Notes |
| --- | --- | --- |
| `id` | `11111111-…` | UUID — **what the API keys on** |
| `uic` | `123456789` | the EIK. **The field is `uic`, not `eik`** |
| `vat` | `BG123456789` | separate VAT number, `BG`-prefixed |
| `slug` | `akme-bulgariya-eood-123456789` | `<name-slug>-<uic>`, used in web-app routes |

Companies also carry `subscriptionPlan` (`PRO`), `baseCurrencyCode` (`EUR`),
`isVatRegistered`, `featureFlags[]`, Stripe ids, and nested `address`,
`settings`, `baseCurrency`, `firm`, `logo`, `role`.

### `GET /me`

```
{ id, email, firstName, lastName, locale, isEmailConfirmed, lastSeenAt,
  companies: [ …, selected: bool, role: {…} ],
  firms: [], invitations: [] }
```

`companies[].selected` marks the active company, which is what the CLI falls
back to when `--company` is omitted.

### `GET /companies/{id}/users`

Returns a **bare JSON array** — not wrapped in `{data: …}`:

```
[ { id, userId, companyId, firmId, email, firstName, lastName,
    status: "ACTIVE", locale, isEmailConfirmed, lastSeenAt, selected,
    roleId, role: { id, name_bg, name_en, description_bg, description_en } } ]
```

- `id == userId` — so `DELETE /users/{id}` takes the user id, not a separate
  membership id. Confirmed on live data.
- `roleId == role.id`; the role object is embedded, so listing users needs no
  second request to resolve role names.
- `status` is `"ACTIVE"` for members; pending invitations presumably differ —
  not yet observed, this account has none.

### `GET /companies/{id}/roles`

Two built-in roles on a fresh company, each with bilingual name + description:

| `name_en` | `name_bg` | Gist of the description |
| --- | --- | --- |
| Manager | Управител | full rights, including changing the subscription plan |
| Accountant | Счетоводител | limited rights, mainly accounting-related |

Note these are **not** the `OWNER` / `MEMBER` string constants that appear in
the bundle — those are a separate membership-level enum. Roles proper are
database rows with UUIDs, which is what makes custom roles possible.

## Write payloads (✅ confirmed via the Zod oracle)

### `POST /companies/{id}/users` — invite

```json
{ "email": "...", "roleId": "<uuid>", "locale": "BG" | "EN" }
```

Plus `?resend=true|false`. All three fields are required.

### `PUT /companies/{id}/users/{userId}` — update

```json
{ "roleId": "<uuid>" }
```

**Role is the only updatable field.** There is no name/email editing here.

### `PUT /companies/{id}/roles/{roleId}` — custom role

```json
{ "name_bg": "...", "permissions": [ ... ] }
```

The `permissions` array's element shape is not yet mapped.

### `POST /companies/{id}/invoices` — issue an invoice

Eighteen required top-level fields — a flat schema, no nesting except the
lines:

| Field | Type | Notes |
| --- | --- | --- |
| `contactId` | uuid | the customer |
| `numberPrefix` | string | from the numbering series (`"0"`) |
| `number` | **number** | from the series' `nextNumber` |
| `displayNumber` | string | the human-facing number, e.g. `"0000000001"` |
| `issueDate`, `taxableEventDate`, `dueDate` | `YYYY-MM-DD` | plain date strings |
| `currencyCode` | string | e.g. `EUR` |
| `status` | enum | `ISSUED\|SENT\|PARTIALLY_PAID\|PAID\|OVERDUE\|UNCOLLECTIBLE\|VOID` |
| `locale` | enum | `BG\|EN` |
| `isVatRegistered` | boolean | from the company record |
| `taxMethod` | enum | `NO_TAX\|PER_LINE\|PER_TOTAL` |
| `vatNonChargeReasonId` | uuid or **null** | must be *present*; null is fine |
| `lineItems` | array | see below |
| `paymentMethod` | enum | `CASH\|BANK_TRANSFER\|CARD\|CASH_ON_DELIVERY\|SET_OFF\|COMBINED\|POSTAL_MONEY_TRANSFER\|OTHER` |
| `paymentMethodOther` | string | free text when `paymentMethod` is `OTHER` |
| `bankAccountId` | uuid or **null** | must be present; null is fine |

Note the pattern on the nullable ones: Zod reports them as `Required` when
**absent**, but accepts an explicit `null`. Omitting the key is an error;
sending `null` is not.

Each `lineItems[]` element requires **all** of:

```json
{ "name_bg": "...", "name_en": "...", "quantity": 1, "unitPrice": 100,
  "vatRate": 20, "itemId": "<uuid>", "unitId": "<uuid>", "accountId": "<uuid>" }
```

So a line cannot be written free-hand: it must point at a **catalog item**, a
**unit** and a **chart-of-accounts entry**. A fresh company ships with 11 units
and 44 accounts but **zero items and zero contacts**, so both have to exist
before any invoice can be issued.

⚠️ **Not verified end-to-end.** The field list and enums above come from the
server's own validation errors, which is authoritative for *shape*. A complete
payload has never been posted, so the semantics — whether `number` must equal
the series' `nextNumber`, whether the server re-derives `displayNumber`, and
whether issuing advances `nextNumber` automatically — are still unconfirmed.
`display_number()` in `client.py` is likewise **inferred** (ten digits, prefix
included) from the numbering series' shape, not from a real invoice.

## Numbering

```
GET /companies/{id}/numberings?documentType=INVOICE
-> [{ id, documentType, prefix: "0", nextNumber: 1, isActive: true, year: 0 }]
```

`documentType` is **required** — omitting it is a 400, not a default. Permitted
values: `INVOICE`, `PROFORMA_INVOICE`, `CASH_RECEIPT_VOUCHER`,
`CASH_DISBURSEMENT_VOUCHER`, `JOURNAL_VOUCHER`.

## Pagination — two different collection shapes

This is the API's one real inconsistency, and it bites:

| Shape | Endpoints |
| --- | --- |
| **Bare array** `[...]` | `/users`, `/units`, `/accounts`, `/numberings`, `/vat-non-charge-reasons`, `/bank-accounts` |
| **Paginated envelope** | `/invoices`, `/contacts`, `/items` |

The envelope:

```json
{ "data": [...], "count": 0,
  "pagination": {"total": 0, "page": 1, "pageSize": 20, "pages": 0},
  "order": {"order": "desc", "orderBy": "createdAt"},
  "filters": {} }
```

Use `unwrap()` in `client.py` rather than assuming either. Default page size is
20; `?page=` works, and passing `limit=5` did **not** change `pageSize`, so the
page-size parameter is probably `pageSize` — untested.

## Reference data on a fresh company

| Collection | Count | Notes |
| --- | --- | --- |
| Units | 11 | бр./pc., мин/min, час/hr, гр/gr, кг/kg, тон/t, … |
| Accounts | 44 | chart of accounts, with `number`/`subNumber`/`category`/`parentId` |
| VAT non-charge reasons | 30 | codes like `ART113_9_ART99`, `ART113_9_NOT_REGISTERED` |
| Contacts / Items / Bank accounts | 0 | **must be created before invoicing** |


## Slug ↔ id

The web app routes by `/<name-slug>-<uic>/...` (e.g.
`akme-bulgariya-eood-123456789`) but **the API keys everything by the
company UUID**. The frontend resolves the slug client-side against the company
list from `/me` (`slug === route.params.companySlug`). `client.py` does the same
in `resolve_company()`, additionally accepting a bare UIC, the VAT number, or a
name fragment in either language.

## PDF downloads

PDF endpoints return a binary body, and the real filename is in the
`Content-Disposition` header — read the header, don't invent a name from the id.

## Open questions

- [ ] Whether `POST /invoices` requires `number` to match the series'
      `nextNumber`, re-derives `displayNumber` server-side, and advances the
      series automatically. Needs one real invoice to settle.
- [ ] The `permissions[]` element shape for custom roles.
- [ ] The page-size query parameter (`pageSize`? `limit` had no effect).
- [ ] What `status` a pending invitation carries (members are `"ACTIVE"`), and
      whether invitations appear in the `users` list at all or only under
      `/me`'s `invitations[]`. This account has none to observe.
- [ ] How built-in roles (Manager / Accountant) are distinguished from custom
      ones — no `isDefault`/`isSystem` flag is visible on the role objects.
- [ ] Rate limiting / Cloudflare behaviour under scripted use. Nothing has
      challenged plain `httpx` so far.

### Settled

- [x] **Collections come in two shapes** — bare arrays for reference data,
      paginated envelopes for documents. See *Pagination* above. `unwrap()`
      handles both.
- [x] **Invite payload** is `{email, roleId, locale}`; **user update** takes
      `{roleId}` only.
- [x] **`id == userId`** on company users, so `DELETE …/users/{id}` is correct.
- [x] **All names are bilingual `_bg`/`_en`** — see *Response shapes*.

## Reproduction snippets

Confirm the API is live and see the error envelope (no auth needed):

```bash
curl -sS https://app.efirma.bg/api/v1/companies
```

Discover a request schema without logging in:

```bash
curl -sS -X POST -H 'Content-Type: application/json' -d '{}' \
  https://app.efirma.bg/api/v1/auth/refresh
```

Re-extract the endpoint catalog when the app is redeployed (the bundle hashes
change on every deploy, so start from the HTML):

```bash
curl -sS https://app.efirma.bg/ -o shell.html
grep -oE '/_nuxt/[A-Za-z0-9_.-]+\.js' shell.html | sort -u \
  | while read -r p; do curl -sS "https://app.efirma.bg$p"; done \
  | grep -ohE '`/(companies|auth|me|firms)[a-zA-Z0-9._/${}-]*`' | sort -u
```

## Boundary

Reading and routine team administration are fine to automate. This client
deliberately does **not** touch Stripe subscription changes or the NRA audit-file
submission — anything that spends money or files with the revenue agency stays a
deliberate, in-browser action.
