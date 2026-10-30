"""Customer quotes: pricing rules pick one supplier per product, then reprice.

Only validated (ok) offers are quotable. Supplier selection happens within
same-currency groups so no implicit exchange rate ever sneaks into a pick;
currency conversion is an explicit rule op with a user-set rate.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from pipeline.export import sanitize_cell
from pipeline.fx import FxRateMissing
from pipeline.models import OfferStatus, PricingRule, SupplierStrategy

MATCHER_KEYS = ("brand", "model", "grade")

QUOTE_COLUMNS = [
    "Brand",
    "Model",
    "Capacity GB",
    "Grade",
    "Supplier",
    "Quantity",
    "Unit Price",
    "Currency",
    "Rule",
]


def _decimal(value, message: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValidationError(message) from None
    if not result.is_finite():
        raise ValidationError(message)
    return result


def _clean_op(op, index: int) -> dict:
    label = f"Step {index + 1}"
    if not isinstance(op, dict):
        raise ValidationError(f"{label} must be an object.")
    kind = op.get("op")
    if kind == "convert":
        rate = _decimal(op.get("rate"), f"{label}: rate must be a positive number.")
        if rate <= 0:
            raise ValidationError(f"{label}: rate must be a positive number.")
        currency = str(op.get("to") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError(f"{label}: convert needs a 3-letter currency.")
        return {"op": "convert", "rate": str(rate), "to": currency}
    if kind == "convert_to_destination":
        return {"op": "convert_to_destination"}
    if kind == "percent":
        value = _decimal(op.get("value"), f"{label}: percent must be a number.")
        return {"op": "percent", "value": str(value)}
    if kind == "round":
        step = _decimal(op.get("step"), f"{label}: round needs a positive step.")
        if step <= 0:
            raise ValidationError(f"{label}: round needs a positive step.")
        return {"op": "round", "step": str(step)}
    raise ValidationError(f"{label}: unknown operation {kind!r}.")


def clean_rule(
    *,
    name,
    matcher,
    supplier_strategy,
    preferred_supplier,
    ops,
    priority,
    active,
) -> dict:
    """Validate raw rule input into model-ready values."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("Rule needs a name.")
    if not isinstance(matcher, dict):
        raise ValidationError("Matcher must be an object.")
    cleaned_matcher = {}
    for key, value in matcher.items():
        if key not in MATCHER_KEYS:
            raise ValidationError(f"Cannot match on {key!r}.")
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Match value for {key} must be text.")
        cleaned_matcher[key] = value.strip()
    if supplier_strategy not in SupplierStrategy.values:
        raise ValidationError(f"Unknown supplier strategy {supplier_strategy!r}.")
    preferred = (preferred_supplier or "").strip() if isinstance(preferred_supplier, str) else ""
    if supplier_strategy == SupplierStrategy.PREFERRED and not preferred:
        raise ValidationError("Preferred strategy needs a supplier name.")
    if not isinstance(ops, list):
        raise ValidationError("Steps must be a list.")
    cleaned_ops = [_clean_op(op, index) for index, op in enumerate(ops)]
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        raise ValidationError("Priority must be a non-negative number.")
    return {
        "name": name.strip()[:100],
        "matcher": cleaned_matcher,
        "supplier_strategy": supplier_strategy,
        "preferred_supplier": preferred[:255],
        "ops": cleaned_ops,
        "priority": priority,
        "active": bool(active),
    }


def create_rule(*, organization, user, data: dict) -> PricingRule:
    """Create a pricing rule. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, organization.id)
    cleaned = clean_rule(
        name=data.get("name"),
        matcher=data.get("matcher") or {},
        supplier_strategy=data.get("supplier_strategy", SupplierStrategy.LOWEST),
        preferred_supplier=data.get("preferred_supplier") or "",
        ops=data.get("ops") or [],
        priority=data.get("priority", 100),
        active=data.get("active", True),
    )
    try:
        rule = PricingRule.objects.create(organization=organization, **cleaned)
    except IntegrityError:
        raise ValidationError(
            f"A rule named {cleaned['name']!r} already exists."
        ) from None
    record_audit(organization.id, user, "rule_created", "rule", rule.id)
    return rule


def update_rule(*, rule: PricingRule, user, data: dict) -> PricingRule:
    """Replace a pricing rule's definition. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, rule.organization_id)
    cleaned = clean_rule(
        name=data.get("name", rule.name),
        matcher=data.get("matcher", rule.matcher),
        supplier_strategy=data.get("supplier_strategy", rule.supplier_strategy),
        preferred_supplier=data.get("preferred_supplier", rule.preferred_supplier),
        ops=data.get("ops", rule.ops),
        priority=data.get("priority", rule.priority),
        active=data.get("active", rule.active),
    )
    for field, value in cleaned.items():
        setattr(rule, field, value)
    try:
        rule.save()
    except IntegrityError:
        raise ValidationError(
            f"A rule named {cleaned['name']!r} already exists."
        ) from None
    record_audit(rule.organization_id, user, "rule_updated", "rule", rule.id)
    return rule


def delete_rule(*, rule: PricingRule, user) -> None:
    """Delete a pricing rule. Operating roles only; audited."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, rule.organization_id)
    record_audit(rule.organization_id, user, "rule_deleted", "rule", rule.id)
    rule.delete()


def _group_key(offer) -> tuple:
    return (
        (offer.brand or "").strip().lower(),
        (offer.model_name or "").strip().lower(),
        offer.capacity_gb,
        (offer.grade or "").strip().lower(),
        (offer.currency or "").strip().upper(),
    )


def _rule_matches(rule: PricingRule, attrs: dict) -> bool:
    for key, value in (rule.matcher or {}).items():
        if attrs.get(key, "").strip().lower() != value.strip().lower():
            return False
    return True


def _winning_rule(rules: list, attrs: dict):
    matching = [rule for rule in rules if rule.active and _rule_matches(rule, attrs)]
    matching.sort(key=lambda rule: (-len(rule.matcher or {}), rule.priority, rule.name))
    return matching[0] if matching else None


def _pick(candidates: list, strategy: str, preferred: str):
    if strategy == SupplierStrategy.PREFERRED:
        want = (preferred or "").strip().lower()
        for offer in candidates:
            if (offer.supplier or "").strip().lower() == want:
                return offer
        return None
    if strategy == SupplierStrategy.HIGHEST:
        return max(candidates, key=lambda offer: offer.unit_price)
    return min(candidates, key=lambda offer: offer.unit_price)


def _apply_ops(price: Decimal, currency: str, ops: list, fx: dict) -> tuple:
    rates = fx.get("rates") or {}
    destination = (fx.get("destination") or "").strip().upper()
    for op in ops or []:
        if op["op"] == "convert":
            price = price * Decimal(op["rate"])
            currency = op["to"]
        elif op["op"] == "convert_to_destination":
            if not destination:
                raise FxRateMissing(
                    "no destination currency in Settings",
                    detail="no destination currency in Settings",
                )
            if currency != destination:
                rate = rates.get(currency)
                if rate is None:
                    raise FxRateMissing(
                        f"no recorded {currency}->{destination} rate",
                        detail=(
                            f"no recorded {currency}->{destination} rate "
                            "for this file (set the file's currency date)"
                        ),
                    )
                price = price * Decimal(rate)
                currency = destination
        elif op["op"] == "percent":
            price = price * (1 + Decimal(op["value"]) / 100)
        elif op["op"] == "round":
            step = Decimal(op["step"])
            price = (price / step).to_integral_value(rounding=ROUND_HALF_UP) * step
    return price, currency


def _file_fx(run) -> dict:
    """The file's recorded rate table plus display provenance."""
    source = run.source_file
    day = source.currency_date or source.created_at.date()
    return {
        "currency_date": day.isoformat(),
        "destination": source.fx_destination or None,
        "rates": dict(source.fx_rates or {}),
        "source": source.fx_source or None,
        "simulated": bool(source.fx_simulated),
        "fetched_at": (
            source.fx_fetched_at.isoformat() if source.fx_fetched_at else None
        ),
    }


def build_quote(run) -> dict:
    """Group ok offers, pick one supplier per group, apply the winning rule."""
    offers = list(run.offers.filter(status=OfferStatus.OK).order_by("id"))
    rules = list(run.organization.pricing_rules.filter(active=True))
    fx = _file_fx(run)
    groups: dict = {}
    for offer in offers:
        groups.setdefault(_group_key(offer), []).append(offer)
    lines, notes = [], []
    for key, candidates in sorted(groups.items(), key=lambda item: [str(part) for part in item[0]]):
        first = candidates[0]
        attrs = {
            "brand": first.brand or "",
            "model": first.model_name or "",
            "grade": first.grade or "",
        }
        rule = _winning_rule(rules, attrs)
        strategy = rule.supplier_strategy if rule else SupplierStrategy.LOWEST
        preferred = rule.preferred_supplier if rule else ""
        picked = _pick(candidates, strategy, preferred)
        label = " ".join(
            part
            for part in (
                attrs["brand"],
                attrs["model"],
                f"{first.capacity_gb}GB" if first.capacity_gb is not None else "",
            )
            if part
        )
        if picked is None:
            notes.append(f"Skipped {label}: preferred supplier {preferred!r} not found.")
            continue
        try:
            price, currency = _apply_ops(
                picked.unit_price, first.currency, rule.ops if rule else [], fx
            )
        except FxRateMissing as exc:
            notes.append(f"Skipped {label}: {exc.detail}.")
            continue
        lines.append(
            {
                "brand": attrs["brand"],
                "model": attrs["model"],
                "capacity_gb": first.capacity_gb,
                "grade": attrs["grade"],
                "supplier": picked.supplier,
                "quantity": picked.quantity,
                "unit_price": str(price),
                "currency": currency,
                "rule": rule.name if rule else None,
            }
        )
    return {
        "run_id": run.id,
        "run_status": run.status,
        "lines": lines,
        "notes": notes,
        "fx": fx,
    }


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def render_quote_csv(quote: dict) -> bytes:
    """Render quote lines as CSV bytes."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(QUOTE_COLUMNS)
    for line in quote["lines"]:
        writer.writerow(
            [
                sanitize_cell(line["brand"]),
                sanitize_cell(line["model"]),
                _cell(line["capacity_gb"]),
                sanitize_cell(line["grade"]),
                sanitize_cell(line["supplier"]),
                _cell(line["quantity"]),
                line["unit_price"],
                sanitize_cell(line["currency"]),
                sanitize_cell(line["rule"] or ""),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def render_quote_xlsx(quote: dict) -> bytes:
    """Render quote lines as xlsx bytes (prices as numbers)."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Quote"
    sheet.append(QUOTE_COLUMNS)
    for line in quote["lines"]:
        sheet.append(
            [
                sanitize_cell(line["brand"]),
                sanitize_cell(line["model"]),
                line["capacity_gb"],
                sanitize_cell(line["grade"]),
                sanitize_cell(line["supplier"]),
                line["quantity"],
                float(line["unit_price"]),
                sanitize_cell(line["currency"]),
                sanitize_cell(line["rule"] or ""),
            ]
        )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
