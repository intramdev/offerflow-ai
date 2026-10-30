"""Upload intake: verify, store, create file plus run, enqueue after commit."""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError

from common.storage import LocalFileAdapter
from pipeline.jobs import enqueue_run, record_audit
from pipeline.models import ImportRun, RuleSetVersion, SourceFile
from pipeline.parsing import UnsupportedFile, check_upload_safety

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    base = (name or "").rsplit("/", 1)[-1].strip()
    stem, dot, extension = base.rpartition(".")
    cleaned = SAFE_NAME_RE.sub("_", stem[:100]) or "upload"
    suffix = f".{extension.lower()[:8]}" if dot and extension else ".bin"
    return f"{cleaned}{suffix}"


def _reject_duplicate(*, organization, user, original_name: str, digest: str) -> None:
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


def store_upload_run(
    *, organization, user, content: bytes, original_name: str, mapping: dict
) -> ImportRun:
    """Persist validated bytes plus file/run rows, then enqueue processing."""
    digest = hashlib.sha256(content).hexdigest()
    adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
    key = adapter.build_key(
        str(organization.id),
        "uploads",
        f"{digest[:16]}-{uuid.uuid4().hex[:8]}-{_safe_filename(original_name)}",
    )
    adapter.save_bytes(key, content)
    rule = RuleSetVersion.objects.filter(is_active=True).order_by("-created_at").first()
    if rule is None:
        raise ValidationError("No active rule version.")
    source_file = SourceFile.objects.create(
        organization=organization,
        uploaded_by=user,
        original_name=original_name[:255],
        size_bytes=len(content),
        sha256=digest,
        storage_key=key,
    )
    from pipeline.fx import snapshot_upload_rates

    snapshot_upload_rates(source_file=source_file, organization=organization)
    run = ImportRun.objects.create(
        organization=organization,
        source_file=source_file,
        rule_version=rule,
        mapping=mapping,
    )
    record_audit(organization.id, user, "upload_created", "run", run.id)
    enqueue_run(run.id)
    return run


def create_upload(*, organization, user, uploaded_file) -> ImportRun:
    """Persist a direct upload and start its run. Raises on any refusal."""
    original_name = getattr(uploaded_file, "name", "") or ""
    content = uploaded_file.read()
    if not content:
        raise ValidationError("Empty file.")
    try:
        check_upload_safety(
            content,
            original_name,
            max_bytes=settings.OFFERFLOW_UPLOAD_MAX_BYTES,
            max_uncompressed_bytes=settings.OFFERFLOW_MAX_UNCOMPRESSED_BYTES,
        )
    except UnsupportedFile as exc:
        raise ValidationError(str(exc)) from exc
    digest = hashlib.sha256(content).hexdigest()
    _reject_duplicate(
        organization=organization, user=user, original_name=original_name, digest=digest
    )
    return store_upload_run(
        organization=organization,
        user=user,
        content=content,
        original_name=original_name,
        mapping={},
    )


def rename_upload(*, source_file: SourceFile, user, original_name: str) -> SourceFile:
    """Rename an upload's display name. Stored bytes are untouched: the
    storage key never derives from this name, so history and artifacts
    keep working under the new label."""
    from organizations.context import set_tenant_context
    from organizations.services import require_organization_role

    require_organization_role(user, source_file.organization_id)
    set_tenant_context(source_file.organization_id, user.id)
    old_name = source_file.original_name
    source_file.original_name = original_name[:255]
    source_file.save(update_fields=["original_name"])
    record_audit(
        source_file.organization_id,
        user,
        "file_renamed",
        "file",
        source_file.id,
        {"from": old_name, "to": source_file.original_name},
    )
    return source_file


def delete_upload(*, source_file: SourceFile, user) -> None:
    """Delete an upload with its run, offers, and stored bytes.

    Audit events survive (they target by id string); artifact and upload
    bytes are removed best-effort after the rows are gone, so a storage
    hiccup can only orphan bytes, never revive rows.
    """
    from organizations.context import set_tenant_context
    from organizations.services import require_organization_role

    require_organization_role(user, source_file.organization_id)
    set_tenant_context(source_file.organization_id, user.id)
    keys = [source_file.storage_key]
    try:
        run = source_file.run
    except ImportRun.DoesNotExist:
        run = None
    if run is not None:
        keys.extend(run.artifacts.values_list("storage_key", flat=True))
    record_audit(
        source_file.organization_id,
        user,
        "upload_deleted",
        "file",
        source_file.id,
        {
            "name": source_file.original_name,
            "sha256": source_file.sha256,
            "size_bytes": source_file.size_bytes,
        },
    )
    source_file.delete()
    adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
    for key in keys:
        with contextlib.suppress(OSError):
            adapter.delete(key)
