"""Suppliers: import auto-discovery, favorites, ratings, notes.

Every import names its suppliers; the first sighting creates the
company's record and later sightings reuse it, matched up to case.
Favorites are an explicit ordering (rank 1 is the favorite) kept
contiguous by the reorder call; everyone else is unranked.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from pipeline.matching import normalize_supplier
from pipeline.models import Supplier, SupplierNote

RATING_FIELDS = (
    "rating_shipping",
    "rating_quality",
    "rating_payment",
    "rating_quantity",
    "rating_rma",
)


def ensure_suppliers(organization_id: int, names) -> list[Supplier]:
    """Create missing supplier records for an import; reuse the rest.

    Blank names are skipped. Matching is up to case; the first-seen
    spelling becomes the display name. Idempotent per name.
    """
    seen: dict[str, Supplier] = {}
    for raw in names or []:
        key = normalize_supplier(raw)
        if not key or key in seen:
            continue
        display = raw.strip()[:255] if isinstance(raw, str) else ""
        if not display:
            continue
        supplier = (
            Supplier.objects.filter(organization_id=organization_id, name__iexact=key)
            .order_by("id")
            .first()
        )
        if supplier is None:
            try:
                supplier = Supplier.objects.create(
                    organization_id=organization_id, name=display
                )
            except IntegrityError:
                supplier = Supplier.objects.filter(
                    organization_id=organization_id, name__iexact=key
                ).order_by("id").first()
        if supplier is not None:
            seen[key] = supplier
    return list(seen.values())


def _clean_name(name) -> str:
    cleaned = (name or "").strip() if isinstance(name, str) else ""
    if not cleaned:
        raise ValidationError("A supplier needs a name.")
    return cleaned[:255]


def _clean_rating(field: str, value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be 1 to 5 stars or empty.")
    if not 1 <= value <= 5:
        raise ValidationError(f"{field} must be 1 to 5 stars or empty.")
    return value


def _clean_fee(value):
    if value is None or value == "":
        return None
    try:
        fee = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValidationError("Customs fee must be a percentage number.")
    if not fee.is_finite() or fee < 0 or fee > 100:
        raise ValidationError("Customs fee must be between 0 and 100.")
    return fee


def _clean_details(data: dict) -> dict:
    """Validate editable supplier fields (name handled by callers)."""
    cleaned: dict = {}
    if "location" in data:
        location = data.get("location")
        if location is not None and not isinstance(location, str):
            raise ValidationError("Location must be text.")
        cleaned["location"] = (location or "").strip()[:255]
    if "customs_required" in data:
        cleaned["customs_required"] = bool(data.get("customs_required"))
    if "customs_fee_percent" in data:
        cleaned["customs_fee_percent"] = _clean_fee(data.get("customs_fee_percent"))
    for field in RATING_FIELDS:
        if field in data:
            cleaned[field] = _clean_rating(field, data.get(field))
    return cleaned


def create_supplier(*, organization, user, data: dict) -> Supplier:
    """Create a supplier by hand. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, organization.id)
    name = _clean_name(data.get("name"))
    if organization.suppliers.filter(name__iexact=name).exists():
        raise ValidationError(f"A supplier named {name!r} already exists.")
    try:
        supplier = Supplier.objects.create(
            organization=organization, name=name, **_clean_details(data)
        )
    except IntegrityError:
        raise ValidationError(
            f"A supplier named {name!r} already exists."
        ) from None
    record_audit(
        organization.id, user, "supplier_created", "supplier", supplier.id
    )
    return supplier


def update_supplier(*, supplier: Supplier, user, data: dict) -> Supplier:
    """Edit a supplier's details. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, supplier.organization_id)
    if "name" in data:
        name = _clean_name(data.get("name"))
        if (
            supplier.organization.suppliers.filter(name__iexact=name)
            .exclude(pk=supplier.pk)
            .exists()
        ):
            raise ValidationError(f"A supplier named {name!r} already exists.")
        supplier.name = name
    changed = ["name"] if "name" in data else []
    for field, value in _clean_details(data).items():
        setattr(supplier, field, value)
        changed.append(field)
    if changed:
        try:
            supplier.save(update_fields=changed)
        except IntegrityError:
            raise ValidationError(
                f"A supplier named {supplier.name!r} already exists."
            ) from None
    record_audit(
        supplier.organization_id, user, "supplier_updated", "supplier", supplier.id
    )
    return supplier


def delete_supplier(*, supplier: Supplier, user) -> None:
    """Delete a supplier and its notes. Operating roles only; audited.

    Imports keep working: the next file naming the supplier simply
    re-creates a blank record.
    """
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, supplier.organization_id)
    organization_id = supplier.organization_id
    supplier_id = supplier.id
    supplier.delete()
    record_audit(
        organization_id, user, "supplier_deleted", "supplier", supplier_id
    )


def set_favorite_order(*, organization, user, ordered_ids: list) -> list[Supplier]:
    """Rank favorites 1..N in the given order; unlisted lose rank.

    Every id must be a company supplier, listed exactly once.
    Operating roles only; audited.
    """
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, organization.id)
    if not isinstance(ordered_ids, list):
        raise ValidationError("ordered_ids must be a list of supplier ids.")
    if any(
        isinstance(pk, bool) or not isinstance(pk, int) for pk in ordered_ids
    ):
        raise ValidationError("ordered_ids must be a list of supplier ids.")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ValidationError("ordered_ids lists a supplier twice.")
    known = set(
        organization.suppliers.values_list("id", flat=True)
    )
    unknown = [pk for pk in ordered_ids if pk not in known]
    if unknown:
        raise ValidationError(f"Unknown suppliers ordered: {sorted(unknown)}.")
    suppliers = {s.id: s for s in organization.suppliers.all()}
    for rank, pk in enumerate(ordered_ids, start=1):
        suppliers[pk].favorite_rank = rank
    for pk, supplier in suppliers.items():
        if pk not in ordered_ids:
            supplier.favorite_rank = None
    Supplier.objects.bulk_update(
        list(suppliers.values()), ["favorite_rank"]
    )
    record_audit(
        organization.id,
        user,
        "supplier_order_set",
        "supplier",
        "",
        {"ordered_ids": ordered_ids},
    )
    return [suppliers[pk] for pk in ordered_ids]


def add_supplier_note(*, supplier: Supplier, user, text: str) -> SupplierNote:
    """Append a timestamped note. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, supplier.organization_id)
    cleaned = (text or "").strip() if isinstance(text, str) else ""
    if not cleaned:
        raise ValidationError("A note needs some text.")
    note = SupplierNote.objects.create(
        supplier=supplier, author=user, text=cleaned
    )
    record_audit(
        supplier.organization_id, user, "supplier_note_added", "supplier", supplier.id
    )
    return note


def delete_supplier_note(*, note: SupplierNote, user) -> None:
    """Delete one note. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, note.supplier.organization_id)
    organization_id = note.supplier.organization_id
    supplier_id = note.supplier_id
    note.delete()
    record_audit(
        organization_id, user, "supplier_note_deleted", "supplier", supplier_id
    )


__all__ = [
    "RATING_FIELDS",
    "add_supplier_note",
    "create_supplier",
    "delete_supplier",
    "delete_supplier_note",
    "ensure_suppliers",
    "set_favorite_order",
    "update_supplier",
]
