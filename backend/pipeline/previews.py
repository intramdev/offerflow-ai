"""Upload previews: propose a mapping, render sample rows, confirm to import.

Preview bytes live under an unguessable key with a JSON sidecar carrying
organization, filename, and detected headers. Nothing touches the database
until confirm; stale previews are garbage-collected piggyback on creation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError

logger = logging.getLogger(__name__)

from common.storage import LocalFileAdapter
from organizations.services import (
    authorize_organization_access,
    require_organization_role,
)
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
    MESSAGE_COLUMN,
    REQUIRED_COLUMNS,
    HeaderNotFound,
    MappingRequired,
    MessageCandidateFile,
    UnsupportedFile,
    UploadTooLarge,
    candidate_message_text,
    check_upload_extension,
    check_upload_safety,
    column_letter,
    delimited_extension,
    discover_sheets,
    header_signature,
    parse_delimited_sample,
    parse_upload,
    refuse_oversize,
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
    for orphan in root.glob("*.bin"):
        # A metadata write that failed mid-preview leaves bytes no
        # session can resume; at 200 MB each these must not linger.
        if not orphan.with_suffix(".json").exists():
            try:
                expired = (
                    datetime.fromtimestamp(
                        orphan.stat().st_mtime, tz=timezone.utc
                    )
                    < cutoff
                )
            except OSError:
                continue
            if expired and re.fullmatch(
                r"[A-Za-z0-9_-]{8,64}", orphan.stem or ""
            ):
                adapter.delete(f"previews/{orphan.stem}.bin")
    for meta_path in root.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text())
            created = datetime.fromisoformat(meta.get("created_at", ""))
            expired = created < cutoff
        except (ValueError, OSError, TypeError):
            continue
        if expired:
            key = meta_path.stem
            if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
                adapter.delete(f"previews/{key}.bin")
                adapter.delete(f"previews/{key}.json")


def _derive_sheet(content, original_name, limits, message_fallback,
                  sheet_name=None):
    """Parse preview bytes into headers, map, sources, missing, rows, sheet.

    Shared by creation, sheet switching, and assist-apply so re-derived
    previews always match what a fresh upload of the same bytes and
    selected sheet would show. Delimited tables use the bounded sample
    parse (head rows plus an exact counted total) so a 200 MB file
    derives in about a second instead of tens of seconds.
    """
    extension = delimited_extension(original_name)
    if extension and not message_fallback:
        title = (original_name or "upload").rsplit("/", 1)[-1]
        try:
            sheet, total = parse_delimited_sample(
                content,
                title,
                max_rows=limits["max_rows"],
                max_cols=limits["max_cols"],
                header_scan_rows=limits["header_scan_rows"],
                forced_delimiter="\t" if extension == ".tsv" else None,
                sheet_name=sheet_name,
                data_row_budget=0,
            )
            return (
                sheet.headers,
                sheet.column_map,
                sheet.sources,
                [],
                total,
                sheet.sheet,
            )
        except MappingRequired as exc:
            return (
                exc.headers,
                exc.column_map,
                exc.sources,
                exc.missing,
                exc.total_rows,
                exc.sheet or sheet_name,
            )
    try:
        sheet = parse_upload(
            content,
            original_name,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
            message_fallback=message_fallback,
            sheet_name=sheet_name,
        )
        return (
            sheet.headers,
            sheet.column_map,
            sheet.sources,
            [],
            len(sheet.rows),
            sheet.sheet,
        )
    except MappingRequired as exc:
        return (
            exc.headers,
            exc.column_map,
            exc.sources,
            exc.missing,
            len(exc.rows),
            exc.sheet or sheet_name,
        )


def _default_sheet(entries: list):
    """First visible discovered section, or None when there is nothing."""
    for entry in entries:
        if entry.get("visible", True):
            return entry["name"]
    return None


def _sheet_block(entries: list, selected):
    """Public sheet-selection block: available, selected, omitted, notice."""
    omitted = [
        entry["name"] for entry in entries
        if entry["name"] != selected and entry.get("visible", True)
    ]
    hidden = sum(1 for entry in entries if not entry.get("visible", True))
    notice = None
    if omitted:
        dropped = ", ".join(repr(name) for name in omitted)
        notice = (
            f"Sheet {selected!r} selected — {dropped} will not be "
            "imported. Switch sheets to import a different one."
        )
        if hidden:
            notice += f" {hidden} hidden sheet(s) skipped."
    elif hidden:
        notice = f"{hidden} hidden sheet(s) skipped."
    return {
        "available": entries,
        "selected": selected,
        "omitted": omitted,
        "notice": notice,
    }


def _validate_sheet(entries: list, wanted, *, filename: str) -> str:
    """Resolve an explicit sheet choice against discovery, or refuse."""
    names = [entry["name"] for entry in entries]
    if wanted not in names:
        seen = ", ".join(names) or "(none)"
        raise ValidationError(
            f"Sheet {wanted!r} not found in {filename}. Available: {seen}."
        )
    entry = names.index(wanted)
    if not entries[entry].get("visible", True):
        raise ValidationError(f"Sheet {wanted!r} is hidden.")
    return wanted


def _load_preview(key: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
        raise ValidationError("Unknown or expired preview.")
    try:
        return json.loads(_adapter().read_bytes(f"previews/{key}.json"))
    except (FileNotFoundError, ValueError):
        raise ValidationError("Unknown or expired preview.") from None


@contextlib.contextmanager
def _preview_lock(key: str):
    """Serialize read-modify-write cycles on one preview.

    Every writer re-reads inside the lock, so two concurrent applies
    cannot both validate against revision N and silently collapse
    into one. Not re-entrant: helpers called under the lock must not
    lock again.
    """
    try:
        with _adapter().locked_bytes(f"previews/{key}.json"):
            yield
    except FileNotFoundError:
        raise ValidationError("Unknown or expired preview.") from None


def _memory_supplier(value) -> str:
    return ("" if value is None else str(value)).strip()[:255]


def remember_mapping(
    *,
    organization,
    headers: list,
    mapping: dict,
    supplier_hint="",
    constants=None,
    ignored=None,
):
    """Store a confirmed header-signature to mapping reuse record.

    Profiles are scoped by organization, header signature, AND supplier:
    one supplier's confirmed interpretation never overwrites another's.
    """
    signature = header_signature(headers)
    clean_constants = {str(k): v for k, v in (constants or {}).items()}
    clean_ignored = [str(name) for name in (ignored or [])]
    stored_mapping = {
        str(k): v for k, v in mapping.items() if k not in clean_ignored
    }
    supplier = _memory_supplier(supplier_hint)
    try:
        memory, created = ColumnMappingMemory.objects.get_or_create(
            organization=organization,
            signature=signature,
            supplier_hint=supplier,
            defaults={
                "headers": list(headers),
                "mapping": stored_mapping,
                "constants": clean_constants,
                "ignored": clean_ignored,
            },
        )
    except IntegrityError:
        # A pre-0029 schema still constrains (organization, signature),
        # so a second supplier collides with the first. Memory is
        # advisory: skip the write instead of failing the import.
        # Applying migration 0029 restores per-supplier rows.
        logger.warning(
            "Skipping mapping-memory write for org %s signature %s: "
            "legacy unique constraint; apply migration 0029.",
            getattr(organization, "id", "?"),
            signature,
        )
        return None
    if not created:
        from django.db.models import F

        memory.headers = list(headers)
        memory.mapping = stored_mapping
        memory.constants = clean_constants
        memory.ignored = clean_ignored
        memory.use_count = F("use_count") + 1
        memory.save()
    return memory


def lookup_memory(*, organization, headers: list, supplier=None):
    """Return the remembered mapping for ordered headers, if any.

    Message pastes share one header signature, so a message-lane lookup
    only matches when the confirmed supplier equals the remembered one:
    a new supplier never inherits another supplier's identity, currency,
    or grades. Table lanes fall back to the most recently used profile
    for the signature; it stays a visible proposal the reviewer edits.
    """
    signature = header_signature(headers)
    if list(headers) == [MESSAGE_COLUMN]:
        wanted = _memory_supplier(supplier).lower()
        if not wanted:
            return None
        return (
            ColumnMappingMemory.objects.filter(
                organization=organization, signature=signature
            )
            .exclude(supplier_hint="")
            .order_by("-last_used_at")
            .filter(supplier_hint__iexact=wanted)
            .first()
        )
    if supplier:
        match = (
            ColumnMappingMemory.objects.filter(
                organization=organization, signature=signature
            )
            .filter(supplier_hint__iexact=_memory_supplier(supplier))
            .order_by("-last_used_at")
            .first()
        )
        if match is not None:
            return match
    return (
        ColumnMappingMemory.objects.filter(
            organization=organization, signature=signature
        )
        .order_by("-last_used_at")
        .first()
    )


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
    # The synthetic message column is not mappable: drop anything the
    # header vocabulary does not know so it can never reach the parser
    # as a column override (message previews map nothing).
    effective = {k: v for k, v in (detected or {}).items() if k in HEADER_TOKENS}
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


def _message_lane_accepts(content: bytes, original_name: str, limits: dict) -> bool:
    """True when the paste/message lane can read these upload bytes."""
    try:
        parse_upload(
            content,
            original_name,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
            message_fallback=True,
        )
    except UnsupportedFile:
        return False
    return True


def _render_message_samples(*, content: bytes, merged: dict, applied: dict,
                              sheet, draft_rules, document_policy: str,
                              constants) -> dict:
    """Sample rows for the message lane, driven by the confirmed draft.

    Records, joins, section values, and terminal derivation come from
    the same derive_record_cells the worker imports, so the preview
    shows exactly what confirm would import. Explicitly submitted set
    values win over derived ones; derived values win over the
    organization currency default.
    """
    from pipeline.draft import derive_record_cells

    lines = _content_lines(content)
    derived = derive_record_cells(lines, draft_rules or [])
    explicit = set((constants or {}))
    rows = []
    for record_id, info in list(derived.items())[:SAMPLE_ROWS]:
        text = "\n".join(lines[number - 1] for number in info["lines"])
        cells = {"message": text}
        cells.update(merged)
        for name, value in info["cells"].items():
            if name not in explicit:
                cells[name] = value
        fields = normalize_cells(dict(cells))
        status, gaps, reason = compute_offer_status(
            fields, document_policy=document_policy
        )
        rows.append(
            {
                "index": info["lines"][0],
                "record": record_id,
                "lines": info["lines"],
                "cells": cells,
                "terminal": info["terminal"],
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
        "record_count": len(derived),
    }


def _render_samples(content: bytes, filename: str, mapping: dict,
                    constants=None, ignored=None, default_currency="",
                    message_fallback: bool = False,
                    sheet_name=None, exclude_lines=(),
                    draft_rules=None, document_policy: str = "stock") -> dict:
    skip = set(exclude_lines or ())
    limits = _limits()
    merged, labels, applied = _with_currency_default(
        mapping, constants, default_currency, ignored
    )
    extension = delimited_extension(filename)
    try:
        if extension and not message_fallback:
            # Samples show the first rows only: parse the head with
            # true record numbers instead of materializing a 200 MB
            # table on every mapping tweak. The budget covers skipped
            # head rows (tabular previews never carry exclusions, but
            # a stale set must not shift the sample window either).
            title = (filename or "upload").rsplit("/", 1)[-1]
            sheet, _total = parse_delimited_sample(
                content,
                title,
                max_rows=limits["max_rows"],
                max_cols=limits["max_cols"],
                header_scan_rows=limits["header_scan_rows"],
                column_overrides=mapping or None,
                column_constants=merged or None,
                ignored_columns=ignored or None,
                constant_sources=labels or None,
                forced_delimiter="\t" if extension == ".tsv" else None,
                sheet_name=sheet_name,
                data_row_budget=SAMPLE_ROWS + min(len(skip), 64),
            )
        else:
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
                message_fallback=message_fallback,
                sheet_name=sheet_name,
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
                for row in exc.rows
                if row.index not in skip
            ][:SAMPLE_ROWS],
        }
    if message_fallback:
        return _render_message_samples(
            content=content,
            merged=merged,
            applied=applied,
            sheet=sheet,
            draft_rules=draft_rules,
            document_policy=document_policy,
            constants=constants,
        )
    rows = []
    for row in [row for row in sheet.rows if row.index not in skip][
        :SAMPLE_ROWS
    ]:
        fields = normalize_cells(dict(row.data))
        status, gaps, reason = compute_offer_status(
            fields, document_policy=document_policy
        )
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
    original_name = getattr(uploaded_file, "name", "") or ""
    # Extension, then declared size: both refuse without reading the
    # body, so a hostile 200 MB pick costs no 200 MB read. The size
    # error stays an UploadTooLarge (413), never a plain refusal.
    try:
        check_upload_extension(original_name)
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    refuse_oversize(uploaded_file.size, max_bytes=_limits()["max_bytes"])
    content = uploaded_file.read()
    if not content:
        raise ValidationError("Empty file.")
    return _create_preview(
        organization=organization,
        user=user,
        content=content,
        original_name=original_name,
        message_fallback=False,
    )


def create_preview_from_text(*, organization, user, text: str) -> dict:
    """Preview pasted text as if it were an uploaded .txt file.

    Tabular pastes flow through the normal mapping UI; headerless
    pastes become message rows (one per source line, exact text kept)
    that the interpretation lane reads, flagged message_lines for
    the UI. Delimited lines without headers are read as message
    lines too — the assistant clarifies what each position means
    before anything is mapped.
    """
    content = (text or "").encode("utf-8")
    if not content.strip():
        raise ValidationError("Paste some text first.")
    # Pasted text shares the upload byte limit: a 200 MB paste would
    # otherwise run the same multi-tens-of-seconds parse as a file.
    refuse_oversize(len(content), max_bytes=_limits()["max_bytes"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return _create_preview(
        organization=organization,
        user=user,
        content=content,
        original_name=f"pasted-text-{stamp}.txt",
        message_fallback=True,
    )


def _create_preview(
    *, organization, user, content: bytes, original_name: str,
    message_fallback: bool,
) -> dict:
    from pipeline.jobs import record_audit

    limits = _limits()
    try:
        extension = check_upload_safety(
            content,
            original_name,
            max_bytes=limits["max_bytes"],
            max_uncompressed_bytes=limits["max_uncompressed_bytes"],
        )
    except UploadTooLarge:
        raise
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    digest = hashlib.sha256(content).hexdigest()
    _reject_duplicate(
        organization=organization, user=user, original_name=original_name, digest=digest
    )
    try:
        entries = discover_sheets(content, original_name)
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    selected = _default_sheet(entries)
    try:
        headers, detected, sources, missing, total_rows, derived = _derive_sheet(
            content, original_name, limits, message_fallback, selected
        )
    except HeaderNotFound as exc:
        if (
            extension in (".txt", ".csv")
            and not message_fallback
            and _message_lane_accepts(content, original_name, limits)
        ):
            text, truncated, total_chars = candidate_message_text(content)
            raise MessageCandidateFile(
                text, truncated=truncated, total_chars=total_chars
            ) from exc
        raise ValidationError(str(exc)) from exc
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    selected = derived or selected
    # Only the message fallback produces the synthetic message column;
    # header matching only ever yields canonical names.
    message_lines = detected == {MESSAGE_COLUMN: 0}
    memory = lookup_memory(organization=organization, headers=headers)
    # Message lines map nothing: every field comes from set values or
    # interpretation, so there is no column proposal.
    proposed = {} if message_lines else dict(detected)
    proposed_constants: dict = {}
    proposed_ignored: list = []
    memory_info = None
    if memory is not None and (message_lines or _memory_usable(memory, headers)):
        if not message_lines:
            proposed = {str(k): v for k, v in memory.mapping.items()}
            proposed_ignored = list(memory.ignored or [])
        # Message pastes share one header signature, so only set values
        # (typically the supplier) carry over — column positions never do,
        # and ignored stays empty because message confirms reject it.
        proposed_constants = {
            str(k): v for k, v in (memory.constants or {}).items()
            if k in HEADER_TOKENS
        }
        memory_info = {
            "use_count": memory.use_count,
            "last_used_at": memory.last_used_at.isoformat(),
            "supplier_hint": memory.supplier_hint,
        }
    import secrets

    key = secrets.token_urlsafe(24)
    adapter = _adapter()
    try:
        adapter.save_bytes(f"previews/{key}.bin", content)
    except OSError:
        # A failed bytes write (for example a full disk on a large
        # upload) must surface loudly and leave no phantom preview.
        adapter.delete(f"previews/{key}.bin")
        adapter.delete(f"previews/{key}.json")
        raise
    sheets = _sheet_block(entries, selected)
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
        "message_lines": message_lines,
        "sheet": selected,
        "sheets": entries,
        "sheets_omitted": sheets["omitted"],
        "total_rows": total_rows,
        "revision": 1,
        "rules": [],
        "document_policy": "stock",
        "history": [],
        "next_rule_id": 1,
        "excluded_lines": [],
        "applied_constants": {},
        "last_mapping": {
            "mapping": proposed,
            "constants": proposed_constants,
            "ignored": proposed_ignored,
        },
    }
    try:
        adapter.save_bytes(
            f"previews/{key}.json", json.dumps(meta).encode("utf-8")
        )
    except OSError:
        adapter.delete(f"previews/{key}.bin")
        adapter.delete(f"previews/{key}.json")
        raise
    _gc_previews()
    samples = _render_samples(
        content,
        original_name,
        proposed,
        proposed_constants,
        proposed_ignored,
        organization.default_currency,
        message_fallback=message_fallback,
        sheet_name=selected,
        draft_rules=[],
        document_policy="stock",
    )
    record_audit(
        organization.id,
        user,
        "preview_created",
        "preview",
        key,
        {
            "filename": original_name[:255],
            "sha256": digest,
            "sheet": selected,
            "sheets_omitted": sheets["omitted"],
        },
    )
    if message_lines:
        from pipeline.draft import derive_records

        _records, _unresolved, _sections = derive_records(
            _content_lines(content), []
        )
    else:
        _records, _unresolved, _sections = [], [], []
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
        "message_lines": message_lines,
        "total_rows": total_rows,
        "memory": memory_info,
        "proposed": proposed,
        "proposed_constants": proposed_constants,
        "proposed_ignored": proposed_ignored,
        "samples": samples,
        "sheets": sheets,
        "draft": _draft_block(
            meta,
            lines=[],
            records=_records,
            unresolved=_unresolved,
            sections=_sections,
        ),
    }


def _ensure_sheets(key: str, meta: dict, content: bytes) -> dict:
    """Backfill discovery for previews created before sheet tracking."""
    if "sheets" in meta:
        return meta
    try:
        entries = discover_sheets(content, meta["filename"])
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    selected = meta.get("sheet") or _default_sheet(entries)
    block = _sheet_block(entries, selected)
    meta.update(
        {
            "sheet": selected,
            "sheets": entries,
            "sheets_omitted": block["omitted"],
        }
    )
    _adapter().save_bytes(f"previews/{key}.json", json.dumps(meta).encode("utf-8"))
    return meta


def _switch_preview_sheet(key: str, meta: dict, content: bytes, wanted: str):
    """Re-derive a preview for another discovered sheet and persist it.

    Returns (meta, missing, total_rows, block). Old-sheet column
    positions are meaningless under the new headers: render resets to
    the fresh detection, confirm validates the submitted mapping
    against the new headers instead.
    """
    meta = _ensure_sheets(key, meta, content)
    selected = _validate_sheet(meta["sheets"], wanted, filename=meta["filename"])
    try:
        headers, detected, sources, missing, total, derived = _derive_sheet(
            content,
            meta["filename"],
            _limits(),
            bool(meta.get("message_lines")),
            selected,
        )
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    selected = derived or selected
    block = _sheet_block(meta["sheets"], selected)
    meta.update(
        {
            "headers": headers,
            "detected": detected,
            "sources": sources,
            "sheet": selected,
            "sheets_omitted": block["omitted"],
            "total_rows": total,
        }
    )
    _adapter().save_bytes(f"previews/{key}.json", json.dumps(meta).encode("utf-8"))
    return meta, missing, total, block


def render_preview(*, key: str, user, mapping: dict,
                   constants=None, ignored=None, sheet=None) -> dict:
    """Re-render sample rows for an adjusted mapping (or another sheet)."""
    with _preview_lock(key):
        meta = _ensure_draft(_load_preview(key))
        organization = authorize_organization_access(user, meta["organization_id"])
        try:
            content = _adapter().read_bytes(f"previews/{key}.bin")
        except FileNotFoundError:
            raise ValidationError("Unknown or expired preview.") from None
        meta = _ensure_sheets(key, meta, content)
        switched = None
        if sheet is not None and sheet != meta.get("sheet"):
            meta, missing, total, _block = _switch_preview_sheet(
                key, meta, content, sheet
            )
            # Old-sheet positions cannot apply to the new headers.
            mapping, constants, ignored = {}, {}, []
            switched = {
                "headers": _header_entries(meta["headers"]),
                "detected": meta["detected"],
                "missing": missing,
                "total_rows": total,
            }
        _submitted, effective, clean_constants, clean_ignored = _clean_positions(
            mapping,
            meta["headers"],
            meta["detected"],
            require_complete=False,
            constants=constants,
            ignored=ignored,
        )
        # Remember the mapping the user actually reviewed, so a refresh
        # resumes the editor state instead of only the draft rules.
        meta["last_mapping"] = {
            "mapping": effective,
            "constants": clean_constants,
            "ignored": clean_ignored,
        }
        _adapter().save_bytes(
            f"previews/{key}.json", json.dumps(meta).encode("utf-8")
        )
        message_lane = meta.get("message_lines") is True
        memory_info = _memory_info_for_supplier(
            organization,
            meta["headers"],
            clean_constants.get("supplier") or "",
        )
        if message_lane:
            from pipeline.draft import derive_records

            _records, _unresolved, _sections = derive_records(
                _content_lines(content), meta.get("rules") or []
            )
        else:
            _records, _unresolved, _sections = [], [], []
        payload = {
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
                message_fallback=message_lane,
                sheet_name=meta.get("sheet"),
                exclude_lines=meta.get("excluded_lines") or [],
                draft_rules=meta.get("rules") or [],
                document_policy=meta.get("document_policy", "stock"),
            ),
            "sheets": _sheet_block(meta.get("sheets") or [], meta.get("sheet")),
            "memory": memory_info,
            "draft": _draft_block(
                meta,
                lines=[],
                records=_records,
                unresolved=_unresolved,
                sections=_sections,
            ),
        }
        if switched is not None:
            payload["switched"] = switched
        return payload


def _memory_info_for_supplier(organization, headers, supplier: str):
    """Saved-profile card for a render/resume response, if one applies."""
    supplier = (supplier or "").strip()
    if not supplier:
        return None
    memory = lookup_memory(
        organization=organization,
        headers=headers,
        supplier=supplier,
    )
    if memory is None:
        return None
    return {
        "supplier_hint": memory.supplier_hint,
        "use_count": memory.use_count,
        "last_used_at": memory.last_used_at.isoformat(),
        "constants": {
            str(k): v
            for k, v in (memory.constants or {}).items()
            if k in HEADER_TOKENS
        },
        "reason": (
            f"Saved profile for {memory.supplier_hint} "
            f"(used {memory.use_count} times)."
        ),
    }


def get_preview_draft(*, key: str, user) -> dict:
    """Return the persisted draft so a refresh resumes where it left off."""
    meta = _ensure_draft(_load_preview(key))
    organization = authorize_organization_access(user, meta["organization_id"])
    organization  # membership verified; the draft carries no secrets.
    try:
        content = _adapter().read_bytes(f"previews/{key}.bin")
    except FileNotFoundError:
        raise ValidationError("Unknown or expired preview.") from None
    if bool(meta.get("message_lines")):
        from pipeline.draft import derive_records

        records, unresolved, sections = derive_records(
            _content_lines(content), meta.get("rules") or []
        )
    else:
        records, unresolved, sections = [], [], []
    return {
        "key": key,
        "filename": meta["filename"][:255],
        "extension": meta["extension"],
        "size_bytes": len(content),
        "sha256": meta["sha256"],
        "headers": _header_entries(meta["headers"]),
        "detected": meta["detected"],
        "sources": meta["sources"],
        "message_lines": bool(meta.get("message_lines")),
        "sheet": meta.get("sheet"),
        "sheets": _sheet_block(meta.get("sheets") or [], meta.get("sheet")),
        "applied_constants": meta.get("applied_constants", {}),
        "draft": _draft_block(
            meta,
            lines=[],
            records=records,
            unresolved=unresolved,
            sections=sections,
        ),
    }


def _preview_total_rows(key: str, meta: dict, content: bytes) -> int:
    """Stored tabular data-row total, backfilling older previews.

    The count never depends on the mapping, so resume and assist
    reuse the reviewed value instead of re-scanning 200 MB. Caller
    must hold _preview_lock: a backfill persists the value.
    """
    stored = meta.get("total_rows")
    if type(stored) is int and stored >= 0:
        return stored
    _h, _d, _s, _m, total, _derived = _derive_sheet(
        content,
        meta["filename"],
        _limits(),
        False,
        meta.get("sheet"),
    )
    meta["total_rows"] = total
    _adapter().save_bytes(
        f"previews/{key}.json", json.dumps(meta).encode("utf-8")
    )
    return total


def resume_preview(*, key: str, user) -> dict:
    """Rebuild the full preview payload for a pending draft.

    A refresh-safe resume: restores the last rendered mapping, the
    assisted rules, exclusions and policy — the same Preview the
    editor was showing. Raises like any unknown key when the draft
    is confirmed, cancelled or expired.
    """
    with _preview_lock(key):
        meta = _ensure_draft(_load_preview(key))
        organization = authorize_organization_access(user, meta["organization_id"])
        try:
            content = _adapter().read_bytes(f"previews/{key}.bin")
        except FileNotFoundError:
            raise ValidationError("Unknown or expired preview.") from None
        meta = _ensure_sheets(key, meta, content)
        stored = meta.get("last_mapping") or {}
        _submitted, effective, clean_constants, clean_ignored = _clean_positions(
            stored.get("mapping") or {},
            meta["headers"],
            meta["detected"],
            require_complete=False,
            constants=stored.get("constants") or {},
            ignored=stored.get("ignored") or [],
        )
        message_lane = meta.get("message_lines") is True
        memory_info = _memory_info_for_supplier(
            organization,
            meta["headers"],
            clean_constants.get("supplier") or "",
        )
        if message_lane:
            from pipeline.draft import derive_records

            records, unresolved, sections = derive_records(
                _content_lines(content), meta.get("rules") or []
            )
            total_rows = len(_content_lines(content))
        else:
            records, unresolved, sections = [], [], []
            total_rows = _preview_total_rows(key, meta, content)
        samples = _render_samples(
            content,
            meta["filename"],
            effective,
            clean_constants,
            clean_ignored,
            organization.default_currency,
            message_fallback=message_lane,
            sheet_name=meta.get("sheet"),
            exclude_lines=meta.get("excluded_lines") or [],
            draft_rules=meta.get("rules") or [],
            document_policy=meta.get("document_policy", "stock"),
        )
        missing = [
            name
            for name in REQUIRED_COLUMNS
            if meta["detected"].get(name) is None
            and effective.get(name) is None
            and clean_constants.get(name) in (None, "")
        ]
        return {
            "key": key,
            "filename": meta["filename"][:255],
            "extension": meta["extension"],
            "size_bytes": len(content),
            "sha256": meta["sha256"],
            "headers": _header_entries(meta["headers"]),
            "detected": meta["detected"],
            "sources": meta["sources"],
            "missing": missing,
            "message_lines": message_lane,
            "total_rows": total_rows,
            "memory": memory_info,
            "proposed": effective,
            "proposed_constants": clean_constants,
            "proposed_ignored": clean_ignored,
            "samples": samples,
            "sheets": _sheet_block(meta.get("sheets") or [], meta.get("sheet")),
            "draft": _draft_block(
                meta,
                lines=[],
                records=records,
                unresolved=unresolved,
                sections=sections,
            ),
        }


PENDING_PREVIEWS_LIMIT = 50


def list_pending_previews(*, user, organization_id) -> list:
    """Metadata-only list of the org's unconfirmed previews for resume.

    Membership-scoped: only previews whose owning organization the
    user belongs to. Returns summaries, never source bytes. Expired
    drafts are collected, not listed.
    """
    from pathlib import Path

    organization = authorize_organization_access(user, organization_id)
    _gc_previews()
    root = Path(settings.OFFERFLOW_FILE_ROOT) / "previews"
    if not root.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - PREVIEW_TTL
    adapter = _adapter()
    items = []
    for meta_path in root.glob("*.json"):
        key = meta_path.stem
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key or ""):
            continue
        try:
            meta = json.loads(meta_path.read_text())
            created = datetime.fromisoformat(meta.get("created_at", ""))
            expired = created < cutoff
        except (ValueError, OSError, TypeError):
            continue
        if meta.get("organization_id") != organization.id:
            continue
        if expired:
            continue
        if not adapter.exists(f"previews/{key}.bin"):
            continue
        items.append(
            {
                "key": key,
                "filename": str(meta.get("filename", ""))[:255],
                "created_at": meta.get("created_at"),
                "revision": meta.get("revision", 1),
                "document_policy": meta.get("document_policy", "stock"),
                "rules_count": len(meta.get("rules") or []),
                "excluded_count": len(meta.get("excluded_lines") or []),
                "message_lines": bool(meta.get("message_lines")),
                "sheet": meta.get("sheet"),
            }
        )
    items.sort(key=lambda item: item["created_at"] or "", reverse=True)
    return items[:PENDING_PREVIEWS_LIMIT]


def _supplier_hint(content: bytes, filename: str, mapping: dict, constants: dict,
                   sheet_name=None) -> str:
    if "supplier" in (constants or {}):
        return str(constants["supplier"]).strip()[:255]
    limits = _limits()
    try:
        extension = delimited_extension(filename)
        if extension:
            # Only the first data row is ever read: one head record
            # instead of a full 200 MB parse on every confirm.
            title = (filename or "upload").rsplit("/", 1)[-1]
            sheet, _total = parse_delimited_sample(
                content,
                title,
                max_rows=limits["max_rows"],
                max_cols=limits["max_cols"],
                header_scan_rows=limits["header_scan_rows"],
                column_overrides=mapping or None,
                forced_delimiter="\t" if extension == ".tsv" else None,
                sheet_name=sheet_name,
                data_row_budget=1,
            )
        else:
            sheet = parse_upload(
                content,
                filename,
                max_rows=limits["max_rows"],
                max_cols=limits["max_cols"],
                header_scan_rows=limits["header_scan_rows"],
                column_overrides=mapping or None,
                sheet_name=sheet_name,
            )
    except UnsupportedFile:
        return ""
    if not sheet.rows:
        return ""
    supplier = sheet.rows[0].data.get("supplier")
    return ("" if supplier is None else str(supplier).strip())[:255]


def confirm_preview(*, key: str, user, mapping: dict,
                    constants=None, ignored=None, sheet=None, revision=None):
    """Validate a full mapping and start the import run.

    Snapshots the confirmed draft revision — rules, exclusions, policy,
    and derived record lines — into the run, so the worker imports
    exactly what the user reviewed instead of re-extracting.
    """
    with _preview_lock(key):
        from pipeline.draft import derive_records
        from pipeline.uploads import store_upload_run

        meta = _ensure_draft(_load_preview(key))
        organization = authorize_organization_access(user, meta["organization_id"])
        require_organization_role(user, meta["organization_id"])
        if revision is not None and revision != meta.get("revision", 1):
            raise DraftConflictError(meta.get("revision", 1), revision)
        try:
            content = _adapter().read_bytes(f"previews/{key}.bin")
        except FileNotFoundError:
            raise ValidationError("Unknown or expired preview.") from None
        meta = _ensure_sheets(key, meta, content)
        if sheet is not None and sheet != meta.get("sheet"):
            # The submitted mapping is validated against the NEW headers;
            # a stale old-sheet mapping fails honestly in _clean_positions.
            meta, _missing, _total, _block = _switch_preview_sheet(
                key, meta, content, sheet
            )
        message_lines = meta.get("message_lines") is True
        if message_lines and (mapping or ignored):
            raise ValidationError(
                "Message imports read every line as-is: submit an empty "
                "mapping and only set values such as the supplier."
            )
        submitted, effective, clean_constants, clean_ignored = _clean_positions(
            mapping,
            meta["headers"],
            meta["detected"],
            require_complete=not message_lines,
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
        draft_globals = {
            rule["field"]: rule["value"]
            for rule in meta.get("rules") or []
            if rule["kind"] == "global"
        }
        merged_constants = {**draft_globals, **clean_constants}
        run_mapping = {
            "headers": meta["headers"],
            "detected": meta["detected"],
            "confirmed": submitted,
            "constants": merged_constants,
            "ignored": clean_ignored,
            "sheet": meta.get("sheet"),
            "sheets_omitted": meta.get("sheets_omitted", []),
            "draft_revision": meta.get("revision", 1),
            "draft_rules": meta.get("rules") or [],
            "excluded_lines": meta.get("excluded_lines") or [],
            "document_policy": meta.get("document_policy", "stock"),
        }
        if message_lines:
            run_mapping["free_text"] = True
            records, _unresolved, _sections = derive_records(
                _content_lines(content), meta.get("rules") or []
            )
            run_mapping["record_lines"] = [
                {"id": record["id"], "lines": record["lines"]}
                for record in records
            ]
        run = store_upload_run(
            organization=organization,
            user=user,
            content=content,
            original_name=meta["filename"],
            mapping=run_mapping,
        )
        remember_mapping(
            organization=organization,
            headers=meta["headers"],
            mapping=effective,
            constants=merged_constants,
            ignored=clean_ignored,
            supplier_hint=_supplier_hint(
                content, meta["filename"], effective, merged_constants,
                sheet_name=meta.get("sheet"),
            ),
        )
        adapter = _adapter()
        adapter.delete(f"previews/{key}.bin")
        adapter.delete(f"previews/{key}.json")
        return run


def cancel_preview(*, key: str, user) -> None:
    """Drop preview bytes after a membership check."""
    with _preview_lock(key):
        meta = _load_preview(key)
        authorize_organization_access(user, meta["organization_id"])
        adapter = _adapter()
        adapter.delete(f"previews/{key}.bin")
        adapter.delete(f"previews/{key}.json")


class AssistRefused(Exception):
    """The guard refused the turn before any token was spent."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AssistInFlight(Exception):
    """The same idempotency key already has a live attempt.

    The client should keep polling with the same key instead of
    sending a new request: the running attempt never dispatches
    twice, and its result becomes retrievable under the key.
    """

    def __init__(self, key: str):
        super().__init__(
            "That request is still running; retry with the same key "
            "to collect its result instead of starting a new call."
        )
        self.key = key


# A pending attempt older than this is presumed dead (the process
# died mid-call): a retry reclaims the key for a new attempt. The
# abandoned attempt's uncertain spend stays booked under the
# reservation lifecycle, and the retry is an explicit new attempt
# that may spend again — only completed-result replay avoids a
# second charge.
IDEMPOTENCY_RECLAIM_SECONDS = 600

_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _assist_request_hash(preview_key: str, turns: list, base_revision) -> str:
    # The draft revision is part of the request identity: the same
    # conversation against a changed draft is a different request, so
    # a reconnect replay can never serve a proposal computed against
    # older confirmed rules.
    payload = json.dumps(
        [preview_key, turns, base_revision],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _claim_assist_key(
    *,
    organization,
    user,
    preview_key,
    request_hash,
    idempotency_key,
    turns,
    base_revision,
):
    """Claim one idempotent attempt, or return the stored outcome.

    Returns the AssistIdempotency row ready for a fresh dispatch,
    except state "done" rows, which the caller replays without
    dispatching. A live "pending" row raises AssistInFlight; a stale
    one is reclaimed for an explicit new attempt. A key bound to a
    different request fails loudly instead of returning the wrong
    answer. The queued turns and base revision persist on the row so
    the worker runs exactly the attempt the sender queued.
    """
    from django.db.models import F
    from django.utils import timezone as dj_timezone

    from pipeline.models import AssistIdempotency

    if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key or ""):
        raise ValidationError("idempotency_key is not a valid key.")
    queued = {"turns": turns, "base_revision": base_revision}
    try:
        record, created = AssistIdempotency.objects.get_or_create(
            organization=organization,
            key=idempotency_key,
            defaults={
                "user": user,
                "preview_key": preview_key,
                "request_hash": request_hash,
                "request": queued,
                "attempts": 1,
                "state": "pending",
            },
        )
    except IntegrityError:
        # Lost a race with an identical claim: the winner owns it.
        record = AssistIdempotency.objects.get(
            organization=organization, key=idempotency_key
        )
        created = False
    if created:
        return record
    if record.request_hash != request_hash:
        raise ValidationError(
            "That idempotency key was used for a different request."
        )
    if record.state == "done":
        return record
    if record.state == "pending":
        age = dj_timezone.now() - record.updated_at
        if age.total_seconds() < IDEMPOTENCY_RECLAIM_SECONDS:
            raise AssistInFlight(idempotency_key)
        logger.warning(
            "Reclaiming stale assist key for org %s after %ds.",
            organization.id,
            int(age.total_seconds()),
        )
    record.user = user
    record.preview_key = preview_key
    record.request = queued
    record.attempts = F("attempts") + 1
    record.claimed = False
    record.state = "pending"
    record.response = None
    record.tokens = 0
    record.save()
    record.refresh_from_db()
    return record


class DraftConflictError(Exception):
    """The writer's draft revision is stale; reload before retrying."""

    def __init__(self, revision_actual: int, revision_expected, detail=None):
        super().__init__(
            detail
            or (
                f"Draft is at revision {revision_actual}; request targeted "
                f"{revision_expected}. Reload the preview and review the "
                "latest rules before retrying."
            )
        )
        self.revision_actual = revision_actual
        self.revision_expected = revision_expected


ASSIST_PREVIEW_LINES = 40
ASSIST_PREVIEW_CHARS = 4000
ASSIST_MAX_DROPS = 20
ASSIST_TURNS = 6


def _clean_assist_turns(conversation) -> list:
    if not isinstance(conversation, list) or not conversation:
        raise ValidationError("Conversation needs at least one message.")
    turns = []
    for turn in conversation[-ASSIST_TURNS:]:
        if not isinstance(turn, dict):
            raise ValidationError("Each message needs a role and content.")
        role, content = turn.get("role"), turn.get("content")
        if role not in ("user", "assistant"):
            raise ValidationError("Message roles are user or assistant.")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > 2000
        ):
            raise ValidationError("Each message must be 1..2000 characters.")
        turns.append({"role": role, "content": content})
    if turns[-1]["role"] != "user":
        raise ValidationError("The conversation must end with your message.")
    return turns


def _content_lines(content: bytes) -> list:
    return content.decode("utf-8", errors="replace").split("\n")


def _assist_preview_context(*, meta, content, organization) -> dict:
    context = {
        "filename": meta["filename"],
        "message_lines": bool(meta.get("message_lines")),
        "default_currency": organization.default_currency,
    }
    if not context["message_lines"]:
        context.update(
            {
                "headers": list(meta.get("headers") or []),
                "detected": dict(meta.get("detected") or {}),
            }
        )
        return context
    numbered = []
    chars = 0
    truncated = False
    all_lines = _content_lines(content)
    excluded = set(meta.get("excluded_lines") or [])
    live = [
        (number, text)
        for number, text in enumerate(all_lines, start=1)
        if number not in excluded
    ]
    for number, text in live:
        stripped = text.strip()
        if not stripped:
            continue
        if len(numbered) >= ASSIST_PREVIEW_LINES or chars >= ASSIST_PREVIEW_CHARS:
            truncated = True
            break
        numbered.append({"line": number, "text": stripped[:200]})
        chars += len(stripped)
    from pipeline.draft import derive_records as _derive_assist_records

    rules = meta.get("rules") or []
    records, unresolved, sections = _derive_assist_records(all_lines, rules)
    shown = {entry["line"] for entry in numbered}
    context.update(
        {
            "lines": numbered,
            "line_count": len(all_lines),
            "total_rows": sum(1 for _number, text in live if text.strip()),
            "truncated": truncated,
            "document_policy": meta.get("document_policy", "stock"),
            "rules": rules[-50:],
            "rule_count": len(rules),
            "records": [
                {
                    "id": record["id"],
                    "lines": record["lines"],
                    "class": record["class"],
                    "section": record["section"],
                }
                for record in records[:60]
            ],
            "record_count": len(records),
            "sections": sections,
            "unresolved": unresolved[:20],
            "unresolved_count": len(unresolved),
        }
    )
    if truncated:
        # The line quote cut off: section headers beyond the cutoff
        # still travel with their record counts, so later segments
        # keep their inherited context instead of losing it silently.
        context["sections_beyond_cutoff"] = [
            {
                "header_line": section["header_line"],
                "text": all_lines[section["header_line"] - 1].strip()[:120],
                "records": len(section["records"]),
            }
            for section in sections
            if section["header_line"] not in shown
        ]
    return context


def _clean_assist_constants(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("set_constants must be an object.")
    cleaned = {}
    for name, value in raw.items():
        if name not in HEADER_TOKENS:
            raise ValidationError(f"Unknown value {name!r}.")
        cleaned[name] = _clean_constant(name, value)
    return cleaned


def _clean_assist_drops(raw, *, message_lines, content) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("drop_lines must be a list of line numbers.")
    if raw and not message_lines:
        raise ValidationError("Only pasted message lines can be dropped.")
    lines = _content_lines(content)
    drops = []
    for number in raw:
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValidationError("drop_lines must hold line numbers.")
        if not 1 <= number <= len(lines):
            raise ValidationError(f"Line {number} is outside the paste.")
        drops.append(number)
    drops = sorted(set(drops))
    if len(drops) > ASSIST_MAX_DROPS:
        raise ValidationError(f"At most {ASSIST_MAX_DROPS} lines per request.")
    dropped = set(drops)
    kept = [
        text
        for number, text in enumerate(lines, start=1)
        if number not in dropped and text.strip()
    ]
    if raw and not kept:
        raise ValidationError("At least one pasted line must remain.")
    return drops


def _dropped_previews(content, drops) -> list:
    lines = _content_lines(content)
    return [
        {"line": number, "text": lines[number - 1].strip()[:120]}
        for number in drops
    ]


def _clean_assist_operations(raw, *, message_lines, content, rules, policy):
    """Validate model-proposed typed operations against the live draft.

    Returns (operations, note). Every kept op validates exactly as a
    user-submitted op would. When a later op in the batch fails, the
    longest valid leading run is still proposed as appliable cards and
    the note names the rejected remainder precisely, so a valid
    business explanation is not discarded wholesale because one
    dependent operation needs clarification. A batch whose first op
    fails still fails the whole turn loudly.
    """
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        raise ValidationError("operations must be a list.")
    if not raw:
        return [], ""
    if not message_lines and any(
        (op.get("op") if isinstance(op, dict) else None) != "global"
        for op in raw
    ):
        raise ValidationError(
            "Only set values apply to table previews; line operations "
            "need pasted message lines."
        )
    from pipeline.draft import DraftError, validate_operations

    lines = _content_lines(content)
    try:
        _pending, _policy, _per_op = validate_operations(
            list(raw),
            lines=lines,
            rules=rules,
            policy=policy,
        )
    except DraftError as exc:
        prefix = []
        for op in raw:
            try:
                validate_operations(
                    prefix + [op], lines=lines, rules=rules, policy=policy
                )
            except DraftError:
                break
            prefix.append(op)
        if not prefix:
            raise ValidationError(str(exc)) from exc
        note = (
            f" Note: the preview kept {len(prefix)} of {len(raw)} "
            f"proposed changes and left out the rest: {exc}"
        )
        return [dict(op) for op in prefix], note
    return [dict(op) for op in raw], ""


def _prepare_assist_turn(*, key: str, user, conversation) -> dict:
    """Run the fast synchronous checks for one assist turn.

    Returns the organization, preview metadata, source bytes, cleaned
    turns, base revision, and provider. Raises AssistRefused for
    out-of-scope questions, PermissionDenied for banned accounts, and
    ProviderNotConfigured when only the simulated provider exists —
    all before any key is claimed or model time is spent.
    """
    from common.provider import ProviderNotConfigured, get_provider
    from organizations.models import UserPreference
    from pipeline.assistant_guard import PREVIEW, inspect_question
    from pipeline.models import AssistantFlag

    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    turns = _clean_assist_turns(conversation)
    if UserPreference.objects.filter(
        user=user, assistant_banned=True
    ).exists():
        raise PermissionDenied("Assistant access is disabled for your account.")
    reason = inspect_question(turns[-1]["content"], scope=PREVIEW)
    if reason is not None:
        AssistantFlag.objects.create(
            organization=organization,
            user=user,
            username=user.username,
            question=turns[-1]["content"][:2000],
            reason=reason,
        )
        raise AssistRefused(reason)
    provider = get_provider(organization_id=organization.id)
    if provider.is_simulated:
        raise ProviderNotConfigured("The assistant needs the paid model.")
    content = _adapter().read_bytes(f"previews/{key}.bin")
    return {
        "organization": organization,
        "meta": meta,
        "content": content,
        "turns": turns,
        "base_revision": meta.get("revision", 1),
        "provider": provider,
    }


def _run_assist_attempt(
    *, organization, user, meta, content, turns, base_revision, provider, spent
) -> dict:
    """Execute one model attempt synchronously and return its reply.

    Books chunk usage as it arrives, validates every proposed value,
    and raises ProviderError, ProposalUnusable, or ValidationError on
    failure. The caller persists the outcome; spent accumulates the
    booked chunk tokens so failures still report their cost.
    """
    from common.meta import ProposalUnusable

    context = _assist_preview_context(
        meta=meta, content=content, organization=organization
    )
    question = turns[-1]["content"][:120]
    provider_model = getattr(getattr(provider, "_config", None), "model", "")
    if not isinstance(provider_model, str):
        provider_model = ""

    def _note_chunk(position, total, prompt_tokens, completion_tokens):
        from pipeline.jobs import record_usage

        spent["total"] += prompt_tokens + completion_tokens
        record_usage(
            organization.id,
            None,
            "provider_call",
            prompt_tokens + completion_tokens,
            {
                "provider": provider.name,
                "model": provider_model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "task": "assistant",
                "question": question,
            },
            user=user,
        )

    result = provider.assist_preview(
        {"preview": context, "conversation": turns}, on_chunk=_note_chunk
    )
    try:
        constants = _clean_assist_constants(
            result.get("set_constants", {})
        )
        drops = _clean_assist_drops(
            result.get("drop_lines", []),
            message_lines=bool(meta.get("message_lines")),
            content=content,
        )
        operations, operations_note = _clean_assist_operations(
            result.get("operations", []),
            message_lines=bool(meta.get("message_lines")),
            content=content,
            rules=meta.get("rules") or [],
            policy=meta.get("document_policy", "stock"),
        )
    except ValidationError as error:
        unusable = ProposalUnusable(
            f"proposal failed validation ({error})"
        )
        unusable.tokens = spent["total"]
        raise unusable from error
    usage = result.get("usage", {})
    if context["message_lines"]:
        context_coverage = {
            "scope": "message_lines",
            "lines_shown": len(context["lines"]),
            "total_rows": context["total_rows"],
            "truncated": context["truncated"],
        }
    else:
        context_coverage = {
            "scope": "headers",
            "lines_shown": 0,
            "total_rows": 0,
            "truncated": False,
        }
    return {
        "state": "done",
        "message": result.get("message", "") + operations_note,
        "proposal": {
            "set_constants": constants,
            "drop_lines": drops,
            "dropped_previews": _dropped_previews(content, drops),
            "operations": operations,
        },
        "base_revision": base_revision,
        "context_coverage": context_coverage,
        "tokens": usage.get("total_tokens", 0),
        "replayed": False,
    }


def _failure_response(*, kind, summary, spent_total, base_revision, retriable):
    """Build the stored response for a failed assist attempt."""
    if kind == "unusable":
        message = (
            "I could not turn that into a safe preview change. "
            "Tell me the exact value to set or the line numbers "
            "to drop."
        )
    elif kind == "missing":
        message = (
            "This preview is no longer available, so the request "
            "cannot run. Start a new preview to ask again."
        )
    else:
        message = summary
    return {
        "message": message,
        "proposal": {
            "set_constants": {},
            "drop_lines": [],
            "dropped_previews": [],
            "operations": [],
        },
        "base_revision": base_revision,
        "tokens": spent_total,
        "error": summary,
        "error_kind": kind,
        "retriable": retriable,
    }


def assist_preview(
    *, key: str, user, conversation, idempotency_key: str | None = None
) -> tuple:
    """One proposed-adjustment turn against a pending preview. Reads only.

    Returns (payload, http_status). The model sees a bounded quote of
    the pending preview plus the recent conversation, and may propose
    set-once values or pasted-line drops. Every proposed value and
    line number is validated; the proposal applies only through
    apply_preview_assist after the user confirms it in the chat.

    Keyless sends run inline and return the reply with 200, as
    before. Keyed sends claim the key, queue one worker attempt, and
    return 202 immediately, so no request holds a connection open
    while the provider thinks; the client polls the status endpoint
    for the stored result. A retry after a dropped connection
    replays the completed result instead of paying for a second
    call, a still-running attempt answers 409, and a retry after a
    failed or reclaimed attempt is an explicit new call that may
    spend again.
    """
    prepared = _prepare_assist_turn(
        key=key, user=user, conversation=conversation
    )
    organization = prepared["organization"]
    meta = prepared["meta"]
    content = prepared["content"]
    turns = prepared["turns"]
    base_revision = prepared["base_revision"]
    provider = prepared["provider"]
    if idempotency_key is None:
        spent = {"total": 0}
        reply = _run_assist_attempt(
            organization=organization,
            user=user,
            meta=meta,
            content=content,
            turns=turns,
            base_revision=base_revision,
            provider=provider,
            spent=spent,
        )
        return reply, 200
    record = _claim_assist_key(
        organization=organization,
        user=user,
        preview_key=key,
        request_hash=_assist_request_hash(key, turns, base_revision),
        idempotency_key=idempotency_key,
        turns=turns,
        base_revision=base_revision,
    )
    if record.state == "done":
        stored = dict(record.response or {})
        stored["status"] = "completed"
        stored["replayed"] = True
        stored["attempts"] = record.attempts
        return stored, 200
    from django.db import transaction

    from pipeline.jobs import run_assist_turn

    transaction.on_commit(
        lambda: run_assist_turn.delay(record.id, organization.id)
    )
    # Always 202 for a freshly queued attempt, even when an eager
    # worker already ran it: one stable contract for the client,
    # which polls the status endpoint for the stored outcome.
    return (
        {
            "status": "pending",
            "key": key,
            "idempotency_key": idempotency_key,
            "attempts": record.attempts,
            "accepted": True,
        },
        202,
    )


def assist_turn_status(*, key: str, user, idempotency_key: str) -> dict:
    """Report one keyed assist attempt: pending, completed, failed.

    Unusable proposals surface as completed guidance (message plus an
    empty proposal), exactly like the inline path, so polling never
    traps the user in a retry loop. Uncertain charges surface as
    unknown with explicit credits-log guidance instead of a plain
    failure. Tenant-scoped and bound to the exact preview: unknown
    keys and wrong previews answer 404, other organizations 403
    (like the send path), so polling can never reveal another
    company's request.
    """
    from pipeline.models import AssistIdempotency

    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key or ""):
        raise ValidationError("Unknown or expired assist request.")
    try:
        record = AssistIdempotency.objects.get(
            organization=organization,
            key=idempotency_key,
            preview_key=key,
        )
    except AssistIdempotency.DoesNotExist:
        raise ValidationError("Unknown or expired assist request.") from None
    if record.state == "pending":
        return {
            "status": "pending",
            "key": key,
            "idempotency_key": idempotency_key,
            "attempts": record.attempts,
        }
    if record.state == "failed":
        stored = dict(record.response or {})
        kind = stored.get("error_kind") or "provider"
        if kind == "unusable":
            return {
                "status": "completed",
                "key": key,
                "idempotency_key": idempotency_key,
                "attempts": record.attempts,
                "message": stored.get("message", ""),
                "proposal": stored.get("proposal")
                or {
                    "set_constants": {},
                    "drop_lines": [],
                    "dropped_previews": [],
                    "operations": [],
                },
                "base_revision": stored.get("base_revision", 1),
                "tokens": record.tokens,
                "hint": stored.get("error"),
                "replayed": True,
            }
        return {
            "status": "unknown" if kind == "unknown" else "failed",
            "key": key,
            "idempotency_key": idempotency_key,
            "attempts": record.attempts,
            "tokens": record.tokens,
            "error": stored.get("error") or "The assistant request failed.",
            "error_kind": kind,
            "retryable": bool(stored.get("retriable", True)),
        }
    stored = dict(record.response or {})
    stored["status"] = "completed"
    stored["replayed"] = True
    stored["attempts"] = record.attempts
    return stored


def _ensure_draft(meta: dict) -> dict:
    """Backfill draft keys for previews created before versioning."""
    meta.setdefault("revision", 1)
    meta.setdefault("rules", [])
    meta.setdefault("document_policy", "stock")
    meta.setdefault("history", [])
    meta.setdefault("next_rule_id", 1)
    meta.setdefault("excluded_lines", [])
    meta.setdefault("applied_constants", {})
    return meta


def _summarize_operations(ops: list) -> str:
    kinds: dict = {}
    for op in ops:
        name = op.get("op") if isinstance(op, dict) else "?"
        kinds[name] = kinds.get(name, 0) + 1
    return ", ".join(f"{count}x {name}" for name, count in sorted(kinds.items()))


def _draft_block(meta: dict, *, lines, records, unresolved, sections) -> dict:
    _lines = lines  # derivation inputs stay explicit at call sites.
    return {
        "revision": meta.get("revision", 1),
        "document_policy": meta.get("document_policy", "stock"),
        "rules": meta.get("rules", []),
        "excluded_lines": meta.get("excluded_lines", []),
        "records": records,
        "unresolved": unresolved,
        "sections": sections,
        "history": meta.get("history", []),
    }


def apply_preview_assist(
    *,
    key: str,
    user,
    set_constants,
    drop_lines,
    operations=None,
    revision=None,
    base_revision=None,
) -> dict:
    """Apply user-confirmed operations to the preview draft.

    Legacy set/drop values fold into typed operations so one engine
    validates everything. The original bytes are never rewritten:
    exclusions are recorded rules and headers/detection stay as first
    read. Every change bumps the revision; a stale writer gets a
    DraftConflictError instead of silently overwriting. When the
    caller passes the proposal's base_revision, it must still match
    the draft: a proposal computed before newer confirmed decisions
    cannot silently supersede them.
    """
    with _preview_lock(key):
        from pipeline.draft import (
            DraftConflict,
            DraftError,
            apply_operations,
            derive_records,
        )
        from pipeline.jobs import record_audit

        meta = _ensure_draft(_load_preview(key))
        organization = authorize_organization_access(user, meta["organization_id"])
        require_organization_role(user, meta["organization_id"])
        current = meta.get("revision", 1)
        if base_revision is not None and base_revision != current:
            raise DraftConflictError(
                current,
                base_revision,
                detail=(
                    f"This proposal was made for draft revision "
                    f"{base_revision}, but the draft is now at revision "
                    f"{current}. Ask the assistant again so the proposal "
                    "is recomputed against the latest confirmed rules."
                ),
            )
        try:
            content = _adapter().read_bytes(f"previews/{key}.bin")
        except FileNotFoundError:
            raise ValidationError("Unknown or expired preview.") from None
        message_lines = bool(meta.get("message_lines"))
        constants = _clean_assist_constants(set_constants)
        drops = _clean_assist_drops(
            drop_lines, message_lines=message_lines, content=content
        )
        ops = [dict(op) for op in (operations or [])]
        for name, value in constants.items():
            ops.append({"op": "global", "field": name, "value": value})
        if drops:
            ops.append({"op": "exclude_lines", "lines": drops})
        if not ops:
            raise ValidationError("Nothing to apply.")
        if not message_lines and any(
            (op.get("op") if isinstance(op, dict) else None) != "global" for op in ops
        ):
            raise ValidationError("Only set values apply to table previews.")
        lines = _content_lines(content)
        try:
            rules, policy = apply_operations(
                ops,
                lines=lines,
                rules=meta.get("rules") or [],
                revision_actual=meta.get("revision", 1),
                revision_expected=revision,
                policy=meta.get("document_policy", "stock"),
            )
        except DraftConflict as exc:
            raise DraftConflictError(meta.get("revision", 1), revision) from exc
        except DraftError as exc:
            raise ValidationError(str(exc)) from exc
        next_id = meta.get("next_rule_id", 1)
        for rule in rules:
            if "id" not in rule:
                rule["id"] = f"r{next_id}"
                next_id += 1
        if message_lines:
            records, unresolved, sections = derive_records(lines, rules)
            if not records:
                raise ValidationError("That would leave no readable lines.")
        else:
            records, unresolved, sections = [], [], []
        # Assisted constants derive from the current active rules every
        # revision: a removed rule stops contributing immediately instead
        # of lingering from the previous revision. Explicit user values
        # stay separate: confirm merges per-request constants over these.
        applied = {
            rule["field"]: rule["value"]
            for rule in rules
            if rule["kind"] == "global"
        }
        excluded = sorted(
            {
                number
                for rule in rules
                if rule["kind"] == "exclude_lines"
                for number in rule["lines"]
            }
        )
        new_revision = meta.get("revision", 1) + 1
        history = list(meta.get("history", []))
        history.append(
            {
                "revision": new_revision,
                "at": datetime.now(timezone.utc).isoformat(),
                "by": user.id,
                "summary": _summarize_operations(ops),
                "operations": ops,
            }
        )
        meta.update(
            {
                "revision": new_revision,
                "rules": rules,
                "document_policy": policy,
                "next_rule_id": next_id,
                "history": history[-50:],
                "applied_constants": applied,
                "excluded_lines": excluded,
            }
        )
        _adapter().save_bytes(
            f"previews/{key}.json", json.dumps(meta).encode("utf-8")
        )
        headers = meta["headers"]
        detected = meta["detected"]
        sources = meta["sources"]
        missing = [
            name
            for name in REQUIRED_COLUMNS
            if detected.get(name) is None and applied.get(name) in (None, "")
        ]
        if message_lines:
            total_rows = len(records)
        else:
            total_rows = _preview_total_rows(key, meta, content)
        proposed = {} if message_lines else dict(detected)
        samples = _render_samples(
            content,
            meta["filename"],
            proposed,
            dict(applied),
            [],
            organization.default_currency,
            message_fallback=message_lines,
            sheet_name=meta.get("sheet"),
            exclude_lines=excluded,
            draft_rules=rules,
            document_policy=policy,
        )
        record_audit(
            organization.id,
            user,
            "preview_assisted",
            "preview",
            key,
            {
                "set_constants": sorted(constants),
                "dropped_lines": drops,
                "operations": len(ops),
                "revision": new_revision,
            },
        )
        return {
            "applied": {
                "set_constants": constants,
                "drop_lines": drops,
                "dropped_previews": _dropped_previews(content, drops),
                "operations": ops,
                "revision": new_revision,
            },
            "preview": {
                "key": key,
                "filename": meta["filename"][:255],
                "extension": meta["extension"],
                "size_bytes": len(content),
                "sha256": meta["sha256"],
                "headers": _header_entries(headers),
                "detected": detected,
                "sources": sources,
                "missing": missing,
                "message_lines": message_lines,
                "total_rows": total_rows,
                "memory": None,
                "proposed": proposed,
                "proposed_constants": dict(applied),
                "proposed_ignored": [],
                "samples": samples,
                "sheets": _sheet_block(
                    meta.get("sheets") or [], meta.get("sheet")
                ),
                "draft": _draft_block(
                    meta,
                    lines=lines,
                    records=records,
                    unresolved=unresolved,
                    sections=sections,
                ),
            },
        }
