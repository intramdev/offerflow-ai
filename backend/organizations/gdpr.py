"""GDPR self-service: full organization export and account erasure.

The export is a ZIP archive: data.json holds every database row scoped
to the organization, plus the original uploaded bytes and the generated
artifacts. Erasure removes the organization, its stored bytes, and any
member login left without an organization (shared or staff accounts
survive); runs still in flight block erasure loudly.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from common.storage import LocalFileAdapter

ACTIVE_RUN_STATUSES = frozenset(
    {"uploaded", "parsing", "interpretation", "validation", "exporting"}
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _coerce(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _row(instance: models.Model) -> dict:
    data = {}
    for field in instance._meta.concrete_fields:
        data[field.name] = _coerce(field.value_from_object(instance))
    return data


def _section(queryset) -> list:
    return [_row(item) for item in queryset]


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (name or "file").rsplit("/", 1)[-1])
    return cleaned[:120] or "file"


def _user_card(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "date_joined": _coerce(user.date_joined),
    }


def build_export_payload(organization) -> dict:
    """Return every organization-scoped row as JSON-safe structures."""
    from organizations.models import Invitation, Membership, UserPreference
    from pipeline.models import (
        AuditEvent,
        Category,
        ColumnMappingMemory,
        CustomerQuote,
        ExportArtifact,
        ImportRun,
        NormalizedOffer,
        PricingRule,
        ProductAlias,
        ReviewDecision,
        ReviewRequest,
        SourceFile,
        SourceRow,
        SpendAllowance,
        Supplier,
        SupplierNote,
        UsageEvent,
        ValidationResult,
    )

    memberships = Membership.objects.filter(
        organization=organization
    ).select_related("user")
    preferences = {
        preference.user_id: preference.datetime_format
        for preference in UserPreference.objects.filter(
            user_id__in=[entry.user_id for entry in memberships]
        )
    }
    files = SourceFile.objects.filter(organization=organization)
    runs = ImportRun.objects.filter(organization=organization)
    return {
        "exported_at": datetime.now().astimezone().isoformat(),
        "organization": {
            "id": organization.id,
            "name": organization.name,
            "default_currency": organization.default_currency,
            "destination_currency": organization.destination_currency,
            "created_at": _coerce(organization.created_at),
        },
        "members": [
            {
                "user": _user_card(entry.user),
                "role": entry.role,
                "joined_at": _coerce(entry.joined_at),
                "datetime_format": preferences.get(entry.user_id),
            }
            for entry in memberships
        ],
        "invitations": _section(
            Invitation.objects.filter(organization=organization)
        ),
        "supplier_files": _section(files),
        "import_runs": _section(runs),
        "source_rows": _section(
            SourceRow.objects.filter(organization=organization)
        ),
        "offers": _section(
            NormalizedOffer.objects.filter(organization=organization)
        ),
        "validation_results": _section(
            ValidationResult.objects.filter(organization=organization)
        ),
        "review_requests": _section(
            ReviewRequest.objects.filter(organization=organization)
        ),
        "review_decisions": _section(
            ReviewDecision.objects.filter(organization=organization)
        ),
        "export_artifacts": _section(
            ExportArtifact.objects.filter(organization=organization)
        ),
        "customer_quotes": _section(
            CustomerQuote.objects.filter(organization=organization)
        ),
        "categories": _section(
            Category.objects.filter(organization=organization)
        ),
        "pricing_rules": _section(
            PricingRule.objects.filter(organization=organization)
        ),
        "suppliers": _section(
            Supplier.objects.filter(organization=organization)
        ),
        "supplier_notes": _section(
            SupplierNote.objects.filter(supplier__organization=organization)
        ),
        "product_aliases": _section(
            ProductAlias.objects.filter(organization=organization)
        ),
        "mapping_memory": _section(
            ColumnMappingMemory.objects.filter(organization=organization)
        ),
        "usage_events": _section(
            UsageEvent.objects.filter(organization=organization)
        ),
        "audit_events": _section(
            AuditEvent.objects.filter(organization=organization)
        ),
        "spend_allowances": _section(
            SpendAllowance.objects.filter(organization=organization)
        ),
    }


def build_export_archive(organization) -> tuple[bytes, dict]:
    """Return (zip bytes, manifest) for the organization."""
    from pipeline.models import ExportArtifact, SourceFile

    adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
    payload = build_export_payload(organization)
    manifest = {"uploads": [], "artifacts": []}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "data.json",
            json.dumps(payload, indent=2, ensure_ascii=False),
        )
        for source_file in SourceFile.objects.filter(
            organization=organization
        ).order_by("id"):
            entry = {
                "id": source_file.id,
                "original_name": source_file.original_name,
                "included": False,
            }
            name = (
                f"uploads/{source_file.id}-"
                f"{_safe_filename(source_file.original_name)}"
            )
            try:
                archive.writestr(name, adapter.read_bytes(source_file.storage_key))
            except Exception as exc:
                entry["reason"] = (
                    f"{type(exc).__name__}: stored bytes unreadable."
                )
            else:
                entry["included"] = True
                entry["archive_path"] = name
            manifest["uploads"].append(entry)
        for artifact in ExportArtifact.objects.filter(
            organization=organization
        ).order_by("id"):
            entry = {
                "id": artifact.id,
                "format": artifact.format,
                "included": False,
            }
            name = f"artifacts/{artifact.id}.{artifact.format}"
            try:
                archive.writestr(name, adapter.read_bytes(artifact.storage_key))
            except Exception as exc:
                entry["reason"] = (
                    f"{type(exc).__name__}: stored bytes unreadable."
                )
            else:
                entry["included"] = True
                entry["archive_path"] = name
            manifest["artifacts"].append(entry)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buffer.getvalue(), manifest


def erase_organization(*, organization) -> dict:
    """Delete the organization, its bytes, and orphaned member logins."""
    from django.contrib.auth import get_user_model

    from pipeline.models import ExportArtifact, ImportRun, SourceFile

    active = ImportRun.objects.filter(
        organization=organization, status__in=ACTIVE_RUN_STATUSES
    )
    if active.exists():
        raise ValidationError(
            "Runs are still processing; wait until they finish, "
            "then delete again."
        )
    adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
    removed_bytes = 0
    for key in list(
        SourceFile.objects.filter(organization=organization).values_list(
            "storage_key", flat=True
        )
    ) + list(
        ExportArtifact.objects.filter(organization=organization).values_list(
            "storage_key", flat=True
        )
    ):
        try:
            if adapter.delete(key):
                removed_bytes += 1
        except Exception:
            continue
    from organizations.models import Membership

    member_ids = list(
        Membership.objects.filter(organization=organization).values_list(
            "user_id", flat=True
        )
    )
    name = organization.name
    organization.delete()
    orphans = (
        get_user_model()
        .objects.filter(
            id__in=member_ids,
            org_memberships__isnull=True,
            is_staff=False,
            is_superuser=False,
        )
        .distinct()
    )
    deleted_users = orphans.count()
    orphans.delete()
    return {
        "organization": name,
        "stored_files_removed": removed_bytes,
        "member_logins_deleted": deleted_users,
    }
