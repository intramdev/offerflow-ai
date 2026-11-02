"""Background execution: idempotent, transaction-aware, tenant-scoped."""

from __future__ import annotations

from celery import shared_task
from django.db import transaction

from common.provider import ProviderNotConfigured, get_provider
from pipeline.models import (
    AuditEvent,
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    RunStatus,
    SourceRow,
    TERMINAL_STATUSES,
    UsageEvent,
    transition_run,
)
from pipeline.normalize import (
    compute_offer_status,
    merge_interpreted_cells,
    normalize_cells,
)
from pipeline.validate import run_validations


def record_audit(organization_id, user, action, target_type="", target_id="", metadata=None):
    """Append one audit event. System actors pass user None."""
    return AuditEvent.objects.create(
        organization_id=organization_id,
        user=user,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        metadata=metadata or {},
    )


def record_usage(organization_id, run, kind, quantity=1, detail=None, user=None):
    """Append one usage event for later budget reconciliation."""
    return UsageEvent.objects.create(
        organization_id=organization_id,
        run=run,
        user=user,
        kind=kind,
        quantity=quantity,
        detail=detail or {},
    )


def enqueue_run(run_id: int) -> None:
    """Publish the job only after the creating transaction commits."""
    transaction.on_commit(lambda: process_import_run.delay(run_id))


class RunCancelled(Exception):
    """Raised inside the worker when the run was cancelled mid-flight."""


def cancel_run(*, run: ImportRun, user) -> ImportRun:
    """Stop an import that has not been approved yet.

    Cancelling halts a running worker at the next chunk boundary (no
    further paid calls) and shelves runs awaiting input or review.
    Approved runs move forward to export instead; remap one to start
    over with a different mapping.
    """
    from django.core.exceptions import ValidationError

    if run.status in TERMINAL_STATUSES:
        raise ValidationError("That run is already finished.")
    if run.status == RunStatus.APPROVED:
        raise ValidationError(
            "That run is already approved; remap it to start over."
        )
    transition_run(run, RunStatus.CANCELLED)
    record_audit(run.organization_id, user, "run_cancelled", "run", run.id)
    return run


def _ask_mapping(run: ImportRun, exc) -> None:
    """Store the mapping report and ask one choice question per missing column."""
    from pipeline.review import mapping_choice_label

    stored = run.mapping or {}
    run.mapping = {
        "headers": exc.headers,
        "detected": exc.column_map,
        "confirmed": {},
        "constants": stored.get("constants") or {},
        "ignored": stored.get("ignored") or [],
        "sheet": stored.get("sheet"),
        "sheets_omitted": stored.get("sheets_omitted", []),
    }
    if stored.get("free_text") is True:
        run.mapping["free_text"] = True
    run.save(update_fields=["mapping", "updated_at"])
    ReviewRequest.objects.filter(run=run, offer__isnull=True).delete()
    choices = [
        mapping_choice_label(header, position)
        for position, header in enumerate(exc.headers)
        if header
    ]
    for canonical in exc.missing:
        ReviewRequest.objects.create(
            organization_id=run.organization_id,
            run=run,
            offer=None,
            kind=ReviewKind.CHOICE,
            field=f"mapping:{canonical}",
            question=(
                f"Which column holds the {canonical}? "
                "The importer could not tell from the headers."
            ),
            evidence={
                "headers": exc.headers,
                "detected": exc.column_map,
                "missing": exc.missing,
            },
            choices=choices,
        )
    transition_run(run, RunStatus.NEEDS_INPUT)
    record_audit(run.organization_id, None, "run_needs_input", "run", run.id)


def remap_run(*, run: ImportRun, user, mapping: dict,
              constants=None, ignored=None) -> ImportRun:
    """Replace a run's column mapping and reprocess the same file.

    Approved and completed runs can be remapped too: their approval and
    export artifacts are discarded so nothing stale survives, and the
    audit trail records where the run came from.
    """
    import contextlib

    from django.conf import settings
    from django.core.exceptions import ValidationError

    from common.storage import LocalFileAdapter
    from organizations.context import set_tenant_context
    from organizations.services import require_organization_role
    from pipeline.previews import _clean_positions, remember_mapping

    if run.status not in (
        RunStatus.NEEDS_INPUT,
        RunStatus.READY_FOR_REVIEW,
        RunStatus.APPROVED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    ):
        raise ValidationError(
            "Runs that are still processing or cancelled cannot be remapped."
        )
    require_organization_role(user, run.organization_id)
    set_tenant_context(run.organization_id, user.id)
    stored = run.mapping or {}
    headers = stored.get("headers") or []
    if not headers:
        raise ValidationError("That run has no detected headers to remap.")
    submitted, effective, clean_constants, clean_ignored = _clean_positions(
        mapping,
        headers,
        stored.get("detected") or {},
        require_complete=True,
        constants=constants,
        ignored=ignored,
    )
    stored["confirmed"] = submitted
    stored["constants"] = clean_constants
    stored["ignored"] = clean_ignored
    run.mapping = stored
    run.save(update_fields=["mapping", "updated_at"])
    ReviewRequest.objects.filter(
        run=run, offer__isnull=True, status=ReviewStatus.OPEN
    ).delete()
    from_status = run.status
    if from_status in (RunStatus.APPROVED, RunStatus.COMPLETED):
        artifact_keys = list(run.artifacts.values_list("storage_key", flat=True))
        run.artifacts.all().delete()
        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        for key in artifact_keys:
            with contextlib.suppress(OSError):
                adapter.delete(key)
    transition_run(run, RunStatus.UPLOADED)
    run.error = ""
    run.save(update_fields=["error", "updated_at"])
    from pipeline.previews import _supplier_hint

    try:
        hint_bytes = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT).read_bytes(
            run.source_file.storage_key
        )
    except (AttributeError, OSError, ValueError):
        hint_bytes = b""
    remember_mapping(
        organization=run.organization,
        headers=headers,
        mapping=effective,
        constants=clean_constants,
        ignored=clean_ignored,
        supplier_hint=_supplier_hint(
            hint_bytes,
            run.source_file.original_name if run.source_file else "",
            effective,
            clean_constants,
            sheet_name=stored.get("sheet"),
        ),
    )
    record_audit(
        run.organization_id,
        user,
        "run_remapped",
        "run",
        run.id,
        {"mapping": submitted, "from_status": from_status},
    )
    # The worker reprocesses; the request returns at once so paid
    # interpretation (minutes on large files) never blocks the browser.
    enqueue_run(run.id)
    run.refresh_from_db()
    return run


def _run_org_for_worker(run_id: int) -> int | None:
    """Resolve a run's organization before tenant context exists.

    The job payload carries only the run id, and row policy hides the run
    row until context is set. The SECURITY DEFINER helper reveals just the
    organization id; the run itself is fetched afterwards under policy.
    Deployments that predate role separation fall back to a direct lookup
    only while their connection role still bypasses row policy; anything
    else fails closed with an explicit error in the worker log.
    """
    import logging

    from django.db import connection
    from django.db.utils import ProgrammingError

    from organizations.context import (
        current_role_is_privileged,
        is_missing_function,
    )

    if connection.vendor != "postgresql":
        return (
            ImportRun.objects.filter(pk=run_id)
            .values_list("organization_id", flat=True)
            .first()
        )
    try:
        # Savepoint: without it the missing-function error aborts the
        # ambient transaction and the fallback queries below die with
        # it instead of resolving the org.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT offerflow_run_org(%s)", [run_id])
                found = cursor.fetchone()
    except ProgrammingError as exc:
        if is_missing_function(exc) and current_role_is_privileged():
            # Legacy single-role deployment: the bootstrap helper was
            # never provisioned, but the role bypasses row policy anyway.
            return (
                ImportRun.objects.filter(pk=run_id)
                .values_list("organization_id", flat=True)
                .first()
            )
        if is_missing_function(exc):
            logging.getLogger(__name__).error(
                "offerflow_run_org() is not installed; run "
                "provision_runtime before processing jobs with the "
                "restricted role."
            )
            return None
        raise
    return found[0] if found else None


def _fail_unresolvable_run(run_id: int) -> None:
    """Best-effort FAILED marking when the org cannot be resolved.

    Without an organization the worker cannot open tenant scope, so
    the run would otherwise sit in uploaded forever with no recovery
    path. The lookup below only succeeds where the connection role
    can see the row without context (legacy privileged deployments,
    which is also the only place this fallback can run); the
    restricted runtime fails closed and leaves the run untouched.
    """
    import logging

    try:
        run = ImportRun.objects.get(pk=run_id)
    except ImportRun.DoesNotExist:
        logging.getLogger(__name__).error(
            "Run %s is unresolvable and invisible to the worker role; "
            "leaving it untouched. If this persists, run "
            "provision_runtime and check tenant provisioning.",
            run_id,
        )
        return
    if run.status in TERMINAL_STATUSES:
        return
    transition_run(run, RunStatus.FAILED)
    run.error = (
        "The worker could not resolve this run's organization; "
        "remap the file to queue it again."
    )
    run.save(update_fields=["error", "updated_at"])
    record_audit(run.organization_id, None, "run_failed", "run", run.id)


@shared_task(name="pipeline.process_import_run", bind=True, max_retries=0)
def process_import_run(self, run_id: int):
    """Process one run exactly once per request; redeliveries are no-ops."""
    org_id = _run_org_for_worker(run_id)
    if org_id is None:
        _fail_unresolvable_run(run_id)
        return "missing"
    from organizations.context import tenant_scope

    # The uploader id below is only an audit label: row policy keys tenant
    # rows off the organization context resolved above, not off this user.
    with tenant_scope(org_id, 0):
        try:
            with transaction.atomic():
                run = ImportRun.objects.select_for_update().get(pk=run_id)
                if run.status in TERMINAL_STATUSES or run.status not in (
                    RunStatus.UPLOADED,
                    RunStatus.FAILED,
                ):
                    return f"noop:{run.status}"
                if run.status == RunStatus.FAILED:
                    transition_run(run, RunStatus.UPLOADED)
                    run.error = ""
                    run.save(update_fields=["error", "updated_at"])
                transition_run(run, RunStatus.PARSING)
        except ImportRun.DoesNotExist:
            return "missing"
        return _run_pipeline(run)


def _run_pipeline(run: ImportRun):
    """Parse, interpret, and validate one run inside tenant scope."""
    try:
        if _parse_step(run):
            return "needs_input"
        _interpret_step(run)
        _validate_step(run)
    except RunCancelled:
        # The cancel action already audited; completed chunks keep their
        # honest spend, and no further calls are made.
        return "cancelled"
    except Exception as exc:
        run.refresh_from_db()
        if run.status not in TERMINAL_STATUSES:
            transition_run(run, RunStatus.FAILED)
            run.error = str(exc)[:2000]
            run.save(update_fields=["error", "updated_at"])
        record_audit(run.organization_id, None, "run_failed", "run", run.id)
        return "failed"
    return "ok"


def _parse_step(run: ImportRun) -> bool:
    """Parse the stored file. Returns True when mapping questions were asked
    and the pipeline must pause for the reviewer."""
    from django.conf import settings

    from common.storage import LocalFileAdapter
    from pipeline.parsing import (
        MappingRequired,
        UnsupportedFile,
        check_upload_safety,
        parse_upload,
    )

    adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
    try:
        content = adapter.read_bytes(run.source_file.storage_key)
    except FileNotFoundError as exc:
        raise UnsupportedFile("Stored upload is missing.") from exc
    check_upload_safety(
        content,
        run.source_file.original_name,
        max_bytes=settings.OFFERFLOW_UPLOAD_MAX_BYTES,
        max_uncompressed_bytes=settings.OFFERFLOW_MAX_UNCOMPRESSED_BYTES,
    )
    from pipeline.previews import _with_currency_default

    stored = run.mapping or {}
    confirmed = stored.get("confirmed") or {}
    constants = stored.get("constants") or {}
    ignored = stored.get("ignored") or []
    effective = dict(stored.get("detected") or {})
    effective.update(confirmed)
    merged, labels, applied = _with_currency_default(
        effective, constants, run.organization.default_currency, ignored
    )
    free_text = stored.get("free_text") is True
    if free_text:
        return _parse_message_rows(
            run, stored, content, constants, dict(merged)
        )
    try:
        sheet = parse_upload(
            content,
            run.source_file.original_name,
            max_rows=settings.OFFERFLOW_MAX_ROWS,
            max_cols=settings.OFFERFLOW_MAX_COLS,
            header_scan_rows=settings.OFFERFLOW_HEADER_SCAN_ROWS,
            column_overrides=confirmed or None,
            column_constants=merged or None,
            ignored_columns=ignored or None,
            constant_sources=labels or None,
            message_fallback=False,
            sheet_name=stored.get("sheet"),
        )
    except MappingRequired as exc:
        _ask_mapping(run, exc)
        return True
    for field in list(applied):
        # A real file column wins over the organization default; only
        # defaults that actually filled a gap are recorded as applied.
        if field in sheet.column_map:
            del applied[field]
    run.mapping = {
        "headers": sheet.headers,
        "detected": sheet.column_map,
        "confirmed": confirmed,
        "constants": constants,
        "ignored": ignored,
        "applied_defaults": applied,
        "sheet": sheet.sheet,
        "sheets_omitted": stored.get("sheets_omitted", []),
    }
    if free_text:
        run.mapping["free_text"] = True
    run.save(update_fields=["mapping", "updated_at"])
    SourceRow.objects.filter(run=run).delete()
    SourceRow.objects.bulk_create(
        [
            SourceRow(
                organization_id=run.organization_id,
                run=run,
                index=row.index,
                sheet=row.provenance.get("sheet", ""),
                data=row.data,
                provenance=row.provenance,
            )
            for row in sheet.rows
        ]
    )
    transition_run(run, RunStatus.INTERPRETATION)
    return False


def _parse_message_rows(run, stored: dict, content: bytes, constants: dict,
                        merged: dict) -> bool:
    """Build message-lane source rows from the confirmed draft revision.

    Records, joins, section values, and terminal derivation come from
    the same derive_record_cells the preview rendered, and the derived
    record set must equal the snapshot confirm stored: the worker
    imports exactly what the user reviewed, never a fresh independent
    extraction. Direct uploads without a preview confirm derive with no
    rules, so every non-blank line becomes one candidate record.
    """
    from pipeline.draft import derive_record_cells, terminal_scope_lines
    from pipeline.parsing import MESSAGE_COLUMN

    lines = content.decode("utf-8", errors="replace").split("\n")
    rules = stored.get("draft_rules") or []
    try:
        derived = derive_record_cells(lines, rules)
    except KeyError as exc:
        raise ValueError(
            "Confirmed draft rules are unreadable; remap to continue."
        ) from exc
    snapshot = stored.get("record_lines")
    if snapshot is not None:
        current = [
            {"id": record_id, "lines": info["lines"]}
            for record_id, info in derived.items()
        ]
        if current != snapshot:
            raise ValueError(
                "Confirmed draft does not match the stored source; refusing "
                "to import. Remap to continue."
            )
    covered = terminal_scope_lines(lines, rules)
    explicit = set((constants or {}))
    sheet_name = run.source_file.original_name if run.source_file else ""
    rows = []
    for record_id, info in derived.items():
        first = info["lines"][0]
        data = {"message": "\n".join(lines[n - 1] for n in info["lines"])}
        data.update(merged)
        for name, value in info["cells"].items():
            # Explicit confirm-time values win; confirmed draft values
            # win over the organization currency default.
            if name not in explicit:
                data[name] = value
        provenance = {
            "sheet": sheet_name,
            "row": first,
            "record": record_id,
            "lines": info["lines"],
        }
        if info["terminal"] is not None:
            provenance["terminal"] = info["terminal"]
        if first in covered:
            provenance["terminal_scope"] = True
        rows.append(
            SourceRow(
                organization_id=run.organization_id,
                run=run,
                index=first,
                sheet=sheet_name,
                data=data,
                provenance=provenance,
            )
        )
    applied = {
        key: value
        for key, value in (stored.get("applied_defaults") or {}).items()
    }
    if not applied and merged.keys() - explicit:
        applied = {
            key: merged[key] for key in merged.keys() - explicit
        }
    if applied.get("currency") and all(
        "currency" in info["cells"] for info in derived.values()
    ):
        # A confirmed rule supplied every currency; the org default
        # filled nothing, so it must not be reported as applied.
        del applied["currency"]
    run.mapping = {
        "headers": stored.get("headers") or [MESSAGE_COLUMN],
        "detected": stored.get("detected") or {MESSAGE_COLUMN: 0},
        "confirmed": stored.get("confirmed") or {},
        "constants": constants,
        "ignored": stored.get("ignored") or [],
        "applied_defaults": applied,
        "sheet": stored.get("sheet"),
        "sheets_omitted": stored.get("sheets_omitted", []),
        "free_text": True,
        "draft_revision": stored.get("draft_revision"),
        "draft_rules": rules,
        "record_lines": snapshot
        if snapshot is not None
        else [
            {"id": record_id, "lines": info["lines"]}
            for record_id, info in derived.items()
        ],
        "excluded_lines": stored.get("excluded_lines") or [],
        "document_policy": stored.get("document_policy", "stock"),
    }
    run.save(update_fields=["mapping", "updated_at"])
    SourceRow.objects.filter(run=run).delete()
    SourceRow.objects.bulk_create(rows)
    transition_run(run, RunStatus.INTERPRETATION)
    return False


# Pasted lines quoted back to the model as shared interpretation context,
# so header lines (supplier name, date) inform every product row. Long
# pastes split into segments on line boundaries; every segment inherits
# the document head and every row belongs to exactly one segment. The
# coverage record says what the model saw. Context is auxiliary — rows
# always travel as data — but when even head + one segment overflows the
# budget, oldest non-head lines drop first and coverage.partial turns
# True instead of silently claiming a complete read.
MESSAGE_CONTEXT_CHARS = 2000
MESSAGE_HEAD_LINES = 5


def _message_segments(rows) -> tuple[list, dict]:
    """Split message rows into headed context segments with coverage.

    Returns (segments, coverage). Each segment holds its rows plus the
    context string to send alongside them. Coverage records segment
    counts and whether any context line had to be dropped (partial).
    """
    rows = list(rows)
    texts = {}
    for row in rows:
        raw = (row.data or {}).get("message")
        texts[id(row)] = (
            raw.strip() if isinstance(raw, str) and raw.strip() else ""
        )
    head_rows = [row for row in rows if texts[id(row)]][:MESSAGE_HEAD_LINES]
    head_ids = {id(row) for row in head_rows}
    head_chars = sum(len(texts[id(row)]) + 1 for row in head_rows)
    pack_budget = MESSAGE_CONTEXT_CHARS - head_chars
    if pack_budget < MESSAGE_CONTEXT_CHARS // 4:
        pack_budget = MESSAGE_CONTEXT_CHARS // 4
    segments: list = []
    current: list = []
    current_chars = 0
    for row in rows:
        size = len(texts[id(row)]) + 1
        if current and current_chars + size > pack_budget:
            segments.append(current)
            current = []
            current_chars = 0
        current.append(row)
        current_chars += size
    if current or not segments:
        segments.append(current)
    built = []
    dropped_lines = 0
    for seg in segments:
        picked = [(row, texts[id(row)]) for row in head_rows]
        picked += [
            (row, texts[id(row)])
            for row in seg
            if texts[id(row)] and id(row) not in head_ids
        ]
        initial = len(picked)
        while picked and len("\n".join(text for _, text in picked)) > MESSAGE_CONTEXT_CHARS:
            drop_at = next(
                (
                    pos
                    for pos, (row, _text) in enumerate(picked)
                    if id(row) not in head_ids
                ),
                None,
            )
            if drop_at is None:
                drop_at = len(picked) - 1
            picked.pop(drop_at)
        dropped_here = initial - len(picked)
        dropped_lines += dropped_here
        built.append(
            {
                "rows": seg,
                "context": "\n".join(text for _, text in picked),
                "dropped_lines": dropped_here,
            }
        )
    coverage = {
        "strategy": "segmented",
        "segments": len(built),
        "total_rows": len(rows),
        "message_rows": sum(1 for row in rows if texts[id(row)]),
        "head_lines": len(head_rows),
        "partial": dropped_lines > 0,
        "dropped_lines": dropped_lines,
    }
    return built, coverage


def _is_message_row(row) -> bool:
    text = (row.data or {}).get("message")
    return isinstance(text, str) and bool(text.strip())


def _constrain_proposals(interpreted: dict, rows, *, allowed: set,
                         has_marking) -> None:
    """Enforce confirmed numeric roles on model proposals, in place.

    Unknown proposal keys are dropped and listed under "_unsupported".
    On terminal-scoped lines a proposed stock quantity survives only
    with an explicit stock marking on the line; anything else is popped
    into "_dropped" with its reason, leaving the gap for a human. Both
    markers travel into offer.interpreted, so the run detail shows what
    the model tried and why the application refused it.
    """
    by_index = {row.index: row for row in rows}
    for index, proposal in interpreted.items():
        if not isinstance(proposal, dict):
            continue
        unsupported = sorted(
            key
            for key in proposal
            if not key.startswith("_") and key not in allowed
        )
        for key in unsupported:
            del proposal[key]
        if unsupported:
            proposal["_unsupported"] = unsupported
        row = by_index.get(index)
        if row is None or "quantity" not in proposal:
            continue
        if not (row.provenance or {}).get("terminal_scope"):
            continue
        text = (row.data or {}).get("message") or ""
        if has_marking(text):
            continue
        dropped = proposal.setdefault("_dropped", {})
        dropped["quantity"] = {
            "value": proposal.pop("quantity"),
            "reason": (
                "terminal_price rule: the line carries no stock marking, "
                "so no number on it may become stock."
            ),
        }


def _interpret_step(run: ImportRun) -> None:
    from common.meta import FIELDS as MODEL_FIELDS
    from common.meta import ProviderError as MetaProviderError
    from pipeline.normalize import has_quantity_marking

    policy = (run.mapping or {}).get("document_policy", "stock")
    rows = list(run.source_rows.order_by("index"))
    # Local-first: the model copies the same cells the localizer reads, so
    # rows that normalize cleanly never need a paid call. Only gap rows go
    # to the provider; fully clean files import with zero model spend.
    local: dict = {}
    gap_rows = []
    for row in rows:
        cells = dict(row.data or {})
        fields = normalize_cells(cells)
        status, gaps, reason = compute_offer_status(
            fields, document_policy=policy
        )
        local[row.index] = (cells, fields, status, gaps, reason)
        if gaps:
            gap_rows.append(row)
    interpreted = None
    non_product: set = set()
    coverage = None
    if not gap_rows:
        provider_name, simulated = "local", True
    else:
        try:
            provider = get_provider(organization_id=run.organization_id)
        except ProviderNotConfigured as exc:
            from pipeline.parsing import UnsupportedFile

            raise UnsupportedFile(f"Provider refused: {exc}") from exc
        provider_name, simulated = provider.name, provider.is_simulated
        run.provider_name = provider_name
        run.simulated = simulated
        run.save(update_fields=["provider_name", "simulated", "updated_at"])
        payloads = []
        if (run.mapping or {}).get("free_text") is True:
            segments, coverage = _message_segments(rows)
            stored = run.mapping or {}
            stored["context_coverage"] = coverage
            run.mapping = stored
            run.save(update_fields=["mapping", "updated_at"])
            owner = {
                id(row): pos for pos, seg in enumerate(segments) for row in seg["rows"]
            }
            grouped: list = [[] for _ in segments]
            for row in gap_rows:
                grouped[owner[id(row)]].append(row)
            for pos, seg_rows in enumerate(grouped):
                if not seg_rows:
                    continue
                payloads.append(
                    {
                        "rows": [
                            {"index": row.index, "data": row.data}
                            for row in seg_rows
                        ],
                        "context": segments[pos]["context"],
                    }
                )
        else:
            payloads.append(
                {
                    "rows": [
                        {"index": row.index, "data": row.data}
                        for row in gap_rows
                    ]
                }
            )

        provider_model = getattr(getattr(provider, "_config", None), "model", "")
        if not isinstance(provider_model, str):
            provider_model = ""

        def _note_chunk(position, total, prompt_tokens, completion_tokens):
            run.refresh_from_db(fields=["status"])
            cancelled = run.status == RunStatus.CANCELLED
            record_usage(
                run.organization_id,
                run,
                "provider_call",
                prompt_tokens + completion_tokens,
                {
                    "provider": provider.name,
                    "model": provider_model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "chunk": f"{position}/{total}",
                },
            )
            stored = run.mapping or {}
            stored["ai_progress"] = {"done": position, "total": total}
            run.mapping = stored
            run.save(update_fields=["mapping", "updated_at"])
            if cancelled:
                raise RunCancelled(f"Run {run.id} was cancelled.")

        any_real = False
        merged: dict = {}
        excluded: set = set()
        for payload in payloads:
            result = provider.interpret_table(payload, on_chunk=_note_chunk)
            if result.get("simulated", True):
                continue
            any_real = True
            for item in result.get("rows", []):
                merged[item["index"]] = item.get("fields", {})
                if item.get("is_product") is False:
                    excluded.add(item["index"])
        if any_real:
            interpreted = merged
            non_product = excluded
            for row in gap_rows:
                if row.index not in interpreted:
                    raise MetaProviderError(
                        f"Model omitted row {row.index}; refusing partial use."
                    )
            _constrain_proposals(
                interpreted, rows, allowed=set(MODEL_FIELDS),
                has_marking=has_quantity_marking,
            )
            # Spend and progress were recorded live per chunk via the
            # callback, so even a later-failing run keeps its honest
            # partial accounting.
    if not gap_rows:
        run.provider_name = provider_name
        run.simulated = simulated
        run.save(update_fields=["provider_name", "simulated", "updated_at"])
    record_usage(
        run.organization_id,
        run,
        "interpretation",
        len(rows),
        {
            "provider": provider_name,
            "simulated": simulated,
            "model_rows": len(gap_rows),
            "context_coverage": coverage or {},
        },
    )
    NormalizedOffer.objects.filter(run=run).delete()
    # File-wide mapping answers survive reprocessing as decided history;
    # only per-offer questions are rebuilt for the new columns.
    ReviewRequest.objects.filter(run=run, offer__isnull=False).delete()
    for row in rows:
        if interpreted is not None and row.index in interpreted:
            cells = merge_interpreted_cells(
                dict(row.data), interpreted[row.index],
                document_policy=policy,
            )
            fields = normalize_cells(cells)
            status, gaps, reason = compute_offer_status(
                fields, document_policy=policy
            )
            scoped = bool((row.provenance or {}).get("terminal_scope"))
            if row.index in non_product and _is_message_row(row) and not scoped:
                # Header, title, date, or note line: set aside visibly as
                # an excluded offer, never silently dropped. Spreadsheet
                # rows ignore the flag — mapped cells are products — and
                # so do terminal-scoped lines, whose product standing the
                # user already confirmed with a terminal-price rule.
                status = OfferStatus.EXCLUDED
                gaps = []
                reason = "Line carries no product offer (header or note)."
        else:
            cells, fields, status, gaps, reason = local[row.index]
        offer = NormalizedOffer.objects.create(
            organization_id=run.organization_id,
            run=run,
            source_row=row,
            supplier=fields["supplier"],
            brand=fields["brand"],
            model_name=fields["model_name"],
            capacity_gb=fields["capacity_gb"],
            color=fields["color"],
            grade=fields["grade"],
            grade_raw=fields["grade_raw"],
            lock_state=fields["lock_state"],
            quantity=fields["quantity"],
            unit_price=fields["unit_price"],
            currency=fields["currency"],
            conditions=fields["conditions"],
            status=status,
            exclusion_reason=reason,
            provenance={"sheet": row.sheet, "row": row.index, "source_row": row.id},
            interpreted=(
                dict(interpreted[row.index])
                if interpreted is not None and row.index in interpreted
                else None
            ),
        )
        for gap in gaps:
            ReviewRequest.objects.create(
                organization_id=run.organization_id,
                run=run,
                offer=offer,
                kind=gap["kind"],
                field=gap["field"],
                question=gap["question"],
                evidence={"provenance": offer.provenance, "raw": cells},
                choices=gap.get("choices", []),
            )
    from pipeline.suppliers import ensure_suppliers

    ensure_suppliers(
        run.organization_id,
        sorted(
            {
                offer.supplier
                for offer in NormalizedOffer.objects.filter(run=run).only(
                    "supplier"
                )
            }
        ),
    )
    transition_run(run, RunStatus.VALIDATION)


def _validate_step(run: ImportRun) -> None:
    passed, _results = run_validations(run)
    open_count = run.reviews.filter(status=ReviewStatus.OPEN).count()
    record_usage(
        run.organization_id,
        run,
        "run_processed",
        1,
        {"passed": passed, "open_reviews": open_count},
    )
    if open_count:
        transition_run(run, RunStatus.NEEDS_INPUT)
        record_audit(run.organization_id, None, "run_needs_input", "run", run.id)
    elif passed:
        transition_run(run, RunStatus.READY_FOR_REVIEW)
        record_audit(run.organization_id, None, "run_ready", "run", run.id)
    else:
        run.error = "Validation failed with no open review requests."
        run.save(update_fields=["error", "updated_at"])
        transition_run(run, RunStatus.FAILED)
        record_audit(run.organization_id, None, "run_failed", "run", run.id)
