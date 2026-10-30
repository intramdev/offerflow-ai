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
from pipeline.normalize import (
    compute_offer_status,
    normalize_cells,
    parse_capacity,
    parse_currency,
    parse_price,
    parse_quantity,
)
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

CONSTANT_PARSERS = {
    "price": (parse_price, "a valid price"),
    "quantity": (parse_quantity, "a valid quantity"),
    "capacity": (parse_capacity, "a valid capacity"),
    "currency": (parse_currency, "a three-letter currency code"),
}


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


def remember_mapping(
    *,
    organization,
    headers: list,
    mapping: dict,
    supplier_hint="",
    constants=None,
    ignored=None,
):
    """Store a confirmed header-signature to mapping reuse record."""
    signature = header_signature(headers)
    clean_constants = {str(k): v for k, v in (constants or {}).items()}
    clean_ignored = [str(name) for name in (ignored or [])]
    stored_mapping = {
        str(k): v for k, v in mapping.items() if k not in clean_ignored
    }
    memory, created = ColumnMappingMemory.objects.get_or_create(
        organization=organization,
        signature=signature,
        defaults={
            "headers": list(headers),
            "mapping": stored_mapping,
            "constants": clean_constants,
            "ignored": clean_ignored,
            "supplier_hint": (supplier_hint or "")[:255],
        },
    )
    if not created:
        from django.db.models import F

        memory.headers = list(headers)
        memory.mapping = stored_mapping
        memory.constants = clean_constants
        memory.ignored = clean_ignored
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
    constants = memory.constants or {}
    if not isinstance(constants, dict):
        return False
    if any(name not in HEADER_TOKENS for name in constants):
        return False
    ignored = memory.ignored or []
    if not isinstance(ignored, list):
        return False
    if any(name not in HEADER_TOKENS for name in ignored):
        return False
    if set(mapping) & set(constants):
        return False
    covered = set(mapping) | set(constants)
    return all(name in covered for name in REQUIRED_COLUMNS)


def _clean_constant(canonical: str, value):
    """Validate one set value; numbers on numeric fields must parse."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError(f"Value for {canonical!r} must be text or a number.")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValidationError(f"Value for {canonical!r} must be a finite number.")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValidationError(f"Value for {canonical!r} must not be blank.")
        if len(value) > 255:
            raise ValidationError(f"Value for {canonical!r} is too long.")
    parser_spec = CONSTANT_PARSERS.get(canonical)
    if parser_spec is not None:
        parser, expectation = parser_spec
        if parser(value) is None:
            raise ValidationError(
                f"Value for {canonical!r} must be {expectation}."
            )
    return value


def _clean_positions(
    mapping,
    headers: list,
    detected: dict,
    *,
    require_complete: bool,
    constants=None,
    ignored=None,
):
    """Validate positions, set values, and ignores.

    Returns (submitted, effective, clean_constants, clean_ignored): the
    positions the user set, the merged position map, and the validated
    extras. A required column is satisfied by a column or a set value.
    """
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
    if ignored is None:
        ignored = []
    if not isinstance(ignored, list):
        raise ValidationError("Ignored fields must be a list.")
    clean_ignored = []
    for canonical in ignored:
        if canonical not in HEADER_TOKENS:
            raise ValidationError(f"Unknown column {canonical!r}.")
        if canonical in cleaned:
            raise ValidationError(
                f"Column {canonical!r} is both mapped and ignored."
            )
        if canonical not in clean_ignored:
            clean_ignored.append(canonical)
    if constants is None:
        constants = {}
    if not isinstance(constants, dict):
        raise ValidationError("Set values must be an object.")
    clean_constants = {}
    for canonical, value in constants.items():
        if canonical not in HEADER_TOKENS:
            raise ValidationError(f"Unknown column {canonical!r}.")
        if canonical in effective and canonical not in clean_ignored:
            raise ValidationError(
                f"Column {canonical!r} has both a column and a set value."
            )
        clean_constants[canonical] = _clean_constant(canonical, value)
    if require_complete:
        missing = [
            name
            for name in REQUIRED_COLUMNS
            if name not in effective and name not in clean_constants
        ]
        if missing:
            raise ValidationError(
                f"Mapping still misses required columns: {', '.join(missing)}."
            )
    return cleaned, effective, clean_constants, clean_ignored


def _with_currency_default(mapping: dict, constants: dict, default_currency: str,
                           ignored=None):
    """Fill an unspecified currency from the organization default.

    A mapped column or a set value counts as specified; an ignored field
    does not, so the default still applies. Returns (constants, sources,
    applied): the merged constants, per-value source labels, and the
    defaults that were applied for the UI to show.
    """
    merged = dict(constants or {})
    labels = {}
    applied = {}
    default = (default_currency or "").strip().upper()
    positions = set(mapping or {}) - set(ignored or [])
    if default and "currency" not in positions and "currency" not in merged:
        merged["currency"] = default
        labels["currency"] = "default"
        applied["currency"] = default
    return merged, labels, applied


def _render_samples(content: bytes, filename: str, mapping: dict,
                    constants=None, ignored=None, default_currency="") -> dict:
    limits = _limits()
    merged, labels, applied = _with_currency_default(
        mapping, constants, default_currency, ignored
    )
    try:
        sheet = parse_upload(
            content,
            filename,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
            column_overrides=mapping or None,
            column_constants=merged or None,
            ignored_columns=ignored or None,
            constant_sources=labels or None,
        )
    except MappingRequired as exc:
        return {
            "complete": False,
            "missing": exc.missing,
            "detected": exc.column_map,
            "sources": exc.sources,
            "applied_defaults": applied,
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
        "applied_defaults": applied,
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
    proposed_constants: dict = {}
    proposed_ignored: list = []
    memory_info = None
    if memory is not None and _memory_usable(memory, headers):
        proposed = {str(k): v for k, v in memory.mapping.items()}
        proposed_constants = dict(memory.constants or {})
        proposed_ignored = list(memory.ignored or [])
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
    samples = _render_samples(
        content,
        original_name,
        proposed,
        proposed_constants,
        proposed_ignored,
        organization.default_currency,
    )
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
        "proposed_constants": proposed_constants,
        "proposed_ignored": proposed_ignored,
        "samples": samples,
    }


def render_preview(*, key: str, user, mapping: dict,
                   constants=None, ignored=None) -> dict:
    """Re-render sample rows for an adjusted mapping."""
    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    _submitted, effective, clean_constants, clean_ignored = _clean_positions(
        mapping,
        meta["headers"],
        meta["detected"],
        require_complete=False,
        constants=constants,
        ignored=ignored,
    )
    try:
        content = _adapter().read_bytes(f"previews/{key}.bin")
    except FileNotFoundError:
        raise ValidationError("Unknown or expired preview.") from None
    return {
        "mapping": effective,
        "constants": clean_constants,
        "ignored": clean_ignored,
        "samples": _render_samples(
            content,
            meta["filename"],
            effective,
            clean_constants,
            clean_ignored,
            organization.default_currency,
        ),
    }


def _supplier_hint(content: bytes, filename: str, mapping: dict, constants: dict) -> str:
    if "supplier" in (constants or {}):
        return str(constants["supplier"]).strip()[:255]
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


def confirm_preview(*, key: str, user, mapping: dict,
                    constants=None, ignored=None):
    """Validate a full mapping and start the import run."""
    from pipeline.uploads import store_upload_run

    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    submitted, effective, clean_constants, clean_ignored = _clean_positions(
        mapping,
        meta["headers"],
        meta["detected"],
        require_complete=True,
        constants=constants,
        ignored=ignored,
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
            "constants": clean_constants,
            "ignored": clean_ignored,
        },
    )
    remember_mapping(
        organization=organization,
        headers=meta["headers"],
        mapping=effective,
        constants=clean_constants,
        ignored=clean_ignored,
        supplier_hint=_supplier_hint(
            content, meta["filename"], effective, clean_constants
        ),
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
