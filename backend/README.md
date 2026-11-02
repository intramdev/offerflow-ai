# OfferFlow AI backend — operator runbook

Django + DRF API, pipeline (parse → interpret → validate → review →
approve → export), pricing rules, date-based FX, monthly AI credits, and
the Celery worker. Simulated lane by default: zero model traffic unless
the paid path is explicitly activated.

## Prerequisites

- Docker Desktop running on your Mac
- This repository checked out (commands below run from the repository root)

## First start

Copy the example environment and fill in every secret:

```sh
cp .env.example .env
```

Generate values with:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))"
```

Use the first form for `OFFERFLOW_SECRET_KEY` and the second (URL-safe) form
for both passwords in `.env`. Never commit `.env`.

Build and start:

```sh
docker compose up -d --build
sleep 10
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
```

Check health:

```sh
curl http://127.0.0.1:8384/api/health/
```

With the default `fixture` provider expect `"status": "ok"` and
`"mode": "simulated"`. With `OFFERFLOW_PROVIDER=meta` the org-less probe
reports degraded by design — paid dispatch requires an organization, so
verify with a real upload instead. Health also reports
`"runtime_role_privileged"`: `true` means the runtime database role
bypasses row policy (single-role mode — run the isolation switch
below); `false` means restricted; `null` means not applicable.

Run the full test suite:

```sh
docker compose exec backend python manage.py test
```

Row-policy tests require PostgreSQL and skip automatically on other
engines; under Compose they run against the real database. Local runs use
`OFFERFLOW_DB_ENGINE=sqlite`. The restricted-role isolation module
(`organizations.tests.test_isolation_pg`, 22 tests) only runs against a
disposable server named by `OFFERFLOW_PG_ISOLATION_DSN` — never point it
at the application database. The module accepts loopback DSNs only;
the disposable server below shares the backend container's network
namespace, so `127.0.0.1` inside the container reaches it and nothing
else can:

```sh
docker run -d --name of-isolation-disposable \
  --network container:offerflow-backend-1 \
  -e POSTGRES_PASSWORD=disposable-only postgres:17.11
sleep 8
docker compose exec -T \
  -e OFFERFLOW_PG_ISOLATION_DSN=postgres://postgres:disposable-only@127.0.0.1:5432/postgres \
  backend python manage.py test organizations.tests.test_isolation_pg
docker rm -f of-isolation-disposable
```

Passing here proves the policies and bootstrap work under a
restricted role; it does not prove the live stack switched — check
`runtime_role_privileged` on `/api/health/` for that.

Only the backend API port is published, on loopback only (host 8384 →
container 8000). PostgreSQL and RabbitMQ stay on the internal container
network with no published ports.

## Pipeline endpoints

- `POST api/pipeline/uploads/` — multipart `organization_id` + `file`
  (`.xlsx`, `.xls`, `.csv`, `.txt`, `.tsv`, `.docx`; 10 MB / 2000 rows;
  `.doc` refused with guidance)
- `POST api/pipeline/previews/` + `GET api/pipeline/previews/<key>/render/`
  — preview-first import with live mapping
- `POST api/pipeline/previews/from-text/` — paste-text import. Each
  non-empty line becomes one candidate record with stable 1-based
  line numbers; nothing is pre-extracted. Returns the same mapping
  preview plus a `draft` block (revision, confirmed rules, records,
  unresolved issues, sections, history).
- `POST api/pipeline/previews/<key>/assist/` + `.../assist/apply/` —
  preview assistant: propose typed interpretation operations, then
  apply them only after the user clicks **Apply to preview** (user
  edits always win). Apply takes the draft `revision` and fails 409
  when stale; unknown operation fields fail the turn outright.
- `POST api/pipeline/previews/<key>/confirm/` — import the preview at
  the given draft revision (stale revisions fail 409)
- `GET api/pipeline/files/` + `PUT api/pipeline/files/<id>/` — file grid
  (search, rename, per-file currency date)
- `GET api/pipeline/runs/<id>/` — run status with validations and progress
- `GET api/pipeline/runs/<id>/offers/` — normalized offers with provenance
- `GET api/pipeline/runs/<id>/reviews/` — open review requests (`?status=all`)
- `POST api/pipeline/reviews/<id>/answer/` — `{"choice"}` or `{"value"}`
- `POST api/pipeline/reviews/<id>/ignore/` — drop the record from the quote
- `POST api/pipeline/runs/<id>/remap/` — replace mapping, reprocess async
- `POST api/pipeline/runs/<id>/approve/` — bind approval to exact versions
- `POST api/pipeline/runs/<id>/export/` — `{"format": "xlsx"|"csv"}`
- `GET api/pipeline/runs/<id>/quote/` + `.../quote/download/<fmt>/` —
  customer quote with recorded FX
- `GET api/pipeline/artifacts/<id>/download/` — exact stored bytes
- `POST api/pipeline/runs/<id>/cancel/` — stop an unapproved run
- `GET/POST api/pipeline/rules/` — pricing rules (general or
  category-scoped; the category rule wins for its shelf)
- `GET/POST api/pipeline/categories/` — company categories
- `PUT api/pipeline/files/<id>/category/` — shelve a whole file
- `GET/POST api/pipeline/suppliers/` — supplier list (auto-created on
  import), `PUT .../order/` for favorites, `.../<id>/notes/` for notes
- `POST api/pipeline/quotes/` — start a customer quote from the latest
  approved runs; `POST .../<id>/confirm/` learns reconcile groups
- `GET api/pipeline/quotes/<id>/download/<fmt>/?category_id=` — full
  quote or one shelf (`none` = uncategorized)
- `GET api/pipeline/quotes/<id>/message/?category_id=` — Telegram
  message, frozen whole or re-rendered per shelf
- `DELETE api/pipeline/quotes/<id>/` — operator-only quote deletion
  (list and detail); every quote field is editable with 2-decimal
  currency rounding
- `GET api/pipeline/assistant/status/?organization_id=` — paid
  assistant reachability (free to ask)
- `POST api/pipeline/assistant/chat/` — one help turn, spent as
  `task=assistant` in the usage log

Cross-tenant identifiers answer 404.

## Organization endpoints

- `GET api/orgs/auth/csrf/` — mint the CSRF cookie
- `POST api/orgs/auth/login/` — `{"username", "password"}` opens a session
- `POST api/orgs/auth/logout/` — close the current session
- `GET api/orgs/auth/me/` — current session user
- `GET api/orgs/memberships/` — caller's own memberships
- `GET api/orgs/active/` / `POST api/orgs/active/` — read or select the
  session's active organization
- `POST api/orgs/invitations/` — invite an email (managers only)
- `POST api/orgs/invitations/accept/` — accept by token
- `GET api/orgs/<id>/members/` — member list (members only)
- `GET api/orgs/preferences/me/` / `PUT` — display preferences (date format)
- `GET/PUT api/orgs/<id>/settings/` — default + destination currency
- `GET api/orgs/<id>/usage/` — monthly credits, reset date, grand total,
  per-import spend log
- `GET api/orgs/<id>/gdpr/export/` — ZIP of every company row plus
  stored bytes (Owner only)
- `POST api/orgs/<id>/gdpr/delete/` — `{"confirm": "<org name>"}` erases
  the workspace, its bytes, and orphaned logins (Owner only; blocked
  while runs are in flight)

## How interpretation works

1. **Parse** (local) — hostile-file guards, header detection, mapping.
2. **Normalize locally** — every row is parsed deterministically first:
   capacity units (`1TB` → gigabytes), European decimals, 3-letter
   currency codes, lock states, grades.
3. **Model gap rows only** — rows that normalized cleanly never touch the
   model. Only rows with gaps are dispatched, in chunks of ≤32 rows within
   the input cap, each with its own budget reservation and live progress.
   Clean files import in seconds with zero model spend — even with a key
   configured, even with an exhausted budget.
4. **Validate** — per-row status (ok / excluded / needs-input) plus
   eight file-level gates: review coverage, criticals complete,
   quantities reconcile, provenance complete, rows reconcile with
   evidence, roles bound to evidence (prices and stock bind to their
   source spans — a capacity or model number can never certify as a
   price), no incompatible merge, and releasable-or-explicit-empty.
5. **Review → approve → export** — reviewers answer or ignore records;
   approval binds to exact versions; exports are byte-identical artifacts.

Cancel stops a running worker at the next chunk boundary (no further paid
calls) and shelves runs awaiting input or review. Approved runs cannot be
cancelled — remap one to start over.

Pasted text enters the same pipeline as a versioned draft: each line
becomes one candidate record, and meaning arrives only through
confirmed rules — your set values, plus typed operations the preview
assistant proposes (which number is the price, which lines are
headers, which grade a section carries, how multi-line products join)
and you apply. Rules persist server-side with a revision number, so
refreshes, long chats, and worker processing can never silently
change the confirmed interpretation; proposal, rules-panel, and
confirm calls all carry the revision and fail 409 when stale. A
price-only policy covers lists that carry prices but no stock:
quantities stay unknown and are omitted instead of invented.

## Simulated mode

`OFFERFLOW_PROVIDER` defaults to `fixture`. The fixture provider is a pure
function of its input: deterministic, labelled synthetic, and performing
zero network I/O. No model credential is configured or mounted. The FX
provider likewise defaults to deterministic synthetic demo rates, always
labeled simulated.

Zero-traffic proof: with default startup, no `provider_call` usage events
may exist for any run, the worker container has no external route
(internal-only network), and loopback health checks must succeed. Backend
egress is not network-blocked (it joins the public network so its
published ports survive); its isolation rests on the fixture default,
`ALLOW_PAID=false`, and zero budgets.

## Real-model activation (owner only)

Do this only after the simulated flow is green and you have decided a
conservative token budget. You need a Meta Model API key of your own
(keys start with `LLM_`) and understood credit expiry and post-credit
billing. Prefer Standard billing for sensitive supplier lists:
Contributor-tier inputs are used to train Meta's models.

### Model ids (`OFFERFLOW_META_MODEL`)

Copy the id verbatim from your account: spelling is exact and
case-sensitive, and a wrong id fails closed at dispatch. OfferFlow reads
gap rows through chat completions, so the field must hold a `muse-spark`
chat model. Nine ids were visible on one Contributor account on
30 Oct 2026: five chat models (`muse-spark-1.1`, `muse-spark-1.2`,
`muse-spark-1.2-contributor`, `muse-spark-1.3`,
`muse-spark-1.3-contributor`) plus `muse-image-1.0`,
`muse-voice-transcribe-1.0`, `muse-wonder-1.0`, and `sam-3.1`, which are
not chat models and cannot serve this field. Model lists change, so
re-list your own access with the free call in `.env.example` (it spends
zero tokens) whenever dispatch complains about the model.

1. In `.env`, set `OFFERFLOW_PROVIDER=meta`, `OFFERFLOW_ALLOW_PAID=true`,
   `OFFERFLOW_META_API_KEY`, and `OFFERFLOW_META_MODEL` (the exact model
   id from your account).
2. Start with the integration overlay (default startup stays isolated):
   `docker compose -f compose.yaml -f compose.integration.yaml up -d --build`
3. In the Django admin, open **AI provider config** and store the shared
   key/model (Fernet-encrypted at rest, never displayed). This row serves
   all companies; disabling it refuses every paid call even if env vars
   exist. Without a complete admin row, the env vars are the fallback.
4. Under **Operator spend**, approve the operator-wide monthly ceiling.
   On a fresh install click **Add operator spend** first — with no row
   the admin shows no change form. Zero (the default) refuses every
   paid call for every company, even when a company has its own
   credits. The ceiling resets on the
   calendar month; the operator lifetime total never resets.
5. Under **Spend allowances**, set each company's **monthly credits**.
   Zero (the default) refuses every paid call. Credits reset on each
   company's own billing anniversary (activation day); the grand total
   never resets. The lifetime budget stays as an extra ceiling.
6. Process one small upload and check the run's `provider_call` usage
   events: observed tokens must reconcile against the reservation, and
   the allowance must show matching spend with nothing left reserved.

Budgeting illustration (Contributor list prices at time of writing:
$0.10/M input, $0.20/M output): 25,000,000 monthly tokens would cost
about $5 only if every token billed at the output rate. The software
enforces token ceilings, not money: your bill depends on the provider's
current prices, the input/output mix, and cached-input treatment, so
size ceilings from your own invoice math, not from this example.
Prefer Standard-billed models for sensitive supplier lists:
Contributor-tier inputs are used to train Meta's models.

### Admin credit controls

- Edit monthly credits inline on the Spend allowances list, or use the
  "Increase or decrease monthly credits…" action for bulk adjustments
  (allowances floor at zero).
- "Reset current-period usage to zero" zeroes used-this-period at once;
  the billing cycle and grand total are untouched.
- The billing-cycle anchor day is editable per company; `paused` is the
  per-company kill switch. `paused` on **Operator spend** is the global
  kill switch and stops new dispatch for every company.

Kill switch, in order: pause the company in the admin, disable the AI
provider config, revoke the key at the provider, unset the paid
variables, and return to plain `docker compose up`. Note that revoking
the key cannot unspend accepted requests.

### Assistant safety

The help assistant has no data access and no tools: it cannot see any
company's rows or run anything. A server-side guard refuses SQL shapes,
code, imperative bulk-destructive commands, and requests for other
companies' data before any token is spent; each refusal writes an
**Assistant flag** row (organization, user, question, reason) for the
super-admin. The admin can ban a repeat offender from the assistant
(flag action or the User preference switch) or deactivate the account
via the standard User admin. Out-of-scope answers always return the
same fixed refusal text.

On the Upload page a preview-assistant mode receives only the pending
paste preview — no stored company data — and proposes typed
interpretation operations as reviewable cards. Nothing is applied
until the user clicks **Apply to preview**; user edits always win
over assisted values.

### GDPR self-service

Owners export (`gdpr/export/`) or erase (`gdpr/delete/`, typed-name
confirmation) their workspace from Settings → Privacy & data. The ZIP
holds every company row plus original uploads and artifacts; a manifest
notes any unreadable bytes. Erasure deletes the organization, its
stored bytes, and member logins left without an organization (shared
and staff accounts survive), and refuses while runs are in flight.
The public `/privacy` and `/terms` pages disclose processing, the
Contributor-training note, and these rights.

## Supplier backfill

Imports create supplier records automatically, but files imported before
suppliers existed leave no rows. Backfill them once per database:

```sh
docker compose exec backend python manage.py sync_suppliers
docker compose exec backend python manage.py sync_suppliers --organization-id 3
```

Names come from existing offers (case-insensitive, blanks skipped);
details, favorites, ratings, and notes are then filled in via the UI.
The command is idempotent: a second run creates nothing.

## Rebuilding images

Volume-mounted code reloads live in the backend (autoreload) but **not**
in the worker: restart it after backend changes, always with the overlay
if the paid lane is active (plain `up` silently drops worker egress):

```sh
docker compose -f compose.yaml -f compose.integration.yaml up -d --build backend worker
```

New Python dependencies require the `--build`; code-only changes need just
`docker compose restart worker`. The frontend dev server reloads live; no
rebuild needed for UI changes.

## Runtime isolation switch

Fresh installs start in single-role mode: backend and worker connect as
the bootstrap superuser, which bypasses every row policy
(`runtime_role_privileged: true` on `/api/health/`, plus check
`offerflow.W001`). The switch below provisions a restricted runtime
role for backend AND worker (both read the same `.env`) while the
superuser stays for migrations only. It never touches volumes or
existing rows; every step is reversible.

Run the steps in order, from the repository root, with the stack up:

1. Back up (non-destructive; substitute your superuser credentials):
   ```sh
   docker compose exec -T -e PGPASSWORD='SUPERUSER_PASSWORD' db \
     pg_dump -U offerflow -d offerflow -F c -f /tmp/offerflow-backup.dump
   docker cp offerflow-db-1:/tmp/offerflow-backup.dump ./offerflow-backup.dump
   ```
   Keep the file until the switch is verified, then delete or vault it.
2. In `.env`, set `OFFERFLOW_DB_ADMIN_USER`/`OFFERFLOW_DB_ADMIN_PASSWORD`
   to the current superuser credentials (the same values the runtime
   uses today). Restart to pick them up — behavior is unchanged:
   ```sh
   docker compose restart backend worker
   ```
3. Provision the restricted role (replace `RUNTIME_PASSWORD` with a fresh
   URL-safe secret, e.g. from `openssl rand -base64 18 | tr -dc A-Za-z0-9`):
   ```sh
   docker compose exec -T backend python manage.py provision_runtime \
     --app-role offerflow_app --app-password 'RUNTIME_PASSWORD'
   ```
   The command verifies role attributes, table grants, bootstrap
   helpers, and every row policy, and fails loudly if any check fails.
4. In `.env`, point the runtime at the new role:
   `OFFERFLOW_DB_USER=offerflow_app` with the password from step 3.
   Then restart both consumers:
   ```sh
   docker compose restart backend worker
   ```
5. Verify:
   - `curl http://127.0.0.1:8384/api/health/` shows
     `"runtime_role_privileged": false`.
   - `docker compose exec -T backend python manage.py check` reports no
     `offerflow.W001`.
   - `docker compose exec -T backend python manage.py provision_runtime
     --verify-only --app-role offerflow_app` passes.
   - Sign in, upload a small file, and confirm the run processes (this
     exercises the worker bootstrap under the restricted role); accept
     one invitation to exercise the invitation resolver.
6. From now on, run schema and superuser commands on the admin
   connection: `migrate --database=admin`,
   `createsuperuser --database=admin`. Plain `migrate` targets the
   restricted runtime and fails by design.

Rollback: restore the previous `OFFERFLOW_DB_USER`/`OFFERFLOW_DB_PASSWORD`
in `.env` and `docker compose restart backend worker`. To remove the
unused role afterwards: `docker compose exec -T db psql -U offerflow -d
offerflow -c 'DROP ROLE IF EXISTS offerflow_app;'`.

Caveat: the `db` container bootstraps its superuser from
`OFFERFLOW_DB_*` only when its volume is created. If you ever recreate
the `dbdata` volume, re-run this procedure from step 1 — and never run
the isolation test module against the application database.

## Reset

Recreate everything from scratch after changing passwords in `.env`, or
whenever local state is suspect. Passwords bake into the database and
broker data on first boot, so recreating only the containers is not
enough: the volumes must go too.

```sh
docker compose down -v
docker compose up -d --build
sleep 10
curl http://127.0.0.1:8384/api/health/
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
```

`down -v` deletes the `dbdata`, `brokerdata`, `filestore`,
`frontend_modules`, and `frontend_next` volumes: all local uploads, the
database (re-migrate afterwards, as above), and the frontend caches (the
next boot re-runs `npm install`, so it takes longer). Passwords must stay
letters and digits only; the broker URL is assembled from the password and
special characters break AMQP parsing.

## Troubleshooting

- `permission denied` on the Docker socket or "Cannot connect to the Docker
  daemon": start Docker Desktop and re-run from your own terminal.
- `Missing required environment variable`: `.env` is absent or incomplete;
  re-copy `.env.example` and fill every value.
- `port is already allocated`: something already listens on 4344 or 8384;
  stop it or change the published port in `compose.yaml` (keep it
  loopback-only).
- `Payload exceeds the input cap; refusing dispatch`: a chunk (or a single
  row) is bigger than `OFFERFLOW_META_MAX_INPUT_TOKENS`; raise the cap or
  slim the file. Never silently truncated.
- `Monthly credits exhausted` / `No monthly credits`: top up the company
  under Spend allowances in the admin.
