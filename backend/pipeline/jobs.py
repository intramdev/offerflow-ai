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


def cancel_run(*, run: ImportRun, user) -> ImportRun:
    """Cancel a non-terminal run. A task already in flight may finish its
    current step afterwards, but terminal status wins and the run stays
    cancelled and unreleasable."""
    from django.core.exceptions import ValidationError

    if run.status in TERMINAL_STATUSES:
        raise ValidationError("That run is already finished.")
    transition_run(run, RunStatus.CANCELLED)
    record_audit(run.organization_id, user, "run_cancelled", "run", run.id)
    return run


def _ask_mapping(run: ImportRun, exc) -> None:
    """Store the mapping report and ask one choice question per missing column."""
    from pipeline.review import mapping_choice_label

    run.mapping = {
        "headers": exc.headers,
        "detected": exc.column_map,
        "confirmed": {},
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


def reprocess_run(run: ImportRun) -> str:
    """Restart a waiting run after its mapping changed. Returns the new status."""
    run.error = ""
    run.save(update_fields=["error", "updated_at"])
    transition_run(run, RunStatus.PARSING)
    try:
        if _parse_step(run):
            return RunStatus.NEEDS_INPUT
        _interpret_step(run)
        _validate_step(run)
    except Exception as exc:
        run.refresh_from_db()
        if run.status not in TERMINAL_STATUSES:
            transition_run(run, RunStatus.FAILED)
            run.error = str(exc)[:2000]
            run.save(update_fields=["error", "updated_at"])
        record_audit(run.organization_id, None, "run_failed", "run", run.id)
        return RunStatus.FAILED
    run.refresh_from_db()
    return run.status


def remap_run(*, run: ImportRun, user, mapping: dict) -> ImportRun:
    """Replace a run's column mapping and reprocess the same file."""
    from django.core.exceptions import ValidationError

    from organizations.context import set_tenant_context
    from organizations.services import require_organization_role
    from pipeline.previews import _clean_positions, remember_mapping

    if run.status not in (
        RunStatus.NEEDS_INPUT,
        RunStatus.READY_FOR_REVIEW,
        RunStatus.FAILED,
    ):
        raise ValidationError(
            "Only waiting, reviewable, or failed runs can be remapped."
        )
    require_organization_role(user, run.organization_id)
    set_tenant_context(run.organization_id, user.id)
    stored = run.mapping or {}
    headers = stored.get("headers") or []
    if not headers:
        raise ValidationError("That run has no detected headers to remap.")
    submitted, effective = _clean_positions(
        mapping, headers, stored.get("detected") or {}, require_complete=True
    )
    stored["confirmed"] = submitted
    run.mapping = stored
    run.save(update_fields=["mapping", "updated_at"])
    ReviewRequest.objects.filter(
        run=run, offer__isnull=True, status=ReviewStatus.OPEN
    ).delete()
    transition_run(run, RunStatus.UPLOADED)
    reprocess_run(run)
    remember_mapping(
        organization=run.organization, headers=headers, mapping=effective
    )
    record_audit(
        run.organization_id, user, "run_remapped", "run", run.id, {"mapping": submitted}
    )
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
    confirmed = (run.mapping or {}).get("confirmed") or {}
    try:
        sheet = parse_upload(
            content,
            run.source_file.original_name,
            max_rows=settings.OFFERFLOW_MAX_ROWS,
            max_cols=settings.OFFERFLOW_MAX_COLS,
            header_scan_rows=settings.OFFERFLOW_HEADER_SCAN_ROWS,
            column_overrides=confirmed or None,
        )
    except MappingRequired as exc:
        _ask_mapping(run, exc)
        return True
    run.mapping = {
        "headers": sheet.headers,
        "detected": sheet.column_map,
        "confirmed": confirmed,
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
    try:
        provider = get_provider(organization_id=run.organization_id)
    except ProviderNotConfigured as exc:
        from pipeline.parsing import UnsupportedFile

        raise UnsupportedFile(f"Provider refused: {exc}") from exc
    rows = list(run.source_rows.order_by("index"))
    payload = {"rows": [{"index": row.index, "data": row.data} for row in rows]}
    result = provider.interpret_table(payload)
    run.provider_name = provider.name
    run.simulated = provider.is_simulated
    run.save(update_fields=["provider_name", "simulated", "updated_at"])
    record_usage(
        run.organization_id,
        run,
        "interpretation",
        len(rows),
        {"provider": provider.name, "simulated": provider.is_simulated},
    )
    interpreted = None
    if not result.get("simulated", True):
        from common.meta import ProviderError as MetaProviderError

        interpreted = {
            item["index"]: item.get("fields", {}) for item in result.get("rows", [])
        }
        usage = result.get("usage", {})
        record_usage(
            run.organization_id,
            run,
            "provider_call",
            usage.get("total_tokens", 0),
            {
                "provider": result.get("provider"),
                "model": result.get("model"),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
        )
    NormalizedOffer.objects.filter(run=run).delete()
    # File-wide mapping answers survive reprocessing as decided history;
    # only per-offer questions are rebuilt for the new columns.
    ReviewRequest.objects.filter(run=run, offer__isnull=False).delete()
    for row in rows:
        cells = dict(row.data or {})
        if interpreted is not None:
            if row.index not in interpreted:
                raise MetaProviderError(
                    f"Model omitted row {row.index}; refusing partial use."
                )
            cells = interpreted[row.index]
        fields = normalize_cells(cells)
        status, gaps, reason = compute_offer_status(fields)
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
            interpreted=cells if interpreted is not None else None,
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
