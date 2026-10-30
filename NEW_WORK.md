# Hackathon development notes

## Starting point

OfferFlow AI draws on IN.TRA.M's experience with supplier information and business automation. A separate commercial product and the initial project idea existed before the hackathon. That product's source code and commit history are not included here.

## Work completed during the event

**27 October 2026:** prepared the project documentation, development scope and repository setup. The initial documents were prepared with Codex assistance. The repository currently contains documentation and configuration, not a working application.

**30 October 2026:** implemented Package A (local simulated Compose foundation) with Muse Code assistance: Django 5.2 LTS skeleton with fail-closed fixture provider, organization-scoped local file adapter, health endpoint reporting simulated mode, Celery/RabbitMQ wiring with a bounded worker, loopback-only Compose services with internal database/broker and resource limits, pinned dependencies, and unit tests for provider selection and storage scoping. Validation: static checks in the assistant session; container startup and test runs are owner-executed (the assistant sandbox has no Docker daemon access).

**30 October 2026:** implemented Package B (tenant and identity foundations) with Muse Code assistance: organization, membership, and invitation records with Owner/Admin/Operator/Viewer roles; session active-organization selection; invitation and revocation flows with manager checks and last-owner protection; middleware publishing database tenant context with mandatory reset; a row-level-security policy scoping invitations per organization as the proven pattern for business tables; and membership HTTP endpoints with server-side membership rechecks. Validation: behavioral suite in the assistant session plus row-policy tests against PostgreSQL.

**30 October 2026:** implemented Package C (spreadsheet vertical slice) with Muse Code assistance: hostile-file intake guards with header detection, deterministic normalization that never infers criticals, six mandatory validation gates, clarification review with approval binding to exact versions, XLSX/CSV export with provenance and formula-injection guards, an idempotent Celery job with tenant context, per-organization row policies across pipeline tables, and the full HTTP surface with cross-tenant 404 discipline. Validation: engine-agnostic suite in the assistant session; row-policy tests run under Compose.

**30 October 2026:** implemented Package D part 1 (session auth plus public website) with Muse Code assistance: login/logout/me/CSRF endpoints with explicit login CSRF enforcement and token rotation, and a Next.js 16 + React 19 + TypeScript frontend with seven static public routes (home, how-it-works, synthetic demo, self-hosting, docs walkthrough, data handling, source/licenses), same-origin API rewrites, and a loopback-only Compose service. Validation: backend suite green including CSRF-enforcement tests; production frontend build renders all routes statically.

**30 October 2026:** implemented Package D part 2 (authenticated app UI) with Muse Code assistance: session/CSRF API client, middleware auth gate, sign-in, organization picker, upload, run detail with checks/offers/review answers/approve/export/cancel/downloads, plus first-sign-in docs. Validation: production frontend build renders all routes; backend pipeline suite green including the run-artifacts addition.

**30 October 2026:** implemented Package E code (paid adapter without live calls) with Muse Code assistance: Meta Chat Completions provider with structured output over the OpenAI SDK, per-organization token budgets with atomic reserve/reconcile/release, in-flight limits, kill switch via pause plus key revocation, input/output caps, disabled SDK retries, internal-network simulated isolation with an explicit integration overlay, and an activation runbook. No live calls were made and no credentials were created. Validation: mocked-transport suite green; live runs await explicit owner activation.

**30 October 2026:** fixed capacity units (`1TB` to `1024MB` now parse to gigabytes; fractional results still ask the reviewer) and activated the paid Meta path with Muse Code assistance: verified 9 accessible models with the free list call, documented exact `OFFERFLOW_META_MODEL` spellings, flipped `OFFERFLOW_PROVIDER=meta` with a 10,000-token budget on ACME ltd, and proved one live upload end to end (1 row: 291 prompt + 661 completion tokens, budget reconciled exactly). Also found and fixed a crash-looping worker on a stale image missing `xlrd`/`openai`. Validation: backend suite 231 tests green.

**30 October 2026:** added date-based exchange rates with Muse Code assistance: organization destination currency in Settings, per-file currency date defaulting to the upload day with a calendar control on the Files page, recorded rate tables stored on each file, a simulated fixture rates provider (deterministic per day, always labeled synthetic; real providers refused until configured), and a rule step that converts quotes to the destination using only the file's recorded rate — missing rates skip the line with a note instead of guessing. Validation: backend suite 219 tests green; production frontend build renders all 14 routes; migrations applied to the Compose database.

**30 October 2026:** added preview-first multi-format import and the Tailwind dashboard with Muse Code assistance: XLS/XLSX/CSV/TXT/TSV/DOCX intake (legacy DOC refused with guidance), live mapping preview with ignore/set-value columns and remembered supplier mappings, file grid with search/rename/paging, async remap of finished runs, per-record ignore from review questions, cancel semantics (pre-approval only, mid-flight abort at chunk boundaries), and a Tailwind v4 + shadcn-style responsive UI. Validation: backend suite green; production frontend build renders all routes.

**30 October 2026:** added the monthly AI credit system with Muse Code assistance: admin-managed key/model shared by all companies (Fernet-encrypted, never displayed), per-company monthly token allowances resetting on each billing anniversary, a dashboard gauge with per-import spend log and a never-reset grand total, plus admin actions to adjust allowances and reset period usage. Raised the ACME ltd allowance to 25M tokens ($5 worst case at Contributor output pricing). Validation: backend suite green including 21 credit tests; migrations applied to the Compose database.

**30 October 2026:** optimized paid interpretation with Muse Code assistance: local-first routing (clean rows never touch the model, so clean files import free in seconds), chunked dispatch (≤32 rows per call with per-chunk budgets and live run progress), and per-call spend events. Proved on a 357-row live file. Also refreshed the root README, backend runbook, and site docs with a simulated-vs-paid capability comparison. Validation: backend suite 265 tests green; production frontend build green.

## Next updates

As development progresses, we'll add dated notes describing what was built, who contributed and the tests or demo that show it working. Existing dependencies and reused materials will be identified separately.

The submission and demo will distinguish the work completed during the hackathon from anything that existed beforehand.
