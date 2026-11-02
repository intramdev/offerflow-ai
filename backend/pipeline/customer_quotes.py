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
from pipeline.quotes import (
    DEFAULT_EXPORT_COLUMNS,
    QUOTE_COLUMN_KEYS,
    _apply_ops,
    _file_fx,
    _winning_rule,
    format_money,
)

QUOTEABLE_STATUSES = (RunStatus.APPROVED, RunStatus.COMPLETED)

#: Columns a customer price list cannot drop: without a product or a
#: price the file is not a quote.
REQUIRED_EXPORT_COLUMNS = ("model", "price")

#: Longest customer-facing label or message header/footer line.
MAX_LABEL_CHARS = 200

#: Longest grade/color override on a quote line.
MAX_VARIANT_CHARS = 60

#: Per-company export template when nothing was customized yet: the
#: sourcing (supplier) column is intelligence and stays out of
#: customer files and messages until the company opts it back in.
DEFAULT_QUOTE_TEMPLATE = {
    "file_columns": list(DEFAULT_EXPORT_COLUMNS),
    "message_show_capacity": True,
    "message_show_grade": True,
    "message_show_color": True,
    "message_show_quantity": False,
    "message_show_categories": True,
    "message_header": "",
    "message_footer": "",
}


def effective_template(organization) -> dict:
    """The company's export template with defaults filled in.

    Unknown stored keys are ignored (older templates keep working);
    callers validating user input want clean_quote_export instead.
    """
    stored = getattr(organization, "quote_export", None) or {}
    merged = dict(DEFAULT_QUOTE_TEMPLATE)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in merged:
                merged[key] = value
    raw_columns = merged["file_columns"]
    if not isinstance(raw_columns, list):
        raw_columns = []
    columns = [key for key in raw_columns if key in QUOTE_COLUMN_KEYS]
    for required in REQUIRED_EXPORT_COLUMNS:
        if required not in columns:
            columns.append(required)
    merged["file_columns"] = columns
    for key in (
        "message_show_capacity",
        "message_show_grade",
        "message_show_color",
        "message_show_quantity",
        "message_show_categories",
    ):
        merged[key] = bool(merged[key])
    for key in ("message_header", "message_footer"):
        text = merged[key]
        merged[key] = text if isinstance(text, str) else ""
    return merged


def _clean_template_text(value, *, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text.")
    text = " ".join(value.split())
    if len(text) > MAX_LABEL_CHARS:
        raise ValidationError(
            f"{label} must be {MAX_LABEL_CHARS} characters or fewer."
        )
    return text


def clean_quote_export(value) -> dict:
    """Validate a full export template from the settings form.

    Loud about every problem: unknown keys, unknown/duplicated
    columns, a dropped required column, non-boolean toggles.
    """
    if not isinstance(value, dict):
        raise ValidationError("The export template must be an object.")
    unknown = sorted(set(value) - set(DEFAULT_QUOTE_TEMPLATE))
    if unknown:
        raise ValidationError(
            f"Unknown template keys: {', '.join(unknown)}."
        )
    columns = value.get("file_columns", DEFAULT_QUOTE_TEMPLATE["file_columns"])
    if not isinstance(columns, list) or not columns:
        raise ValidationError("Pick at least one file column.")
    unknown_columns = sorted(
        {key for key in columns if key not in QUOTE_COLUMN_KEYS}
    )
    if unknown_columns:
        raise ValidationError(
            f"Unknown file columns: {', '.join(unknown_columns)}."
        )
    if len(set(columns)) != len(columns):
        raise ValidationError("File columns must not repeat.")
    missing = [key for key in REQUIRED_EXPORT_COLUMNS if key not in columns]
    if missing:
        raise ValidationError(
            f"A quote file needs these columns: {', '.join(missing)}."
        )
    cleaned = {"file_columns": list(columns)}
    for key in (
        "message_show_capacity",
        "message_show_grade",
        "message_show_color",
        "message_show_quantity",
        "message_show_categories",
    ):
        flag = value.get(key, DEFAULT_QUOTE_TEMPLATE[key])
        if not isinstance(flag, bool):
            raise ValidationError(f"{key} must be true or false.")
        cleaned[key] = flag
    cleaned["message_header"] = _clean_template_text(
        value.get("message_header", ""), label="The message header"
    )
    cleaned["message_footer"] = _clean_template_text(
        value.get("message_footer", ""), label="The message footer"
    )
    return cleaned


#: Sentinel for "no category override" (distinct from null, which
#: moves the line to uncategorized).
_KEEP_CATEGORY = object()


def _clean_variant(value, *, raw_key, name: str):
    """Validate a grade/color override (None keeps the frozen value)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"Line {raw_key}: {name} must be text.")
    cleaned = " ".join(value.split())
    if len(cleaned) > MAX_VARIANT_CHARS:
        raise ValidationError(
            f"Line {raw_key}: {name} must be "
            f"{MAX_VARIANT_CHARS} characters or fewer."
        )
    return cleaned


def _clean_override_price(value, *, raw_key) -> str:
    """Validate a manual unit price, stored rounded to the cent."""
    from decimal import InvalidOperation, ROUND_HALF_UP

    from pipeline.quotes import CENTS

    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValidationError(
            f"Line {raw_key}: the price must be a number."
        ) from None
    if not price.is_finite() or price < 0:
        raise ValidationError(
            f"Line {raw_key}: the price must be zero or more."
        )
    return format(price.quantize(CENTS, ROUND_HALF_UP), "f")


def clean_line_edits(value, *, lines: list, categories: list | None = None) -> dict:
    """Validate per-line presentation overrides for a ready quote.

    Callers address full-list positions ({"0": {...}}) or stable
    line ids ({"L1": {...}}); both are normalized to stable ids so
    an edit follows its line into category shelves. Lines frozen
    before stable ids keep positional keys instead.

    Overrides rename the customer-facing detail (label, grade,
    color, category, quantity, supplier, unit price) or hide the
    line; the frozen source values underneath are never touched,
    so provenance survives and every save bumps the revision with
    a fresh approval binding. Labels need words (hide the line
    instead); a manual price is stored rounded to the cent and
    presented flagged as manual; a supplier must be one of the
    line's candidates; a category must be one of the company's
    shelves (null moves the line to uncategorized). Overrides
    that equal the frozen value are dropped as no-ops. Returns
    the normalized edits map.
    """
    if not isinstance(value, dict):
        raise ValidationError("Line edits must be an object.")
    shelves = {
        shelf["id"]: shelf.get("name") or ""
        for shelf in (categories or [])
        if isinstance(shelf, dict) and isinstance(shelf.get("id"), int)
    }
    by_id = {}
    for position, line in enumerate(lines or []):
        if isinstance(line, dict) and line.get("id"):
            by_id[str(line["id"])] = position
    cleaned = {}
    allowed = {
        "label",
        "hidden",
        "grade",
        "color",
        "category_id",
        "quantity",
        "supplier",
        "unit_price",
    }
    for raw_key, edit in value.items():
        position = by_id.get(str(raw_key))
        if position is None:
            try:
                position = int(raw_key)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"Line {raw_key!r} is not a line number or id."
                ) from None
        if position < 0 or position >= len(lines or []):
            raise ValidationError(f"Line {raw_key} does not exist.")
        if not isinstance(edit, dict):
            raise ValidationError(f"Line {raw_key} must be an object.")
        unknown = sorted(set(edit) - allowed)
        if unknown:
            raise ValidationError(
                f"Line {raw_key}: unknown keys {', '.join(unknown)}."
            )
        line = lines[position]
        label = edit.get("label")
        if label is None:
            label = None
        elif not isinstance(label, str) or not label.strip():
            raise ValidationError(
                f"Line {raw_key}: the label needs words, or hide the line."
            )
        elif len(label.strip()) > MAX_LABEL_CHARS:
            raise ValidationError(
                f"Line {raw_key}: the label must be "
                f"{MAX_LABEL_CHARS} characters or fewer."
            )
        else:
            label = " ".join(label.split())
            if label == line.get("model"):
                label = None
        hidden = edit.get("hidden", False)
        if not isinstance(hidden, bool):
            raise ValidationError(f"Line {raw_key}: hidden must be true or false.")
        grade = _clean_variant(edit.get("grade"), raw_key=raw_key, name="grade")
        if grade is not None and grade == (line.get("grade") or ""):
            grade = None
        color = _clean_variant(edit.get("color"), raw_key=raw_key, name="color")
        if color is not None and color == (line.get("color") or ""):
            color = None
        category_id = edit.get("category_id", _KEEP_CATEGORY)
        category_name: str | None = None
        if category_id is _KEEP_CATEGORY:
            pass
        elif category_id is None:
            category_name = ""
            if line.get("category_id") is None:
                category_id = _KEEP_CATEGORY
        elif isinstance(category_id, bool) or not isinstance(category_id, int):
            raise ValidationError(
                f"Line {raw_key}: the category must be an id or null."
            )
        elif category_id not in shelves:
            raise ValidationError(
                f"Line {raw_key}: unknown category."
            )
        else:
            category_name = shelves[category_id]
            if category_id == line.get("category_id"):
                category_id = _KEEP_CATEGORY
        quantity = edit.get("quantity")
        if quantity is not None:
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                raise ValidationError(
                    f"Line {raw_key}: the quantity must be a whole number."
                )
            if quantity < 0:
                raise ValidationError(
                    f"Line {raw_key}: the quantity cannot be negative."
                )
            if quantity == line.get("quantity"):
                quantity = None
        supplier = edit.get("supplier")
        if supplier is not None:
            if not isinstance(supplier, str) or not supplier.strip():
                raise ValidationError(
                    f"Line {raw_key}: the supplier needs a name."
                )
            supplier = supplier.strip()
            offered = {
                str(item.get("supplier") or "")
                for item in (line.get("candidates") or [])
                if isinstance(item, dict)
            }
            offered.add(str(line.get("supplier") or ""))
            offered.discard("")
            if supplier not in offered:
                raise ValidationError(
                    f"Line {raw_key}: pick one of the line's suppliers."
                )
            if supplier == line.get("supplier"):
                supplier = None
        unit_price = edit.get("unit_price")
        if unit_price is not None:
            unit_price = _clean_override_price(unit_price, raw_key=raw_key)
            try:
                same = Decimal(unit_price) == Decimal(str(line.get("unit_price")))
            except Exception:
                same = False
            if same:
                unit_price = None
        entry: dict = {"label": label, "hidden": hidden}
        if grade is not None:
            entry["grade"] = grade
        if color is not None:
            entry["color"] = color
        if category_id is not _KEEP_CATEGORY:
            entry["category_id"] = category_id
            entry["category_name"] = category_name or ""
        if quantity is not None:
            entry["quantity"] = quantity
        if supplier is not None:
            entry["supplier"] = supplier
        if unit_price is not None:
            entry["unit_price"] = unit_price
        if (
            label is None
            and not hidden
            and len(entry) == 2
        ):
            continue
        stable = (
            str(line.get("id"))
            if isinstance(line, dict) and line.get("id")
            else str(position)
        )
        cleaned[stable] = entry
    return cleaned


def present_lines(*, lines: list, edits: dict | None) -> list:
    """Frozen lines as the customer sees them: overrides applied.

    Edits resolve by stable line id, falling back to the
    full-list position for quotes frozen before stable ids.
    Callers must present the full frozen list (filtering after),
    so even legacy positional edits meet the line they were
    written against. Hidden lines stay in place with hidden=True
    so callers can show them greyed out (internal view) or drop
    them (files/messages). A manual price replaces the frozen
    unit price and is flagged manual_price so every surface can
    say so; the frozen price itself is never rewritten.
    """
    edits = edits if isinstance(edits, dict) else {}
    presented = []
    for index, line in enumerate(lines):
        key = (
            str(line.get("id"))
            if isinstance(line, dict) and line.get("id")
            else str(index)
        )
        edit = edits.get(key) or {}
        shown = dict(line)
        label = edit.get("label")
        if label:
            shown["model"] = label
        if edit.get("grade") is not None:
            shown["grade"] = edit["grade"]
        if edit.get("color") is not None:
            shown["color"] = edit["color"]
        if "category_id" in edit:
            shown["category_id"] = edit["category_id"]
            shown["category_name"] = edit.get("category_name") or ""
        if edit.get("quantity") is not None:
            shown["quantity"] = edit["quantity"]
        if edit.get("supplier") is not None:
            shown["supplier"] = edit["supplier"]
        if edit.get("unit_price") is not None:
            shown["unit_price"] = edit["unit_price"]
            shown["manual_price"] = True
        shown["hidden"] = bool(edit.get("hidden", False))
        presented.append(shown)
    return presented


def export_lines(*, lines: list, edits: dict | None) -> list:
    """Lines for files and messages: labels applied, hidden dropped."""
    return [line for line in present_lines(lines=lines, edits=edits) if not line["hidden"]]


def collect_quote_offers(organization) -> tuple[list, list[int]]:
    """Ok offers from the latest quotable run per supplier.

    Returns (offers, source_run_ids). Precedence per supplier is
    the newest quotable source first: (created_at, id), so upload
    order decides and approval order never regresses a quote to
    an older price list. Approval only gates quotability: drafts,
    failed, and cancelled runs never feed a customer quote.

    Replacement is total per supplier: the chosen run's ok offers
    are the supplier's whole contribution. Products the newer
    file omits disappear (no stale-price fallback); a newer file
    with no ok offers silences that supplier; a zero quantity is
    collected as out-of-stock state, not as missing data.
    """
    runs = list(
        ImportRun.objects.filter(
            organization=organization, status__in=QUOTEABLE_STATUSES
        ).order_by("id")
    )
    if not runs:
        return [], []
    # Attribution uses every offer: an excluded row still names its
    # supplier, so a newer file with no ok offers silences that
    # supplier instead of resurrecting their older price list.
    offers = list(
        NormalizedOffer.objects.filter(
            organization=organization,
            run__in=runs,
        )
        .select_related("run", "run__source_file")
        .order_by("id")
    )
    latest: dict[str, ImportRun] = {}
    for run in runs:
        key = (run.created_at, run.id)
        for offer in offers:
            if offer.run_id != run.id:
                continue
            supplier = normalize_supplier(offer.supplier)
            current = latest.get(supplier)
            current_key = (
                (current.created_at, current.id)
                if current is not None
                else None
            )
            if current_key is None or key > current_key:
                latest[supplier] = run
    wanted = {run.id for run in latest.values()}
    # Per-supplier, not per-run: a run stays wanted for the
    # suppliers it is latest for, but its superseded suppliers'
    # offers must not leak back in through the shared run id.
    chosen = []
    for offer in offers:
        if offer.status != OfferStatus.OK:
            continue
        chosen_run = latest.get(normalize_supplier(offer.supplier))
        if chosen_run is not None and chosen_run.id == offer.run_id:
            chosen.append(offer)
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
    # Stable identity: presentation edits bind to these ids, so a
    # label or hidden flag follows its line into category shelves
    # instead of sticking to a filtered-list position.
    for position, line in enumerate(lines, start=1):
        line["id"] = f"L{position}"
    return {
        "lines": lines,
        "notes": notes,
        "message": render_message(
            organization_name=organization.name,
            day=timezone.localdate(),
            lines=lines,
            template=effective_template(organization),
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
    color = _common_raw([o.color for o in members])
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
            "color": color,
            "conditions": picked.conditions or "",
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
    """Human price for messages: 245.0000 renders as 245.00."""
    return format_money(value)


def render_message(
    *, organization_name: str, day, lines: list, template: dict | None = None
) -> str:
    """Render quote lines as plain Telegram-ready message text.

    Lines carrying categories group under one header per category
    (uncategorized lines trail under "Other"); without categories
    the message stays the familiar flat list. template carries the
    company's toggles (capacity/grade/quantity parts, category
    sections, custom header/footer); None renders the classic shape.
    """
    template = template or {}
    show_capacity = template.get("message_show_capacity", True)
    show_grade = template.get("message_show_grade", True)
    show_color = template.get("message_show_color", True)
    show_quantity = template.get("message_show_quantity", False)
    show_sections = template.get("message_show_categories", True)
    head = template.get("message_header") or f"Quote — {organization_name} — {day.isoformat()}"
    footer = template.get("message_footer") or ""
    rows = [head]

    def _row(line) -> str:
        title = " ".join(
            part
            for part in (
                line["brand"],
                line["model"],
                (
                    f"{line['capacity_gb']}GB"
                    if show_capacity and line["capacity_gb"] is not None
                    else ""
                ),
            )
            if part
        )
        variants = []
        if show_color and line.get("color"):
            variants.append(line["color"])
        if show_grade and line["grade"]:
            variants.append(line["grade"])
        if variants:
            title += f" ({', '.join(variants)})"
        text = f"{title} — {line['currency']} {_display_price(line['unit_price'])}"
        if show_quantity and line.get("quantity") is not None:
            text += f" ×{line['quantity']}"
        return text

    if show_sections and any(line.get("category_name") for line in lines):
        section = None
        for line in lines:
            name = line.get("category_name") or "Other"
            if name != section:
                section = name
                rows.append("")
                rows.append(f"{section}:")
            rows.append(_row(line))
    else:
        rows.extend(_row(line) for line in lines)
    if footer:
        rows.append("")
        rows.append(footer)
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
    from pipeline.release import bind_customer_quote

    quote.lines = result["lines"]
    quote.notes = result["notes"]
    quote.message = result["message"]
    quote.status = QuoteStatus.READY
    quote.revision = 1
    bind_customer_quote(quote)
    quote.save()
    record_audit(
        quote.organization_id,
        user,
        "quote_confirmed",
        "quote",
        quote.id,
        {"groups": learned["groups"], "lines": len(result["lines"]), "revision": 1},
    )
    return quote


def update_quote_lines(*, quote: CustomerQuote, user, edits: dict) -> CustomerQuote:
    """Replace a ready quote's line presentation overrides.

    Operating roles only; audited. Overrides change what
    customers see in files and messages; the frozen lines
    underneath (source prices, offers, provenance) are never
    rewritten, and every save bumps the revision with a fresh
    approval binding.
    """
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, quote.organization_id)
    if quote.status != QuoteStatus.READY:
        raise ValidationError(
            "Confirm the product groups before editing lines."
        )
    from pipeline.release import bind_customer_quote

    shelves = list(
        Category.objects.filter(organization_id=quote.organization_id).values(
            "id", "name"
        )
    )
    quote.line_edits = clean_line_edits(
        edits, lines=quote.lines or [], categories=shelves
    )
    quote.revision = quote.revision + 1
    bind_customer_quote(quote)
    quote.save(update_fields=["line_edits", "revision", "approved_lines_digest"])
    record_audit(
        quote.organization_id,
        user,
        "quote_lines_edited",
        "quote",
        quote.id,
        {"edited": len(quote.line_edits), "revision": quote.revision},
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


def delete_customer_quote(*, quote: CustomerQuote, user) -> None:
    """Delete a draft or ready customer quote.

    Operating roles only; audited. Groups, lines, and presentation
    edits live on the quote and go with it; supplier runs, learned
    aliases, and recorded usage are untouched, so a fresh quote can
    be generated from the same sources at any time.
    """
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, quote.organization_id)
    organization_id = quote.organization_id
    quote_id = quote.id
    quote.delete()
    record_audit(
        organization_id, user, "quote_deleted", "quote", quote_id
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
    "delete_customer_quote",
    "render_message",
    "set_file_category",
    "start_customer_quote",
    "update_category",
]
