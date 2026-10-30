# OfferFlow AI backend — local development (Package A)

Simulated foundation: Django API skeleton, fixture provider with fail-closed
selection, organization-scoped local file adapter, health endpoint reporting
the provider mode, and Celery worker wiring. No real model calls exist yet.

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
docker compose up --build
```

Run database migrations once, from a second terminal (never auto-run
migrations from every replica):

```sh
docker compose run --rm backend python manage.py migrate
```

Check health (expect `"mode": "simulated"`):

```sh
curl http://127.0.0.1:8384/api/health/
```

Run the full test suite:

```sh
docker compose run --rm backend python manage.py test
```

Row-policy tests require PostgreSQL and skip automatically on other
engines; under Compose they run against the real database.

Only the backend API port is published, on loopback only. PostgreSQL and
RabbitMQ stay on the internal container network with no published ports.

## Pipeline endpoints (Package C)

- `POST api/pipeline/uploads/` — multipart `organization_id` + `file` (.xlsx)
- `GET api/pipeline/runs/<id>/` — run status with validation results
- `GET api/pipeline/runs/<id>/offers/` — normalized offers with provenance
- `GET api/pipeline/runs/<id>/reviews/` — open review requests (`?status=all`)
- `POST api/pipeline/reviews/<id>/answer/` — `{"choice"}` or `{"value"}`
- `POST api/pipeline/runs/<id>/approve/` — bind approval to exact versions
- `POST api/pipeline/runs/<id>/export/` — `{"format": "xlsx"|"csv"}`
- `GET api/pipeline/artifacts/<id>/download/` — exact stored bytes
- `POST api/pipeline/runs/<id>/cancel/`

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

## Simulated mode

`OFFERFLOW_PROVIDER` defaults to `fixture`. The fixture provider is a pure
function of its input: deterministic, labelled synthetic, and performing zero
network I/O. No model credential is configured or mounted. Requesting the
`meta` provider, or any unknown provider, is refused. Network-level
enforcement and request instrumentation land with the real adapter in
Package E, where the spending risk actually appears.

## Real-model activation (owner only)

Do this only after the simulated flow is green and you have decided a
conservative token budget. You need a Meta Model API key of your own
(keys start with `LLM_`), Standard billing (never a training-permitting
mode for supplier content), and understood credit expiry and post-credit
billing.

1. In `.env`, set `OFFERFLOW_PROVIDER=meta`, `OFFERFLOW_ALLOW_PAID=true`,
   `OFFERFLOW_META_API_KEY`, and `OFFERFLOW_META_MODEL` (the exact model id
   from your account).
2. In the Django admin, open Spend allowances for your organization and set
   a token budget. Zero (the default) refuses every paid call.
3. Start with the integration overlay (default startup stays isolated):
   `docker compose -f compose.yaml -f compose.integration.yaml up --build`
4. Process one small upload and check the run's `provider_call` usage
   event: observed tokens must reconcile against the reservation, and the
   spend allowance must show matching spend with nothing left reserved.
5. Keep automatic retries off and concurrency at one until accounting is
   validated across several runs.

Kill switch, in order: set the allowance to paused in the admin, revoke
the key at the provider, unset the paid variables, and return to plain
`docker compose up`. Note that revoking the key cannot unspend accepted
requests.

Zero-traffic proof for simulated mode: with default startup, no
`provider_call` usage events may exist for any run, the worker container
must fail to reach external hosts (internal-only, no external route),
and loopback health checks must succeed. Backend egress is not
network-blocked (it joins the public network so its published ports
survive); its isolation rests on the fixture default, ALLOW_PAID=false,
and zero budgets.

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
docker compose run --rm backend python manage.py migrate
docker compose run --rm backend python manage.py createsuperuser
```

`down -v` deletes the `dbdata`, `brokerdata`, `filestore`,
`frontend_modules`, and `frontend_next` volumes: all local uploads, the
database (re-migrate afterwards, as above), and the frontend caches (the
next boot re-runs `npm install`, so it takes longer). Passwords must stay
letters and digits only; the broker URL is assembled from the password and
special characters break AMQP parsing.

## Troubleshooting

- `permission denied` on the Docker socket or "Cannot connect to the Docker
  daemon": start Docker Desktop and re-run from your own terminal. The
  assistant session cannot reach your Docker daemon by design.
- `Missing required environment variable`: `.env` is absent or incomplete;
  re-copy `.env.example` and fill every value.
- `port is already allocated`: something already listens on 8384; stop it or
  change the published port in `compose.yaml` (keep it loopback-only).
