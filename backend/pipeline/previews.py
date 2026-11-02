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
from django.core.exceptions import PermissionDenied, ValidationError

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
    MESSAGE_COLUMN,
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


def _derive_sheet(content, original_name, limits, message_fallback):
    """Parse preview bytes into headers, map, sources, missing, row count.

    Shared by creation and assist-apply so re-derived previews always
    match what a fresh upload of the same bytes would show.
    """
    try:
        sheet = parse_upload(
            content,
            original_name,
            max_rows=limits["max_rows"],
            max_cols=limits["max_cols"],
            header_scan_rows=limits["header_scan_rows"],
            message_fallback=message_fallback,
        )
        return (
            sheet.headers,
            sheet.column_map,
            sheet.sources,
            [],
            len(sheet.rows),
        )
    except MappingRequired as exc:
        return (
            exc.headers,
            exc.column_map,
            exc.sources,
            exc.missing,
            len(exc.rows),
        )


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


def _render_samples(content: bytes, filename: str, mapping: dict,
                    constants=None, ignored=None, default_currency="",
                    message_fallback: bool = False) -> dict:
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
            message_fallback=message_fallback,
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
    original_name = getattr(uploaded_file, "name", "") or ""
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
    single-column pastes become message rows (one per line) that the
    interpretation lane reads, flagged message_lines for the UI.
    """
    content = (text or "").encode("utf-8")
    if not content.strip():
        raise ValidationError("Paste some text first.")
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
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    digest = hashlib.sha256(content).hexdigest()
    _reject_duplicate(
        organization=organization, user=user, original_name=original_name, digest=digest
    )
    headers, detected, sources, missing, total_rows = _derive_sheet(
        content, original_name, limits, message_fallback
    )
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
        "message_lines": message_lines,
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
        message_fallback=message_fallback,
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
        "message_lines": message_lines,
        "total_rows": total_rows,
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
            message_fallback=meta.get("message_lines") is True,
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
    run_mapping = {
        "headers": meta["headers"],
        "detected": meta["detected"],
        "confirmed": submitted,
        "constants": clean_constants,
        "ignored": clean_ignored,
    }
    if message_lines:
        run_mapping["free_text"] = True
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


class AssistRefused(Exception):
    """The guard refused the turn before any token was spent."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    for number, text in enumerate(all_lines, start=1):
        stripped = text.strip()
        if not stripped:
            continue
        if len(numbered) >= ASSIST_PREVIEW_LINES or chars >= ASSIST_PREVIEW_CHARS:
            truncated = True
            break
        numbered.append({"line": number, "text": stripped[:200]})
        chars += len(stripped)
    context.update(
        {
            "lines": numbered,
            "line_count": len(all_lines),
            "total_rows": sum(1 for text in all_lines if text.strip()),
            "truncated": truncated,
        }
    )
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


def assist_preview(*, key: str, user, conversation) -> dict:
    """One proposed-adjustment turn against a pending preview. Reads only.

    The model sees a bounded quote of the pending preview plus the
    recent conversation, and may propose set-once values or pasted-line
    drops. Every proposed value and line number is validated here; the
    proposal applies only through apply_preview_assist after the user
    confirms it in the chat.
    """
    from common.provider import ProviderNotConfigured, get_provider
    from organizations.models import UserPreference
    from pipeline.assistant_guard import inspect_question
    from pipeline.models import AssistantFlag

    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    turns = _clean_assist_turns(conversation)
    if UserPreference.objects.filter(
        user=user, assistant_banned=True
    ).exists():
        raise PermissionDenied("Assistant access is disabled for your account.")
    reason = inspect_question(turns[-1]["content"])
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
    context = _assist_preview_context(
        meta=meta, content=content, organization=organization
    )
    question = turns[-1]["content"][:120]
    provider_model = getattr(getattr(provider, "_config", None), "model", "")
    if not isinstance(provider_model, str):
        provider_model = ""
    spent = {"total": 0}

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
        constants = _clean_assist_constants(result.get("set_constants", {}))
        drops = _clean_assist_drops(
            result.get("drop_lines", []),
            message_lines=bool(meta.get("message_lines")),
            content=content,
        )
    except ValidationError as error:
        from common.meta import ProposalUnusable

        unusable = ProposalUnusable(f"proposal failed validation ({error})")
        unusable.tokens = spent["total"]
        raise unusable from error
    usage = result.get("usage", {})
    return {
        "message": result.get("message", ""),
        "proposal": {
            "set_constants": constants,
            "drop_lines": drops,
            "dropped_previews": _dropped_previews(content, drops),
        },
        "tokens": usage.get("total_tokens", 0),
    }


def apply_preview_assist(*, key: str, user, set_constants, drop_lines) -> dict:
    """Apply user-confirmed assist values: re-quote bytes, re-derive preview.

    Dropped lines are removed from the quoted bytes and the preview is
    re-derived exactly as a fresh upload of the remaining bytes, so the
    lane (message or table) always matches the content. Set values merge
    into the preview's applied constants.
    """
    from pipeline.jobs import record_audit

    meta = _load_preview(key)
    organization = authorize_organization_access(user, meta["organization_id"])
    constants = _clean_assist_constants(set_constants)
    content = _adapter().read_bytes(f"previews/{key}.bin")
    drops = _clean_assist_drops(
        drop_lines, message_lines=bool(meta.get("message_lines")), content=content
    )
    if not constants and not drops:
        raise ValidationError("Nothing to apply.")
    previews = _dropped_previews(content, drops)
    if drops:
        dropped = set(drops)
        kept = [
            text
            for number, text in enumerate(_content_lines(content), start=1)
            if number not in dropped
        ]
        content = "\n".join(kept).encode("utf-8")
        _adapter().save_bytes(f"previews/{key}.bin", content)
    try:
        headers, detected, sources, missing, total_rows = _derive_sheet(
            content, meta["filename"], _limits(), bool(meta.get("message_lines"))
        )
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    message_lines = detected == {MESSAGE_COLUMN: 0}
    applied = {**meta.get("applied_constants", {}), **constants}
    meta.update(
        {
            "sha256": hashlib.sha256(content).hexdigest(),
            "headers": headers,
            "detected": detected,
            "sources": sources,
            "message_lines": message_lines,
            "applied_constants": applied,
        }
    )
    _adapter().save_bytes(f"previews/{key}.json", json.dumps(meta).encode("utf-8"))
    proposed = {} if message_lines else dict(detected)
    samples = _render_samples(
        content,
        meta["filename"],
        proposed,
        dict(applied),
        [],
        organization.default_currency,
        message_fallback=message_lines,
    )
    record_audit(
        organization.id,
        user,
        "preview_assisted",
        "preview",
        key,
        {"set_constants": sorted(constants), "dropped_lines": drops},
    )
    return {
        "applied": {
            "set_constants": constants,
            "drop_lines": drops,
            "dropped_previews": previews,
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
        },
    }
