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
verify with a real upload instead.

Run the full test suite:

```sh
docker compose exec backend python manage.py test
```

Row-policy tests require PostgreSQL and skip automatically on other
engines; under Compose they run against the real database. Local runs use
`OFFERFLOW_DB_ENGINE=sqlite`.

Only the backend API port is published, on loopback only (host 8384 →
container 8000). PostgreSQL and RabbitMQ stay on the internal container
network with no published ports.

## Pipeline endpoints

- `POST api/pipeline/uploads/` — multipart `organization_id` + `file`
  (`.xlsx`, `.xls`, `.csv`, `.txt`, `.tsv`, `.docx`; 10 MB / 2000 rows;
  `.doc` refused with guidance)
- `POST api/pipeline/previews/` + `GET api/pipeline/previews/<key>/render/`
  — preview-first import with live mapping
- `POST api/pipeline/previews/<key>/confirm/` — import the preview
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
- `GET/POST api/pipeline/rules/` — pricing rules

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
4. **Validate** — per-row status (ok / excluded / needs-input) plus six
   file-level gates: review coverage, criticals complete, quantities
   reconcile, provenance complete, no cross-supplier merge, and
   releasable-or-explicit-empty.
5. **Review → approve → export** — reviewers answer or ignore records;
   approval binds to exact versions; exports are byte-identical artifacts.

Cancel stops a running worker at the next chunk boundary (no further paid
calls) and shelves runs awaiting input or review. Approved runs cannot be
cancelled — remap one to start over.

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
4. Under **Spend allowances**, set each company's **monthly credits**.
   Zero (the default) refuses every paid call. Credits reset on each
   company's own billing anniversary (activation day); the grand total
   never resets. The legacy lifetime budget stays as an extra ceiling.
5. Process one small upload and check the run's `provider_call` usage
   events: observed tokens must reconcile against the reservation, and
   the allowance must show matching spend with nothing left reserved.

Budgeting example (Contributor: $0.10/M input, $0.20/M output):
25,000,000 monthly tokens cap worst-case spend at $5, since output is the
pricier direction.

### Admin credit controls

- Edit monthly credits inline on the Spend allowances list, or use the
  "Increase or decrease monthly credits…" action for bulk adjustments
  (allowances floor at zero).
- "Reset current-period usage to zero" zeroes used-this-period at once;
  the billing cycle and grand total are untouched.
- The billing-cycle anchor day is editable per company; `paused` is the
  per-company kill switch.

Kill switch, in order: pause the company in the admin, disable the AI
provider config, revoke the key at the provider, unset the paid
variables, and return to plain `docker compose up`. Note that revoking
the key cannot unspend accepted requests.

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
