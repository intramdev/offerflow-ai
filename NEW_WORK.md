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

## Next updates

As development progresses, we'll add dated notes describing what was built, who contributed and the tests or demo that show it working. Existing dependencies and reused materials will be identified separately.

The submission and demo will distinguish the work completed during the hackathon from anything that existed beforehand.
