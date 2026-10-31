"""Background execution: idempotent, transaction-aware, tenant-scoped."""

from __future__ import annotations

from celery import shared_task
from django.db import transaction

from common.provider import ProviderNotConfigured, get_provider
from organizations.context import reset_tenant_context, set_tenant_context
from pipeline.models import (
    AuditEvent,
    ImportRun,
    NormalizedOffer,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    RunStatus,
    SourceRow,
    TERMINAL_STATUSES,
    UsageEvent,
    transition_run,
)
from pipeline.normalize import compute_offer_status, normalize_cells
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


def record_usage(organization_id, run, kind, quantity=1, detail=None):
    """Append one usage event for later budget reconciliation."""
    return UsageEvent.objects.create(
        organization_id=organization_id,
        run=run,
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
    }
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
    remember_mapping(
        organization=run.organization,
        headers=headers,
        mapping=effective,
        constants=clean_constants,
        ignored=clean_ignored,
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


@shared_task(name="pipeline.process_import_run", bind=True, max_retries=0)
def process_import_run(self, run_id: int):
    """Process one run exactly once per request; redeliveries are no-ops."""
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
    set_tenant_context(run.organization_id, run.source_file.uploaded_by_id or 0)
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
    finally:
        reset_tenant_context()
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
    }
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


def _interpret_step(run: ImportRun) -> None:
    from common.meta import ProviderError as MetaProviderError

    rows = list(run.source_rows.order_by("index"))
    # Local-first: the model copies the same cells the localizer reads, so
    # rows that normalize cleanly never need a paid call. Only gap rows go
    # to the provider; fully clean files import with zero model spend.
    local: dict = {}
    gap_rows = []
    for row in rows:
        cells = dict(row.data or {})
        fields = normalize_cells(cells)
        status, gaps, reason = compute_offer_status(fields)
        local[row.index] = (cells, fields, status, gaps, reason)
        if gaps:
            gap_rows.append(row)
    interpreted = None
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
        payload = {
            "rows": [{"index": row.index, "data": row.data} for row in gap_rows]
        }

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

        result = provider.interpret_table(payload, on_chunk=_note_chunk)
        if not result.get("simulated", True):
            interpreted = {
                item["index"]: item.get("fields", {})
                for item in result.get("rows", [])
            }
            for row in gap_rows:
                if row.index not in interpreted:
                    raise MetaProviderError(
                        f"Model omitted row {row.index}; refusing partial use."
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
        },
    )
    NormalizedOffer.objects.filter(run=run).delete()
    # File-wide mapping answers survive reprocessing as decided history;
    # only per-offer questions are rebuilt for the new columns.
    ReviewRequest.objects.filter(run=run, offer__isnull=False).delete()
    for row in rows:
        if interpreted is not None and row.index in interpreted:
            cells = dict(interpreted[row.index])
            fields = normalize_cells(cells)
            status, gaps, reason = compute_offer_status(fields)
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
