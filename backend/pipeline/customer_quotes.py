"""Customer quotes over matched cross-supplier offers.

Sources are always the latest approved (or completed) run per
supplier, so a quote reflects the newest known price list from
every supplier with nothing hand-picked. Groups arrive confirmed
from reconcile with one canonical model name each; rules then pick
one supplier per group and reprice.

Cross-currency comparison converts every candidate to the
organization's destination using its own file's recorded rates —
each file keeps its own date and table, so numbers stay
reproducible. Without a destination, mixed-currency groups are
skipped loudly instead of compared raw.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from pipeline.fx import FxRateMissing
from pipeline.matching import (
    normalize_brand,
    normalize_supplier,
    propose_groups,
    remember_decisions,
)
from pipeline.models import (
    Category,
    CustomerQuote,
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    QuoteStatus,
    RunStatus,
    SourceFile,
    SupplierStrategy,
)
from pipeline.quotes import _apply_ops, _file_fx, _winning_rule

QUOTEABLE_STATUSES = (RunStatus.APPROVED, RunStatus.COMPLETED)


def collect_quote_offers(organization) -> tuple[list, list[int]]:
    """Ok offers from the latest quotable run per supplier.

    Returns (offers, source_run_ids). A supplier's older approved
    runs are superseded; drafts, failed, and cancelled runs never
    feed a customer quote.
    """
    runs = list(
        ImportRun.objects.filter(
            organization=organization, status__in=QUOTEABLE_STATUSES
        ).order_by("id")
    )
    if not runs:
        return [], []
    offers = list(
        NormalizedOffer.objects.filter(
            organization=organization,
            run__in=runs,
            status=OfferStatus.OK,
        )
        .select_related("run", "run__source_file")
        .order_by("id")
    )
    latest: dict[str, ImportRun] = {}
    for run in runs:
        key = (run.approved_at or run.created_at, run.id)
        for offer in offers:
            if offer.run_id != run.id:
                continue
            supplier = normalize_supplier(offer.supplier)
            current = latest.get(supplier)
            current_key = (
                (current.approved_at or current.created_at, current.id)
                if current is not None
                else None
            )
            if current_key is None or key > current_key:
                latest[supplier] = run
    wanted = {run.id for run in latest.values()}
    chosen = [offer for offer in offers if offer.run_id in wanted]
    return chosen, sorted(wanted)


def build_customer_quote(*, organization, decisions: list) -> dict:
    """Price confirmed groups into lines, notes, and a message.

    decisions is [{"canonical_model": str, "offer_ids": [...]}] with
    an optional "category_id" each, as validated by reconcile.
    Returns {"lines", "notes", "message"}.
    """
    if not isinstance(decisions, list) or not decisions:
        raise ValidationError("A quote needs at least one product group.")
    wanted: set[int] = set()
    for decision in decisions:
        for pk in decision.get("offer_ids", []):
            if isinstance(pk, bool) or not isinstance(pk, int):
                raise ValidationError("Offer ids must be integers.")
            wanted.add(pk)
    offers = {
        offer.id: offer
        for offer in NormalizedOffer.objects.filter(
            id__in=wanted, organization=organization, status=OfferStatus.OK
        ).select_related("run", "run__source_file")
    }
    if len(offers) != len(wanted):
        raise ValidationError("Unknown offers placed.")
    rules = list(organization.pricing_rules.filter(active=True))
    destination = (organization.destination_currency or "").strip().upper()
    categories = dict(
        organization.categories.values_list("id", "name")
    )
    lines, notes = [], []
    for decision in decisions:
        canonical = decision.get("canonical_model", "").strip()
        members = [offers[pk] for pk in decision.get("offer_ids", [])]
        category_id = decision.get("category_id")
        if category_id not in categories:
            category_id = None
        _price_group(
            members,
            canonical=canonical,
            rules=rules,
            destination=destination,
            category_id=category_id,
            category_name=categories.get(category_id, "") if category_id else "",
            lines=lines,
            notes=notes,
        )
    lines.sort(
        key=lambda line: (
            not line["category_name"],
            line["category_name"].lower(),
            normalize_brand(line["brand"]),
            line["capacity_gb"] is None,
            line["capacity_gb"] or 0,
            line["model"].lower(),
        )
    )
    return {
        "lines": lines,
        "notes": notes,
        "message": render_message(
            organization_name=organization.name,
            day=timezone.localdate(),
            lines=lines,
        ),
    }


def _common_raw(values: list) -> str:
    """Most common raw spelling (ties break by first seen)."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for value in values:
        raw = value or ""
        if raw not in counts:
            counts[raw] = 0
            order.append(raw)
        counts[raw] += 1
    best = order[0]
    for raw in order[1:]:
        if counts[raw] > counts[best]:
            best = raw
    return best


def _converted_price(offer, destination: str) -> Decimal | None:
    """Offer price in the destination, or None without a recorded rate."""
    currency = (offer.currency or "").strip().upper()
    if currency == destination:
        return offer.unit_price
    fx = _file_fx(offer.run)
    if (fx.get("destination") or "").strip().upper() != destination:
        return None
    rate = (fx.get("rates") or {}).get(currency)
    if rate is None or offer.unit_price is None:
        return None
    return offer.unit_price * Decimal(rate)


def _pick_across_suppliers(candidates, strategy, preferred, destination, notes, label):
    """Pick one offer, converting to the destination when needed."""
    if strategy == SupplierStrategy.PREFERRED:
        want = (preferred or "").strip().lower()
        for offer in candidates:
            if (offer.supplier or "").strip().lower() == want:
                return offer
        notes.append(f"Skipped {label}: preferred supplier {preferred!r} not found.")
        return None
    currencies = {(o.currency or "").strip().upper() for o in candidates}
    if len(currencies) == 1:
        if strategy == SupplierStrategy.HIGHEST:
            return max(candidates, key=lambda offer: offer.unit_price)
        return min(candidates, key=lambda offer: offer.unit_price)
    if not destination:
        notes.append(
            f"Skipped {label}: mixed currencies need a destination "
            "currency in Settings to compare."
        )
        return None
    priced = []
    excluded = []
    for offer in candidates:
        converted = _converted_price(offer, destination)
        if converted is None:
            excluded.append(offer.supplier or f"offer {offer.id}")
        else:
            priced.append((converted, offer))
    if not priced:
        notes.append(f"Skipped {label}: no recorded rate to {destination}.")
        return None
    if excluded:
        notes.append(
            f"{label}: {', '.join(sorted(set(excluded)))} excluded, "
            f"no recorded rate to {destination}."
        )
    priced.sort(key=lambda item: item[0])
    if strategy == SupplierStrategy.HIGHEST:
        return priced[-1][1]
    return priced[0][1]


def _price_group(
    members, *, canonical, rules, destination, category_id, category_name, lines, notes
) -> None:
    """Price one confirmed group into lines (or skip with a note)."""
    first = members[0]
    brand = _common_raw([o.brand for o in members])
    grade = _common_raw([o.grade for o in members])
    rule = _winning_rule(
        rules,
        {
            "brand": brand,
            "model": canonical,
            "grade": grade,
            "category": category_name,
        },
    )
    strategy = rule.supplier_strategy if rule else SupplierStrategy.LOWEST
    preferred = rule.preferred_supplier if rule else ""
    label = " ".join(
        part
        for part in (
            brand,
            canonical,
            f"{first.capacity_gb}GB" if first.capacity_gb is not None else "",
        )
        if part
    )
    picked = _pick_across_suppliers(
        members, strategy, preferred, destination, notes, label
    )
    if picked is None:
        return
    try:
        price, currency = _apply_ops(
            picked.unit_price,
            (picked.currency or "").strip().upper(),
            rule.ops if rule else [],
            _file_fx(picked.run),
        )
    except FxRateMissing as exc:
        notes.append(f"Skipped {label}: {exc.detail}.")
        return
    lines.append(
        {
            "brand": brand,
            "model": canonical,
            "capacity_gb": first.capacity_gb,
            "grade": picked.grade or "",
            "category_id": category_id,
            "category_name": category_name,
            "supplier": picked.supplier,
            "quantity": picked.quantity,
            "unit_price": str(price),
            "currency": currency,
            "rule": rule.name if rule else None,
            "candidates": sorted(
                (
                    {
                        "supplier": o.supplier,
                        "unit_price": str(o.unit_price),
                        "currency": (o.currency or "").strip().upper(),
                    }
                    for o in members
                ),
                key=lambda item: item["supplier"] or "",
            ),
        }
    )


def quote_lines_for_category(*, lines: list, category_id) -> tuple:
    """Filter frozen quote lines to one category.

    category_id None returns every line; "none" returns uncategorized
    lines; an id returns that shelf. Returns (lines, slug suffix).
    Unknown or empty shelves raise ValidationError loudly.
    """
    from django.utils.text import slugify

    if category_id is None:
        return list(lines), ""
    if category_id == "none":
        filtered = [
            line
            for line in lines
            if isinstance(line, dict) and not line.get("category_id")
        ]
        if not filtered:
            raise ValidationError("This quote has no uncategorized lines.")
        return filtered, "-uncategorized"
    try:
        wanted = int(category_id)
    except (TypeError, ValueError):
        raise ValidationError(
            'category_id must be an id or "none".'
        ) from None
    filtered = [
        line
        for line in lines
        if isinstance(line, dict) and line.get("category_id") == wanted
    ]
    if not filtered:
        raise ValidationError("This quote has no lines in that category.")
    name = filtered[0].get("category_name") or f"category-{wanted}"
    return filtered, f"-{slugify(name) or f'category-{wanted}'}"


def _display_price(value: str) -> str:
    """Human price for messages: 245.0000 renders as 245."""
    try:
        return format(Decimal(value).normalize(), "f")
    except Exception:
        return value


def render_message(*, organization_name: str, day, lines: list) -> str:
    """Render quote lines as plain Telegram-ready message text.

    Lines carrying categories group under one header per category
    (uncategorized lines trail under "Other"); without categories
    the message stays the familiar flat list.
    """
    head = f"Quote — {organization_name} — {day.isoformat()}"
    rows = [head]

    def _row(line) -> str:
        title = " ".join(
            part
            for part in (
                line["brand"],
                line["model"],
                f"{line['capacity_gb']}GB" if line["capacity_gb"] is not None else "",
            )
            if part
        )
        if line["grade"]:
            title += f" ({line['grade']})"
        return f"{title} — {line['currency']} {_display_price(line['unit_price'])}"

    if not any(line.get("category_name") for line in lines):
        rows.extend(_row(line) for line in lines)
        return "\n".join(rows)
    section = None
    for line in lines:
        name = line.get("category_name") or "Other"
        if name != section:
            section = name
            rows.append("")
            rows.append(f"{section}:")
        rows.append(_row(line))
    return "\n".join(rows)


def start_customer_quote(*, organization, user) -> CustomerQuote:
    """Match the latest supplier runs into a reviewable quote draft."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, organization.id)
    offers, run_ids = collect_quote_offers(organization)
    if not offers:
        raise ValidationError(
            "Nothing to quote yet: approve at least one supplier import first."
        )
    proposed = propose_groups(organization=organization, offers=offers, user=user)
    quote = CustomerQuote.objects.create(
        organization=organization,
        status=QuoteStatus.NEEDS_REVIEW,
        source_run_ids=run_ids,
        groups=proposed["groups"],
        notes=proposed["notes"],
        provider_name=proposed["provider"],
        simulated=proposed["simulated"],
        created_by=user,
    )
    record_audit(
        organization.id, user, "quote_started", "quote", quote.id,
        {"runs": run_ids, "groups": len(proposed["groups"])},
    )
    return quote


def confirm_customer_quote(*, quote: CustomerQuote, user, decisions: list) -> CustomerQuote:
    """Validate reconcile decisions, learn them, and price the quote."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, quote.organization_id)
    if quote.status != QuoteStatus.NEEDS_REVIEW:
        raise ValidationError("This quote is already ready.")
    expected = [
        offer_id for group in quote.groups for offer_id in group.get("offers", [])
    ]
    learned = remember_decisions(
        organization=quote.organization,
        decisions=decisions,
        expected_offer_ids=expected,
    )
    result = build_customer_quote(
        organization=quote.organization, decisions=decisions
    )
    quote.groups = [
        {
            "canonical_model": d.get("canonical_model"),
            "offer_ids": d.get("offer_ids"),
            "category_id": d.get("category_id"),
        }
        for d in decisions
    ]
    quote.lines = result["lines"]
    quote.notes = result["notes"]
    quote.message = result["message"]
    quote.status = QuoteStatus.READY
    quote.save()
    record_audit(
        quote.organization_id,
        user,
        "quote_confirmed",
        "quote",
        quote.id,
        {"groups": learned["groups"], "lines": len(result["lines"])},
    )
    return quote


def _clean_category_name(name) -> str:
    """Strip a category name; blank names are refused loudly."""
    cleaned = (name or "").strip() if isinstance(name, str) else ""
    if not cleaned:
        raise ValidationError("A category needs a name.")
    return cleaned[:100]


def create_category(*, organization, user, data: dict) -> Category:
    """Create a company category. Operating roles only; audited."""
    from django.db import IntegrityError

    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, organization.id)
    name = _clean_category_name(data.get("name"))
    if organization.categories.filter(name__iexact=name).exists():
        raise ValidationError(f"A category named {name!r} already exists.")
    try:
        category = Category.objects.create(organization=organization, name=name)
    except IntegrityError:
        raise ValidationError(
            f"A category named {name!r} already exists."
        ) from None
    record_audit(
        organization.id, user, "category_created", "category", category.id
    )
    return category


def update_category(*, category: Category, user, data: dict) -> Category:
    """Rename a company category. Operating roles only; audited.

    Pricing rules naming the old shelf are retargeted to the new
    name, so a rename never silently unprices a category.
    """
    from django.db import IntegrityError

    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit
    from pipeline.models import PricingRule

    require_organization_role(user, category.organization_id)
    old_name = category.name
    category.name = _clean_category_name(data.get("name", category.name))
    if (
        category.organization.categories.filter(name__iexact=category.name)
        .exclude(pk=category.pk)
        .exists()
    ):
        raise ValidationError(
            f"A category named {category.name!r} already exists."
        )
    try:
        category.save(update_fields=["name"])
    except IntegrityError:
        raise ValidationError(
            f"A category named {category.name!r} already exists."
        ) from None
    retargeted = 0
    if category.name != old_name:
        for rule in PricingRule.objects.filter(organization_id=category.organization_id):
            matcher = rule.matcher or {}
            if matcher.get("category", "").strip().lower() == old_name.strip().lower():
                matcher["category"] = category.name
                rule.matcher = matcher
                rule.save(update_fields=["matcher"])
                retargeted += 1
    record_audit(
        category.organization_id,
        user,
        "category_renamed",
        "category",
        category.id,
        {"rules_retargeted": retargeted},
    )
    return category


def delete_category(*, category: Category, user) -> None:
    """Delete a company category, unmapping files and aliases.

    Operating roles only; audited. Mapped files and learned aliases
    keep working — they simply become uncategorized. Deletion is
    refused while a pricing rule names the shelf, so no rule goes
    dead silently.
    """
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit
    from pipeline.models import PricingRule

    require_organization_role(user, category.organization_id)
    users = sorted(
        rule.name
        for rule in PricingRule.objects.filter(
            organization_id=category.organization_id
        )
        if (rule.matcher or {}).get("category", "").strip().lower()
        == category.name.strip().lower()
    )
    if users:
        raise ValidationError(
            f"Category {category.name!r} is used by pricing rules "
            f"({', '.join(users)}); retarget them first."
        )
    organization_id = category.organization_id
    category_id = category.id
    category.delete()
    record_audit(
        organization_id, user, "category_deleted", "category", category_id
    )


def set_file_category(
    *, source_file: SourceFile, user, category_id
) -> SourceFile:
    """Map a whole file to a category (or unmap with null).

    The category must belong to the file's company; operating roles
    only; audited.
    """
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, source_file.organization_id)
    if category_id is None:
        source_file.category = None
    else:
        if (
            isinstance(category_id, bool)
            or not isinstance(category_id, int)
            or category_id < 1
        ):
            raise ValidationError("category_id must be an id or null.")
        try:
            category = source_file.organization.categories.get(pk=category_id)
        except Category.DoesNotExist:
            raise ValidationError("Unknown category for this company.") from None
        source_file.category = category
    source_file.save(update_fields=["category"])
    record_audit(
        source_file.organization_id,
        user,
        "file_category_set",
        "file",
        source_file.id,
        {"category_id": source_file.category_id},
    )
    return source_file


__all__ = [
    "QUOTEABLE_STATUSES",
    "build_customer_quote",
    "collect_quote_offers",
    "confirm_customer_quote",
    "create_category",
    "delete_category",
    "render_message",
    "set_file_category",
    "start_customer_quote",
    "update_category",
]
