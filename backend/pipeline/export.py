"""Export generation: exact validated bytes with source references."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from openpyxl import Workbook

from common.storage import LocalFileAdapter
from pipeline.models import ArtifactFormat, OfferStatus, RunStatus, transition_run

EXPORT_COLUMNS = [
    "supplier",
    "brand",
    "model",
    "capacity_gb",
    "color",
    "grade",
    "lock_state",
    "quantity",
    "unit_price",
    "currency",
    "conditions",
    "source_sheet",
    "source_row",
]


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def normalized_digest(run) -> str:
    """Fingerprint the exact releasable set approval and export bind to."""
    offers = run.offers.filter(status=OfferStatus.OK).order_by("id")
    payload = []
    for offer in offers:
        provenance = offer.provenance or {}
        payload.append(
            {
                "supplier": offer.supplier,
                "brand": offer.brand,
                "model": offer.model_name,
                "capacity_gb": offer.capacity_gb,
                "color": offer.color,
                "grade": offer.grade,
                "lock_state": offer.lock_state,
                "quantity": offer.quantity,
                "unit_price": _json_safe(offer.unit_price),
                "currency": offer.currency,
                "conditions": offer.conditions,
                "source_sheet": provenance.get("sheet"),
                "source_row": provenance.get("row"),
            }
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_cell(value):
    """Guard against spreadsheet formula injection in text cells.

    Strings starting with a trigger character get a leading apostrophe, which
    spreadsheets render invisibly as a text marker. Numbers pass through
    untouched so intended numeric cells stay numeric.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _export_number(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return float(value)
    return value


def _offer_row(offer) -> list:
    provenance = offer.provenance or {}
    return [
        sanitize_cell(offer.supplier),
        sanitize_cell(offer.brand),
        sanitize_cell(offer.model_name),
        _export_number(offer.capacity_gb),
        sanitize_cell(offer.color),
        sanitize_cell(offer.grade),
        offer.lock_state,
        _export_number(offer.quantity),
        _export_number(offer.unit_price),
        offer.currency,
        sanitize_cell(offer.conditions),
        provenance.get("sheet", ""),
        provenance.get("row", ""),
    ]


def build_xlsx_bytes(run, offers: list) -> bytes:
    """Build the offers, excluded, and meta sheets."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "offers"
    sheet.append(EXPORT_COLUMNS)
    for offer in offers:
        sheet.append(_offer_row(offer))
    excluded = workbook.create_sheet("excluded")
    excluded.append(["source_sheet", "source_row", "supplier", "model", "reason"])
    for offer in run.offers.filter(status=OfferStatus.EXCLUDED).order_by("id"):
        provenance = offer.provenance or {}
        excluded.append(
            [
                provenance.get("sheet", ""),
                provenance.get("row", ""),
                sanitize_cell(offer.supplier),
                sanitize_cell(offer.model_name),
                sanitize_cell(offer.exclusion_reason),
            ]
        )
    meta = workbook.create_sheet("meta")
    meta.append(["key", "value"])
    meta.append(["source_file", run.source_file.original_name])
    meta.append(["source_sha256", run.source_file.sha256])
    meta.append(["rule_version", run.rule_version.version])
    meta.append(["provider", run.provider_name])
    meta.append(["simulated", str(run.simulated)])
    meta.append(["normalized_digest", normalized_digest(run)])
    meta.append(["approved_by", str(run.approved_by) if run.approved_by_id else ""])
    meta.append(["approved_at", run.approved_at.isoformat() if run.approved_at else ""])
    meta.append(
        ["generated_at", datetime.now(dt_timezone.utc).isoformat()]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_csv_bytes(run, offers: list) -> bytes:
    """Build the releasable-offers CSV (UTF-8 with BOM for spreadsheets)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for offer in offers:
        writer.writerow(_offer_row(offer))
    return "\ufeff".encode("utf-8") + buffer.getvalue().encode("utf-8")


def create_export(*, run, user, format: str):
    """Generate and freeze one export artifact after rechecking approval."""
    from pipeline.jobs import record_audit, record_usage
    from pipeline.models import ExportArtifact

    if format not in ArtifactFormat.values:
        raise ValidationError(f"Unknown export format {format!r}.")
    if run.status not in (RunStatus.APPROVED, RunStatus.COMPLETED):
        raise ValidationError("Only approved runs can be exported.")
    if normalized_digest(run) != (run.approved_normalized_digest or ""):
        raise ValidationError("Approval is stale; re-approval is required.")
    if run.source_file.sha256 != (run.approved_source_sha or ""):
        raise ValidationError("Source changed since approval; re-approval is required.")
    offers = list(run.offers.filter(status=OfferStatus.OK).order_by("id"))
    if format == ArtifactFormat.XLSX:
        content = build_xlsx_bytes(run, offers)
    else:
        content = build_csv_bytes(run, offers)
    adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
    key = adapter.build_key(
        str(run.organization_id), "artifacts", f"run-{run.id}-{uuid.uuid4().hex}.{format}"
    )
    adapter.save_bytes(key, content)
    artifact = ExportArtifact.objects.create(
        organization_id=run.organization_id,
        run=run,
        format=format,
        storage_key=key,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        source_sha=run.source_file.sha256,
        rule_version=run.rule_version.version,
        normalized_digest=run.approved_normalized_digest,
        created_by=user,
    )
    transition_run(run, RunStatus.EXPORTING)
    transition_run(run, RunStatus.COMPLETED)
    record_usage(run.organization_id, run, "export_created", 1, {"format": format})
    record_audit(
        run.organization_id, user, "export_created", "artifact", artifact.id,
        {"format": format},
    )
    return artifact
