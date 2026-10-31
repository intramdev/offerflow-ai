# OfferFlow AI

**Beta — version 0.4.0.** Simulated-first and fully usable with no model
key; paid Muse features activate per company with credits.

Supplier information arrives in messy spreadsheets and documents. OfferFlow AI
turns them into validated customer quotes through a checkable pipeline: upload,
preview the mapping, interpret, validate, review the ambiguous rows with a
person, approve, and export. Nothing is ever guessed — unreadable rows pause
for a decision or are excluded with a stated reason.

Built by **Gian Paolo Cipicchia** for **IN.TRA.M s.r.o.** for the **Meta
Global AI Developer Hackathon**, organized by Cerebral Valley. Independent
entry: Meta is not affiliated with this app and accepts no responsibility
for it.

## What it does

- **Preview-first import** — upload `.xlsx`, `.xls`, `.csv`, `.txt`, `.tsv`,
  or `.docx` (up to 10 MB / 2000 rows; legacy `.doc` is refused with
  guidance). You see a live preview and adjust the column mapping before
  anything is imported. Columns can be ignored or replaced with a fixed
  value, and remembered mappings are proposed for repeat suppliers.
- **Local interpretation** — capacities (`1TB` → gigabytes), European
  decimals, 3-letter currency codes, lock states, and grades are parsed
  deterministically. Files that read cleanly import in seconds with no
  model involved at all.
- **Human review** — ambiguous rows pause with the original cell evidence on
  screen. Answer the question, or drop the record from the quote with one
  click (it lands in the excluded sheet with a reason, never silently).
- **Customer quotes** — pricing rules plus destination-currency conversion
  using the exchange rate recorded for each file on its own currency date.
  Rates are shown per currency with their date; missing rates skip the line
  with a note instead of guessing.
- **Approve and export** — approval binds to the exact source, rules, and
  normalized result. Exports (XLSX/CSV) carry sheet and row references and
  are byte-identical to the validated artifact.
- **Multi-company** — organizations with Owner/Admin/Operator/Viewer roles,
  per-tenant isolation, audit history, and per-user display preferences
  (US/EU date formats).
- **Monthly AI credits** — when a model key is configured, each company gets
  a monthly token allowance that resets on its own billing anniversary, a
  dashboard gauge with a per-import spend log, and a never-reset grand
  total. Budgets refuse loudly before overspend; kill switches pause a
  company or the whole model lane.
- **Customer-quote matching** — one quote built from the latest approved
  run per supplier file, merging same-brand/capacity/grade products
  across spelling variants. Memory, normalized, and model lanes propose
  groups; uncertain groups reconcile visually and every confirm teaches
  the company its suppliers' spellings.
- **Categories** — optional shelves per company: map a whole file, let
  the model shelve lines, or reconcile manually. Quotes, messages, and
  downloads split per category or stay whole; pricing rules can target
  one category and override the general rule there.
- **Suppliers** — auto-created on import with drag-and-drop favorites
  order, five-aspect star ratings, timestamped notes, location, and
  customs fees.
- **Help assistant** — a paid-model chat bubble (bottom-right, only when
  the model lane is up) answering app-how-to questions. Spend lands in
  the same usage log; a server-side guard refuses SQL, code,
  bulk-destructive commands, and other companies' data for free, and
  flags attempts for the admin, who can ban or deactivate abusers.
- **GDPR self-service** — owners download every company row plus stored
  bytes as a ZIP, or erase the workspace with typed-name confirmation.
  Public privacy and terms pages disclose processing and rights.
- **Profile and accessibility** — each user edits name, email, theme
  (light/dark/system, light by default), typeface, text size,
  high-contrast, and reduced motion; preferences travel with the account
  and meet EN 301 549/WCAG AA aims.
- **Admin activity dashboard** — staff see per-user sign-ins and token
  spend ranked top-down (7/30 days or all time), plus live token and
  sign-in charts on the admin home page.
- **Stuck-slot release** — a run blocked by a stale in-flight lock can be
  released from the credits page for the user's own company only; the
  release is audited and never touches billed spend.
- **Password recovery** — email links, single-use, 30 minutes, throttled;
  super admins can also reset a user password from the backend.
- **Help center** — the in-app Help item documents every screen.
- **Quote line editing** — rewrite any ready quote's customer-facing
  product text or hide whole lines; labels apply to files and the
  Telegram message while prices and sources stay frozen.
- **Quote export template** — per-company settings choose the XLSX/CSV
  columns, the message rows' details, and a custom header and footer.
  Supplier names stay out of customer outputs unless opted back in.
- **Quote pagination** — the quote list shows the newest first, 50 per
  page, with first/previous/numbered/next/last navigation.

## Without AI vs with a Muse key

The system is fully usable with no model key (simulated lane). Adding a
Muse API key unlocks model interpretation for the rows strict parsing
cannot read.

| Capability | No key (simulated) | With Muse key (paid) |
|---|---|---|
| Upload, preview, mapping, remap, delete | Yes | Yes |
| Local parsing (units, decimals, currencies, locks) | Yes | Yes |
| Review questions, ignore-a-record, approve, export, quotes, FX | Yes | Yes |
| Rules engine, multi-company, audit, credits dashboard | Yes | Yes |
| Model reads rows local parsing cannot (gap rows only) | No — gaps always ask the reviewer | Yes, in bounded chunks with live progress |
| Model matching + categorization lanes | Memory/normalized lanes only; the rest asks for review | Yes, bounded single-shot calls |
| Help assistant chat bubble | Hidden (no model lane) | Visible; every turn spends credits |
| Monthly credit allowances, spend log, grand total | Visible, zero spend | Enforced per company |
| Admin-managed key/model, budget + pause controls | Key section empty | Full control |
| Assistant abuse flags + bans | No traffic to flag | Guard, flags, ban, deactivation |
| Any network traffic to a model provider | None, by construction | Only for gaps, matching, categories, chat — only with budget |

Local-first routing keeps paid use minimal: clean files import free even
with a key configured, and a 357-row file with a handful of gaps costs a
few thousand tokens instead of a full-file pass.

## Quickstart (simulated, no key needed)

Prerequisites: Docker Desktop, Git, ports 4344 and 8384 free on loopback.

```sh
git clone https://github.com/intramdev/offerflow-ai.git
cd offerflow-ai
cp .env.example .env   # then fill every secret; passwords: letters/digits only
docker compose up -d --build
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
```

Then open the app at `http://127.0.0.1:4344/app/login` (create an
organization + owner membership in the Django admin at
`http://127.0.0.1:8384/admin/` first) and check API health at
`http://127.0.0.1:8384/api/health/`. Full walkthrough, paid activation,
and troubleshooting: the in-app **Docs** page and
[backend/README.md](backend/README.md).

## Enabling the paid model lane

1. In `.env`: `OFFERFLOW_PROVIDER=meta`, `OFFERFLOW_ALLOW_PAID=true`, your
   `OFFERFLOW_META_API_KEY` (starts with `LLM_`), and the exact
   `OFFERFLOW_META_MODEL` id from your account (a `muse-spark` **chat**
   model — see `.env.example` for spellings).
2. Start with the integration overlay so the worker can reach the API:
   `docker compose -f compose.yaml -f compose.integration.yaml up -d --build`
3. In the Django admin, open **AI provider config** to store the shared
   key/model (encrypted, never displayed), and set each company's monthly
   credits under **Spend allowances**. Zero credits refuse every paid call.
4. Process one small upload and check its usage events: tokens must
   reconcile exactly against the reservation.

Budgeting example (Contributor pricing: $0.10/M input, $0.20/M output):
25,000,000 monthly tokens cap worst-case spend at $5, since output is the
pricier direction. Prefer Standard-billed models for sensitive supplier
lists: Contributor-tier inputs are used to train Meta's models.

Kill switch, in order: pause the company (Spend allowances), disable the
AI provider config, revoke the key at the provider, unset the paid
variables, return to plain `docker compose up`.

## Repository layout

- `backend/` — Django + DRF API, pipeline (parse → interpret → validate →
  review → approve → export), rules, FX, credits, admin (`backend/README.md`
  is the operator runbook).
- `frontend/` — Next.js app: public site (docs, demo, data handling) plus
  the authenticated dashboard (upload, files, runs, credits, rules,
  settings).
- `compose.yaml` — default isolated stack (worker has no external route).
- `compose.integration.yaml` — overlay granting the worker API egress for
  the paid lane.
- `NEW_WORK.md` — dated hackathon development notes.

## Verification

- Backend: `432` tests green
  (`docker compose exec backend python manage.py test`), `pyflakes` clean.
- Frontend: production `npm run build` renders all 22 routes.
- Simulated mode emits zero model traffic by construction: no key is
  configured, the worker has no external route, and every synthetic
  artifact is labeled.

## Development and data

Examples and tests use synthetic data only. This repository contains the
new hackathon work; it does not include proprietary application code or
real supplier and customer records. [NEW_WORK.md](NEW_WORK.md) records
what was built during the event.

## License

Copyright © 2026 IN.TRA.M s.r.o.

Licensed under **GNU AGPL version 3 only** (`AGPL-3.0-only`). See
[LICENSE](LICENSE).

Third-party components and models remain subject to their own licenses
and terms.
