"""Cross-supplier product matching with per-company learning memory.

Blocking is strict: offers group only with equal brand
(case-insensitive), equal capacity, and equal grade — a used
phone's grade is part of its identity. Within a block, model-name
spellings resolve through three lanes, in order:

1. Learned aliases: this company's confirmed (supplier, spelling,
   brand, capacity, grade) tuples re-match instantly.
2. Normalization: spellings equal up to case and punctuation join.
3. The model lane: unknown spellings go to the provider, which
   clusters them with confidence. The fixture cannot judge
   similarity, so simulated runs leave unknowns provisional.

Categories resolve per product group, in order: a unanimous
file-level mapping, the remembered alias categories, then the
model lane. Anything still unsure — spelling or category — becomes
a group flagged for the visual reconcile preview instead of a
guess. Reconcile decisions are validated (same block per group,
every offer placed exactly once, known categories only) and then
remembered, so each company's matching improves with every
confirmed quote.
"""

from __future__ import annotations

import re
from collections import Counter

from django.core.exceptions import ValidationError

from common.meta import MatchUnusable
from common.provider import get_provider
from pipeline.models import (
    Category,
    NormalizedOffer,
    OfferStatus,
    ProductAlias,
    SourceFile,
)

# A model-lane cluster at or above this confidence auto-groups;
# anything lower waits for human reconcile.
MATCH_AUTO_CONFIDENCE = 0.85


def normalize_brand(value) -> str:
    """Brand blocking key: exact up to case and surrounding space."""
    return (value or "").strip().lower() if isinstance(value, str) else ""


def normalize_supplier(value) -> str:
    """Supplier memory key: exact up to case and surrounding space."""
    return (value or "").strip().lower() if isinstance(value, str) else ""


def normalize_grade(value) -> str:
    """Grade blocking key: exact up to case and surrounding space."""
    return (value or "").strip().lower() if isinstance(value, str) else ""


def normalize_color(value) -> str:
    """Color blocking key: exact up to case and surrounding space."""
    return (value or "").strip().lower() if isinstance(value, str) else ""


def normalize_alias(value) -> str:
    """Model-spelling key: lowercase alphanumeric only."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _display_brand(offers: list) -> str:
    """Most common raw brand spelling in the group."""
    raws = [offer.brand for offer in offers if (offer.brand or "").strip()]
    if not raws:
        return ""
    return Counter(raws).most_common(1)[0][0]


def _common_spelling(offers: list) -> str:
    """Most common raw model spelling (ties break by first seen)."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for offer in offers:
        raw = offer.model_name or ""
        if raw not in counts:
            counts[raw] = 0
            order.append(raw)
        counts[raw] += 1
    best = order[0]
    for raw in order[1:]:
        if counts[raw] > counts[best]:
            best = raw
    return best


def propose_groups(*, organization, offers: list, user=None) -> dict:
    """Group ok offers into same-product groups with confidence.

    Returns {"provider", "simulated", "groups", "notes",
    "needs_review"}. Groups carry offer ids, a proposed canonical
    model name, a resolved category, and the lanes that decided them.
    """
    provider = get_provider(organization_id=organization.id)
    provider_model = getattr(getattr(provider, "_config", None), "model", "")
    if not isinstance(provider_model, str):
        provider_model = ""
    categories = list(
        Category.objects.filter(organization=organization).order_by("name")
    )
    file_ids = {offer.run.source_file_id for offer in offers}
    file_categories = dict(
        SourceFile.objects.filter(id__in=file_ids).values_list("id", "category_id")
    )
    blocks: dict[tuple, list] = {}
    for offer in offers:
        key = (
            normalize_brand(offer.brand),
            offer.capacity_gb,
            normalize_grade(offer.grade),
            normalize_color(offer.color),
        )
        blocks.setdefault(key, []).append(offer)
    groups: list[dict] = []
    notes: list[str] = []
    for (brand_norm, capacity, grade_norm, color_norm), members in sorted(
        blocks.items(),
        key=lambda item: (
            item[0][0],
            item[0][1] is None,
            item[0][1],
            item[0][2],
            item[0][3],
        ),
    ):
        groups.extend(
            _match_block(
                organization=organization,
                provider=provider,
                provider_model=provider_model,
                brand_norm=brand_norm,
                capacity=capacity,
                grade_norm=grade_norm,
                color_norm=color_norm,
                members=members,
                categories=categories,
                file_categories=file_categories,
                notes=notes,
                user=user,
            )
        )
    groups.sort(
        key=lambda group: (
            normalize_brand(group["brand"]),
            group["capacity_gb"] is None,
            group["capacity_gb"] or 0,
            normalize_grade(group.get("grade") or ""),
            normalize_color(group.get("color") or ""),
            normalize_alias(group["canonical_model"]),
        )
    )
    return {
        "provider": provider.name,
        "simulated": provider.is_simulated,
        "groups": groups,
        "notes": notes,
        "needs_review": any(
            group["needs_review"] or group["category_review"] for group in groups
        ),
    }


def _match_block(
    *,
    organization,
    provider,
    provider_model,
    brand_norm,
    capacity,
    grade_norm,
    color_norm,
    members,
    categories,
    file_categories,
    notes,
    user=None,
) -> list[dict]:
    """Match one (brand, capacity, grade, color) block; return groups."""
    memory: dict[tuple[str, str], ProductAlias] = {}
    if capacity is not None:
        for alias in ProductAlias.objects.filter(
            organization=organization,
            brand_norm=brand_norm,
            capacity_gb=capacity,
            grade_norm=grade_norm,
        ).select_related("category"):
            memory[(alias.supplier_norm, alias.alias_norm)] = alias
    matchable, provisional = [], []
    for offer in members:
        if not normalize_alias(offer.model_name) or capacity is None:
            provisional.append(offer)
        else:
            matchable.append(offer)
    groups: list[dict] = []
    for offer in provisional:
        reason = "no model name" if capacity is not None else "no capacity"
        groups.append(
            _group(
                [offer],
                canonical=offer.model_name or "",
                confidence=0.0,
                source="provisional",
                needs_review=True,
            )
        )
        notes.append(
            f"{_label(offer)}: {reason}, needs reconcile."
        )
    by_canonical: dict[str, list] = {}
    offer_alias: dict[int, ProductAlias] = {}
    unknown: list = []
    for offer in matchable:
        key = (
            normalize_supplier(offer.supplier),
            normalize_alias(offer.model_name),
        )
        alias = memory.get(key)
        if alias is None:
            unknown.append(offer)
        else:
            offer_alias[offer.id] = alias
            by_canonical.setdefault(alias.canonical_model, []).append(offer)
    assembled: list[tuple[list, str, str]] = []
    seeded: dict[str, int] = {}
    for canonical, grouped in by_canonical.items():
        seeded[normalize_alias(canonical)] = len(assembled)
        assembled.append((list(grouped), canonical, "memory"))
    by_norm: dict[str, list] = {}
    for offer in unknown:
        by_norm.setdefault(normalize_alias(offer.model_name), []).append(offer)
    pending: list = []
    for norm, spelled in by_norm.items():
        if norm in seeded:
            offers, canonical, _source = assembled[seeded[norm]]
            offers.extend(spelled)
            assembled[seeded[norm]] = (offers, canonical, "normalized")
        elif len({offer.model_name for offer in spelled}) > 1:
            assembled.append((list(spelled), _common_spelling(spelled), "normalized"))
        else:
            pending.extend(spelled)
    for offers, canonical, source in assembled:
        groups.append(
            _group(
                offers,
                canonical=canonical,
                confidence=1.0,
                source=source,
                needs_review=False,
            )
        )
    if pending and (len({o.model_name for o in pending}) > 1 or by_canonical):
        groups.extend(
            _provider_clusters(
                organization=organization,
                provider=provider,
                provider_model=provider_model,
                brand_norm=brand_norm,
                capacity=capacity,
                grade_norm=grade_norm,
                color_norm=color_norm,
                known=sorted(by_canonical),
                pending=pending,
                notes=notes,
                user=user,
            )
        )
    else:
        for offer in pending:
            groups.append(
                _group(
                    [offer],
                    canonical=offer.model_name or "",
                    confidence=0.0,
                    source="provisional",
                    needs_review=True,
                )
            )
            notes.append(f"{_label(offer)}: new spelling, needs reconcile.")
    offer_by_id = {offer.id: offer for offer in members}
    return _resolve_categories(
        organization=organization,
        provider=provider,
        provider_model=provider_model,
        brand_norm=brand_norm,
        capacity=capacity,
        grade_norm=grade_norm,
        groups=groups,
        offer_by_id=offer_by_id,
        offer_alias=offer_alias,
        categories=categories,
        file_categories=file_categories,
        notes=notes,
        user=user,
    )


def _provider_clusters(
    *,
    organization,
    provider,
    provider_model,
    brand_norm,
    capacity,
    grade_norm,
    color_norm,
    known,
    pending,
    notes,
    user=None,
) -> list[dict]:
    """Cluster unknown spellings through the provider; validate strictly."""
    raws: list[str] = []
    by_raw: dict[str, list] = {}
    for offer in pending:
        raw = offer.model_name or ""
        if raw not in by_raw:
            by_raw[raw] = []
            raws.append(raw)
        by_raw[raw].append(offer)
    suppliers = {
        raw: sorted({o.supplier for o in by_raw[raw] if (o.supplier or "").strip()})
        for raw in raws
    }
    payload = {
        "block": {
            "brand": brand_norm,
            "capacity_gb": capacity,
            "grade": grade_norm,
            "color": color_norm,
        },
        "known": known,
        "spellings": [
            {"supplier": suppliers[raw], "raw": raw} for raw in raws
        ],
    }
    block_label = (
        f"{brand_norm or '?'} {capacity}GB {grade_norm or 'no grade'} "
        f"{color_norm or 'no color'}"
    ).strip()

    def _note_chunk(position, total, prompt_tokens, completion_tokens):
        from pipeline.jobs import record_usage

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
                "task": "match",
                "block": block_label,
            },
            user=user,
        )

    try:
        result = provider.match_products(payload, on_chunk=_note_chunk)
    except MatchUnusable as exc:
        for offer in pending:
            notes.append(f"{_label(offer)}: model proposal unusable ({exc}).")
        return [_provisional(offer) for offer in pending]
    clusters = result.get("clusters", [])
    if not isinstance(clusters, list):
        for offer in pending:
            notes.append(f"{_label(offer)}: model proposal unusable.")
        return [_provisional(offer) for offer in pending]
    return _apply_clusters(
        clusters, raws=raws, by_raw=by_raw, known=known, notes=notes
    )


def _apply_clusters(clusters, *, raws, by_raw, known, notes) -> list[dict]:
    """Validate clusters; usable ones group, the rest stay provisional."""
    known_set = set(known)
    seen: set[int] = set()
    groups: list[dict] = []
    leftovers: list[str] = []

    def _reject(raw, reason):
        notes.append(f"{raw or '?'}: {reason}, needs reconcile.")
        if raw not in leftovers:
            leftovers.append(raw)

    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        members = cluster.get("members")
        canonical = cluster.get("canonical")
        confidence = cluster.get("confidence")
        valid = (
            isinstance(members, list)
            and members
            and all(isinstance(m, int) and 0 <= m < len(raws) for m in members)
            and isinstance(canonical, str)
            and canonical.strip() != ""
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0.0 <= float(confidence) <= 1.0
        )
        if not valid:
            continue
        if any(m in seen for m in members):
            for m in members:
                if m not in seen:
                    _reject(raws[m], "model double-booked a spelling")
            seen.update(members)
            continue
        member_raws = [raws[m] for m in members]
        if canonical not in member_raws and canonical not in known_set:
            for raw in member_raws:
                _reject(raw, "model invented a name")
            seen.update(members)
            continue
        seen.update(members)
        if float(confidence) >= MATCH_AUTO_CONFIDENCE:
            offers = [o for raw in member_raws for o in by_raw[raw]]
            groups.append(
                _group(
                    offers,
                    canonical=canonical,
                    confidence=float(confidence),
                    source="llm",
                    needs_review=False,
                )
            )
        else:
            for raw in member_raws:
                if float(confidence) > 0:
                    notes.append(
                        f"{raw or '?'}: model unsure "
                        f"({float(confidence):.2f}), needs reconcile."
                    )
                else:
                    notes.append(f"{raw or '?'}: new spelling, needs reconcile.")
                for offer in by_raw[raw]:
                    group = _provisional(offer)
                    if float(confidence) > 0:
                        group["suggestion"] = canonical
                    groups.append(group)
    for index, raw in enumerate(raws):
        if index not in seen:
            _reject(raw, "model left a spelling unplaced")
    for raw in leftovers:
        for offer in by_raw[raw]:
            groups.append(_provisional(offer))
    return groups


def _resolve_categories(
    *,
    organization,
    provider,
    provider_model,
    brand_norm,
    capacity,
    grade_norm,
    groups,
    offer_by_id,
    offer_alias,
    categories,
    file_categories,
    notes,
    user=None,
) -> list[dict]:
    """Resolve one category per group: file, memory, then model lane.

    With no categories configured every group stays uncategorized and
    quiet — categories are optional. A unanimous file-level mapping
    wins; otherwise a remembered alias majority (or a lone alias on a
    singleton) decides; the rest go to the provider, or straight to
    reconcile when simulated.
    """
    names = {category.id: category.name for category in categories}
    if not names:
        return groups
    model_needed: list[dict] = []
    for group in groups:
        members = [offer_by_id[pk] for pk in group["offers"]]
        file_ids = {
            file_categories.get(offer.run.source_file_id) for offer in members
        }
        file_ids.discard(None)
        if len(file_ids) == 1:
            category_id = next(iter(file_ids))
            if category_id in names:
                group["category_id"] = category_id
                group["category_name"] = names[category_id]
                group["category_source"] = "file"
                continue
        remembered = [
            offer_alias[offer.id].category_id
            for offer in members
            if offer.id in offer_alias
            and offer_alias[offer.id].category_id is not None
            and offer_alias[offer.id].category_id in names
        ]
        if remembered and (
            len(members) == 1
            or len(set(remembered)) == 1
            and len(remembered) > len(members) / 2
        ):
            category_id = remembered[0]
            group["category_id"] = category_id
            group["category_name"] = names[category_id]
            group["category_source"] = "alias_memory"
            continue
        model_needed.append(group)
    if not model_needed:
        return groups
    if provider.is_simulated:
        for group in model_needed:
            group["category_review"] = True
            notes.append(
                f"{group['canonical_model'] or '?'}: no category rule "
                "covers this product, needs reconcile."
            )
        return groups
    _categorize_groups(
        organization=organization,
        provider=provider,
        provider_model=provider_model,
        brand_norm=brand_norm,
        capacity=capacity,
        grade_norm=grade_norm,
        groups=model_needed,
        offer_by_id=offer_by_id,
        names=names,
        notes=notes,
        user=user,
    )
    return groups


def _categorize_groups(
    *,
    organization,
    provider,
    provider_model,
    brand_norm,
    capacity,
    grade_norm,
    groups,
    offer_by_id,
    names,
    notes,
    user=None,
) -> None:
    """Assign categories through the provider; validate strictly.

    Mutates groups in place: confident known-name decisions stick,
    everything else — unknown names, low confidence, unplaced or
    double-booked products — waits for human reconcile.
    """
    by_name = {name: pk for pk, name in names.items()}
    payload = {
        "block": {"brand": brand_norm, "capacity_gb": capacity, "grade": grade_norm},
        "categories": sorted(names.values()),
        "products": [
            {
                "ref": ref,
                "supplier": sorted(
                    {
                        offer.supplier
                        for pk in group["offers"]
                        for offer in [offer_by_id[pk]]
                        if (offer.supplier or "").strip()
                    }
                ),
                "brand": group["brand"],
                "model": group["canonical_model"],
                "grade": group["grade"],
            }
            for ref, group in enumerate(groups)
        ],
    }
    block_label = (
        f"categorize {brand_norm or '?'} {capacity}GB "
        f"{grade_norm or 'no grade'}"
    ).strip()

    def _note_chunk(position, total, prompt_tokens, completion_tokens):
        from pipeline.jobs import record_usage

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
                "task": "categorize",
                "block": block_label,
            },
            user=user,
        )

    try:
        result = provider.categorize(payload, on_chunk=_note_chunk)
    except MatchUnusable as exc:
        for group in groups:
            group["category_review"] = True
            notes.append(
                f"{group['canonical_model'] or '?'}: category proposal "
                f"unusable ({exc})."
            )
        return
    decisions = result.get("decisions", [])
    if not isinstance(decisions, list):
        decisions = []
    seen: set[int] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        ref = decision.get("ref")
        name = decision.get("category")
        confidence = decision.get("confidence")
        valid = (
            isinstance(ref, int)
            and not isinstance(ref, bool)
            and 0 <= ref < len(groups)
            and isinstance(name, str)
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0.0 <= float(confidence) <= 1.0
        )
        if not valid:
            continue
        if ref in seen:
            group = groups[ref]
            group["category_id"] = None
            group["category_name"] = ""
            group["category_source"] = "none"
            group["category_review"] = True
            group.pop("category_suggestion", None)
            notes.append(
                f"{group['canonical_model'] or '?'}: model double-booked "
                "this product, needs reconcile."
            )
            continue
        seen.add(ref)
        group = groups[ref]
        label = group["canonical_model"] or "?"
        if name and name not in by_name:
            group["category_review"] = True
            notes.append(
                f"{label}: model suggested an unknown category "
                f"({name}), needs reconcile."
            )
        elif name and float(confidence) >= MATCH_AUTO_CONFIDENCE:
            group["category_id"] = by_name[name]
            group["category_name"] = name
            group["category_source"] = "auto"
        else:
            group["category_review"] = True
            if name:
                group["category_suggestion"] = name
                notes.append(
                    f"{label}: model unsure about this category "
                    f"({float(confidence):.2f}), needs reconcile."
                )
            else:
                notes.append(
                    f"{label}: model left this product uncategorized, "
                    "needs reconcile."
                )
    for ref, group in enumerate(groups):
        if ref not in seen:
            group["category_review"] = True
            notes.append(
                f"{group['canonical_model'] or '?'}: model left this "
                "product unplaced, needs reconcile."
            )


def _provisional(offer) -> dict:
    return _group(
        [offer],
        canonical=offer.model_name or "",
        confidence=0.0,
        source="provisional",
        needs_review=True,
    )


def _group(offers, *, canonical, confidence, source, needs_review) -> dict:
    return {
        "brand": _display_brand(offers),
        "capacity_gb": offers[0].capacity_gb,
        "grade": offers[0].grade or "",
        "color": offers[0].color or "",
        "canonical_model": canonical,
        "confidence": confidence,
        "source": source,
        "needs_review": needs_review,
        "category_id": None,
        "category_name": "",
        "category_source": "none",
        "category_review": False,
        "offers": [offer.id for offer in offers],
        "suppliers": sorted({o.supplier for o in offers if (o.supplier or "").strip()}),
        "members": [
            {
                "id": offer.id,
                "supplier": offer.supplier,
                "model_name": offer.model_name,
                "grade": offer.grade or "",
                "color": offer.color or "",
                "unit_price": str(offer.unit_price),
                "currency": (offer.currency or "").strip().upper(),
            }
            for offer in offers
        ],
    }


def _label(offer) -> str:
    parts = [offer.supplier, offer.brand, offer.model_name]
    return " ".join(part for part in parts if (part or "").strip()) or f"offer {offer.id}"


def remember_decisions(*, organization, decisions: list, expected_offer_ids) -> dict:
    """Validate reconcile decisions and learn every verifiable alias.

    Each decision is {"canonical_model": str, "offer_ids": [...]} with
    an optional "category_id" (int or null). Groups must stay inside
    one (brand, capacity, grade, color) block and place every
    expected offer exactly once. Members without a model name or
    capacity group fine but are never remembered.
    """
    expected = {int(pk) for pk in expected_offer_ids}
    if not isinstance(decisions, list) or not decisions:
        raise ValidationError("Reconcile needs at least one group.")
    seen: set[int] = set()
    for position, decision in enumerate(decisions, start=1):
        label = f"Group {position}"
        if not isinstance(decision, dict):
            raise ValidationError(f"{label} must be an object.")
        canonical = decision.get("canonical_model")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValidationError(f"{label} needs a canonical model name.")
        offer_ids = decision.get("offer_ids")
        if (
            not isinstance(offer_ids, list)
            or not offer_ids
            or any(isinstance(pk, bool) or not isinstance(pk, int) for pk in offer_ids)
        ):
            raise ValidationError(f"{label} needs a non-empty offer list.")
        if len(set(offer_ids)) != len(offer_ids):
            raise ValidationError(f"{label} lists an offer twice.")
        if seen & set(offer_ids):
            raise ValidationError(f"{label} reuses an offer from another group.")
        seen.update(offer_ids)
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        if missing:
            raise ValidationError(f"Offers left unplaced: {missing}.")
        raise ValidationError(f"Unknown offers placed: {extra}.")
    offers = {
        offer.id: offer
        for offer in NormalizedOffer.objects.filter(
            id__in=expected, organization=organization, status=OfferStatus.OK
        )
    }
    if len(offers) != len(expected):
        raise ValidationError("Unknown offers placed.")
    known_categories = set(
        Category.objects.filter(organization=organization).values_list("id", flat=True)
    )
    remembered = 0
    for position, decision in enumerate(decisions, start=1):
        label = f"Group {position}"
        members = [offers[pk] for pk in decision["offer_ids"]]
        brands = {normalize_brand(o.brand) for o in members}
        capacities = {o.capacity_gb for o in members}
        grades = {normalize_grade(o.grade) for o in members}
        colors = {normalize_color(o.color) for o in members}
        if (
            len(brands) > 1
            or len(capacities) > 1
            or len(grades) > 1
            or len(colors) > 1
        ):
            raise ValidationError(
                f"{label} mixes brands, capacities, grades, or colors; "
                "split it first."
            )
        category_id = decision.get("category_id")
        if category_id is not None and (
            isinstance(category_id, bool)
            or not isinstance(category_id, int)
            or category_id not in known_categories
        ):
            raise ValidationError(f"{label} names an unknown category.")
        canonical = decision["canonical_model"].strip()[:255]
        for offer in members:
            alias = normalize_alias(offer.model_name)
            if not alias or offer.capacity_gb is None:
                continue
            _remember_alias(
                organization=organization,
                supplier_norm=normalize_supplier(offer.supplier),
                alias_norm=alias,
                brand_norm=normalize_brand(offer.brand),
                capacity_gb=offer.capacity_gb,
                grade_norm=normalize_grade(offer.grade),
                canonical_model=canonical,
                category_id=category_id,
            )
            remembered += 1
    return {"groups": len(decisions), "remembered": remembered}


def _remember_alias(
    *,
    organization,
    supplier_norm,
    alias_norm,
    brand_norm,
    capacity_gb,
    grade_norm,
    canonical_model,
    category_id,
) -> None:
    """Create or refresh one learned alias (use counts every confirm)."""
    alias, created = ProductAlias.objects.get_or_create(
        organization=organization,
        supplier_norm=supplier_norm,
        alias_norm=alias_norm,
        brand_norm=brand_norm,
        capacity_gb=capacity_gb,
        grade_norm=grade_norm,
        defaults={"canonical_model": canonical_model, "category_id": category_id},
    )
    if created:
        return
    alias.canonical_model = canonical_model
    alias.category_id = category_id
    alias.use_count += 1
    alias.save(
        update_fields=["canonical_model", "category", "use_count", "last_used_at"]
    )
