"""Upload previews: propose a mapping, render sample rows, confirm to import.

Preview bytes live under an unguessable key with a JSON sidecar carrying
organization, filename, and detected headers. Nothing touches the database
until confirm; stale previews are garbage-collected piggyback on creation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.exceptions import ValidationError

from common.storage import LocalFileAdapter
from organizations.services import authorize_organization_access
from pipeline.models import ColumnMappingMemory, SourceFile
from pipeline.normalize import compute_offer_status, normalize_cells
from pipeline.parsing import (
    HEADER_TOKENS,
    REQUIRED_COLUMNS,
    MappingRequired,
    UnsupportedFile,
    check_upload_safety,
    column_letter,
    header_signature,
    parse_upload,
)

PREVIEW_TTL = timedelta(hours=24)
SAMPLE_ROWS = 5


def _adapter() -> LocalFileAdapter:
    return LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)


def _limits() -> dict:
    return {
        "max_bytes": settings.OFFERFLOW_UPLOAD_MAX_BYTES,
        "max_uncompressed_bytes": settings.OFFERFLOW_MAX_UNCOMPRESSED_BYTES,
        "max_rows": settings.OFFERFLOW_MAX_ROWS,
        "max_cols": settings.OFFERFLOW_MAX_COLS,
        "header_scan_rows": settings.OFFERFLOW_HEADER_SCAN_ROWS,
    }


def _safe(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    return value


def _header_entries(headers: list) -> list:
    return [
        {"position": index, "letter": column_letter(index), "header": text}
        for index, text in enumerate(headers)
    ]


def _gc_previews() -> None:
    from pathlib import Path

    root = Path(settings.OFFERFLOW_FILE_ROOT) / "previews"
    if not root.is_dir():
        return
    cutoff = datetime.now(timezone.utc) - PREVIEW_TTL
    adapter = _adapter()
    for meta_path in root.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text())
            created = datetime.fromisoformat(meta.get("created_at", ""))
        except (ValueError, OSError):
            continue
        if created < cutoff:
            key = meta_path.stem
            if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
                adapter.delete(f"previews/{key}.bin")
                adapter.delete(f"previews/{key}.json")


def _load_preview(key: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
        raise ValidationError("Unknown or expired preview.")
    try:
        return json.loads(_adapter().read_bytes(f"previews/{key}.json"))
    except (FileNotFoundError, ValueError):
        raise ValidationError("Unknown or expired preview.") from None


def remember_mapping(*, organization, headers: list, mapping: dict, supplier_hint=""):
    """Store a confirmed header-signature to mapping reuse record."""
    signature = header_signature(headers)
    memory, created = ColumnMappingMemory.objects.get_or_create(
        organization=organization,
        signature=signature,
        defaults={
            "headers": list(headers),
            "mapping": {str(k): v for k, v in mapping.items()},
            "supplier_hint": (supplier_hint or "")[:255],
        },
    )
    if not created:
        from django.db.models import F

        memory.headers = list(headers)
        memory.mapping = {str(k): v for k, v in mapping.items()}
        if supplier_hint:
            memory.supplier_hint = supplier_hint[:255]
        memory.use_count = F("use_count") + 1
        memory.save()
    return memory


def lookup_memory(*, organization, headers: list):
    """Return the remembered mapping for ordered headers, if any."""
    try:
        return ColumnMappingMemory.objects.get(
            organization=organization, signature=header_signature(headers)
        )
    except ColumnMappingMemory.DoesNotExist:
        return None


def _memory_usable(memory: ColumnMappingMemory, headers: list) -> bool:
    mapping = memory.mapping or {}
    positions = list(mapping.values())
    if not all(
        isinstance(p, int) and not isinstance(p, bool) and 0 <= p < len(headers)
        for p in positions
    ):
        return False
    if len(set(positions)) != len(positions):
        return False
    return all(name in mapping for name in REQUIRED_COLUMNS)


def _clean_positions(mapping, headers: list, detected: dict, *, require_complete: bool):
    """Validate submitted positions; merge over detected. Returns (submitted, effective)."""
    if not isinstance(mapping, dict):
        raise ValidationError("Mapping must be an object.")
    cleaned = {}
    for canonical, position in mapping.items():
        if canonical not in HEADER_TOKENS:
            raise ValidationError(f"Unknown column {canonical!r}.")
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValidationError(f"Column {canonical!r} needs a column position.")
        if position < 0 or position >= len(headers):
            raise ValidationError(f"Column {canonical!r} points outside the headers.")
        if not headers[position]:
            raise ValidationError(f"Column {canonical!r} points at an empty header.")
        cleaned[canonical] = position
    if len(set(cleaned.values())) != len(cleaned):
        raise ValidationError("Two fields point at the same column.")
    effective = dict(detected or {})
    effective.update(cleaned)
    if len(set(effective.values())) != len(effective):
        raise ValidationError("The mapping assigns one column twice.")
    if require_complete:
        missing = [name for name in REQUIRED_COLUMNS if name not in effective]
        if missing:
            raise ValidationError(
                f"Mapping still misses required columns: {', '.join(missing)}."
            )
    return cleaned, effective


def _render_samples(content: bytes, filename: str, mapping: dict) -> dict:
    limits = _limits()
    try:
        sheet = parse_upload(
            content,
            filename,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
            column_overrides=mapping or None,
        )
    except MappingRequired as exc:
        return {
            "complete": False,
            "missing": exc.missing,
            "detected": exc.column_map,
            "sources": exc.sources,
            "rows": [
                {"index": row.index, "cells": row.data}
                for row in exc.rows[:SAMPLE_ROWS]
            ],
        }
    rows = []
    for row in sheet.rows[:SAMPLE_ROWS]:
        fields = normalize_cells(dict(row.data))
        status, gaps, reason = compute_offer_status(fields)
        rows.append(
            {
                "index": row.index,
                "cells": row.data,
                "normalized": {key: _safe(value) for key, value in fields.items()},
                "status": status,
                "issues": [gap["question"] for gap in gaps]
                + ([reason] if reason else []),
            }
        )
    return {
        "complete": True,
        "missing": [],
        "detected": sheet.column_map,
        "sources": sheet.sources,
        "rows": rows,
    }


def _reject_duplicate(*, organization, user, original_name: str, digest: str) -> None:
    from pipeline.jobs import record_audit

    duplicate = (
        SourceFile.objects.filter(organization=organization, sha256=digest)
        .order_by("created_at")
        .first()
    )
    if duplicate is None:
        return
    record_audit(
        organization.id,
        user,
        "upload_duplicate_refused",
        "file",
        duplicate.id,
        {"name": original_name[:255], "sha256": digest},
    )
    raise ValidationError(
        f"This file is a duplicate of {duplicate.original_name!r}, "
        f"uploaded on {duplicate.created_at.date().isoformat()}."
    )


def create_preview(*, organization, user, uploaded_file) -> dict:
    """Store preview bytes and propose a mapping with sample rows."""
    from pipeline.jobs import record_audit

    original_name = getattr(uploaded_file, "name", "") or ""
    content = uploaded_file.read()
    if not content:
        raise ValidationError("Empty file.")
    limits = _limits()
    try:
        extension = check_upload_safety(
            content,
            original_name,
            max_bytes=limits["max_bytes"],
            max_uncompressed_bytes=limits["max_uncompressed_bytes"],
        )
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    digest = hashlib.sha256(content).hexdigest()
    _reject_duplicate(
        organization=organization, user=user, original_name=original_name, digest=digest
    )
    try:
        sheet = parse_upload(
            content,
            original_name,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
        )
        headers, detected, sources, missing = (
            sheet.headers,
            sheet.column_map,
            sheet.sources,
            [],
        )
    except MappingRequired as exc:
        headers, detected, sources, missing = (
            exc.headers,
            exc.column_map,
            exc.sources,
            exc.missing,
        )
    memory = lookup_memory(organization=organization, headers=headers)
    proposed = dict(detected)
    memory_info = None
    if memory is not None and _memory_usable(memory, headers):
        proposed = {str(k): v for k, v in memory.mapping.items()}
        memory_info = {
            "use_count": memory.use_count,
            "last_used_at": memory.last_used_at.isoformat(),
            "supplier_hint": memory.supplier_hint,
        }
    import secrets

    key = secrets.token_urlsafe(24)
    adapter = _adapter()
    adapter.save_bytes(f"previews/{key}.bin", content)
    meta = {
        "organization_id": organization.id,
        "user_id": user.id,
        "filename": original_name[:255],
        "sha256": digest,
        "extension": extension,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "headers": headers,
        "detected": detected,
        "sources": sources,
    }
    adapter.save_bytes(f"previews/{key}.json", json.dumps(meta).encode("utf-8"))
    _gc_previews()
    samples = _render_samples(content, original_name, proposed)
    record_audit(
        organization.id,
        user,
        "preview_created",
        "preview",
        key,
        {"filename": original_name[:255], "sha256": digest},
    )
    return {
        "key": key,
        "filename": original_name[:255],
        "extension": extension,
        "size_bytes": len(content),
        "sha256": digest,
        "headers": _header_entries(headers),
        "detected": detected,
        "sources": sources,
        "missing": missing,
        "memory": memory_info,
        "proposed": proposed,
        "samples": samples,
    }


def render_preview(*, key: str, user, mapping: dict) -> dict:
    """Re-render sample rows for an adjusted mapping."""
    meta = _load_preview(key)
    authorize_organization_access(user, meta["organization_id"])
    _submitted, effective = _clean_positions(
        mapping, meta["headers"], meta["detected"], require_complete=False
    )
    try:
        content = _adapter().read_bytes(f"previews/{key}.bin")
    except FileNotFoundError:
        raise ValidationError("Unknown or expired preview.") from None
    return {
        "mapping": effective,
        "samples": _render_samples(content, meta["filename"], effective),
    }


def _supplier_hint(content: bytes, filename: str, mapping: dict) -> str:
    limits = _limits()
    try:
        sheet = parse_upload(
            content,
            filename,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
            column_overrides=mapping or None,
        )
    except UnsupportedFile:
        return ""
    if not sheet.rows:
        return ""
    supplier = sheet.rows[0].data.get("supplier")
    return ("" if supplier is None else str(supplier).strip())[:255]


def confirm_preview(*, key: str, user, mapping: dict):
    """Validate a full mapping and start the import run."""
    from pipeline.uploads import store_upload_run

    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    submitted, effective = _clean_positions(
        mapping, meta["headers"], meta["detected"], require_complete=True
    )
    try:
        content = _adapter().read_bytes(f"previews/{key}.bin")
    except FileNotFoundError:
        raise ValidationError("Unknown or expired preview.") from None
    _reject_duplicate(
        organization=organization,
        user=user,
        original_name=meta["filename"],
        digest=meta["sha256"],
    )
    run = store_upload_run(
        organization=organization,
        user=user,
        content=content,
        original_name=meta["filename"],
        mapping={
            "headers": meta["headers"],
            "detected": meta["detected"],
            "confirmed": submitted,
        },
    )
    remember_mapping(
        organization=organization,
        headers=meta["headers"],
        mapping=effective,
        supplier_hint=_supplier_hint(content, meta["filename"], effective),
    )
    adapter = _adapter()
    adapter.delete(f"previews/{key}.bin")
    adapter.delete(f"previews/{key}.json")
    return run


def cancel_preview(*, key: str, user) -> None:
    """Drop preview bytes after a membership check."""
    meta = _load_preview(key)
    authorize_organization_access(user, meta["organization_id"])
    adapter = _adapter()
    adapter.delete(f"previews/{key}.bin")
    adapter.delete(f"previews/{key}.json")
