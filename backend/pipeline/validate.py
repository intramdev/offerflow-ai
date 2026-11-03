"""Mandatory deterministic gates.

The application invokes these at the validation boundary; no caller can
skip them. A failed critical invariant blocks release regardless of any
other signal, and a validator that errors means unverified, never approved.

Value gates compare each offer against its own source-row evidence plus
the recorded human decisions that legitimately override a cell — never
bare totals, never an unresolved reference.
"""

from __future__ import annotations

from decimal import Decimal

from pipeline.models import (
    OfferStatus,
    ReviewDecision,
    ReviewKind,
    ValidationResult,
)
from pipeline.normalize import (
    DOMAIN_DEVICE,
    LOCKED,
    REVIEWABLE_FIELDS,
    has_quantity_marking,
    merge_interpreted_cells,
    normalize_cells,
    parse_price,
    parse_quantity,
    parse_review_value,
)
from pipeline.normalize import terminal_amount as _terminal_span

# The database stores prices at four decimals; anything within half a
# ten-thousandth is storage rounding, not tampering.
_PRICE_TOLERANCE = Decimal("0.00005")

# Offer attributes checked against decided-or-evidence values. The
# normalized evidence uses the same names.
_EVIDENCE_FIELDS = (
    "supplier",
    "model_name",
    "capacity_gb",
    "grade",
    "lock_state",
    "quantity",
    "unit_price",
    "currency",
)


def _live_decisions(run):
    """Latest human decision per (offer, field).

    Mapping answers carry no offer; they already rewrote the row cells,
    so they need no separate credit here.
    """
    decisions = {}
    rows = (
        ReviewDecision.objects.filter(request__run=run)
        .select_related("request")
        .order_by("id")
    )
    for decision in rows:
        request = decision.request
        if request.offer_id is None:
            continue
        decisions[(request.offer_id, request.field)] = decision
    return decisions


def _is_ignored(decision) -> bool:
    value = decision.value
    return isinstance(value, dict) and value.get("ignored") is True


def _is_not_applicable(decision) -> bool:
    value = decision.value
    return isinstance(value, dict) and value.get("not_applicable") is True


def _decided_value(decision, field):
    """The value a decision authorizes, or None when it carries none.

    Ignore-decisions authorize exclusion, never a value; choices carry
    the picked grade or lock state verbatim. Not-applicable decisions
    likewise authorize no value: the absent evidence stands as-is.
    """
    if _is_ignored(decision) or _is_not_applicable(decision):
        return None
    if decision.request.kind == ReviewKind.CHOICE:
        return decision.choice or None
    if field in REVIEWABLE_FIELDS:
        return parse_review_value(field, decision.value)
    return None


def prices_match(actual, expected) -> bool:
    """Compare two prices within storage-rounding tolerance."""
    if actual is None or expected is None:
        return actual is None and expected is None
    return abs(Decimal(str(actual)) - Decimal(str(expected))) <= _PRICE_TOLERANCE


def _conflict_key(offer) -> tuple:
    """Identity for same-file price conflicts.

    Deliberately conservative: brand, capacity, color, grade, lock, and
    currency differences all mark true variants, which never conflict.
    Same-file scope matches one supplier price list with one effective
    date; cross-file supersession is replacement, not conflict.
    """

    def norm(text):
        return (text or "").strip().casefold()

    return (
        norm(offer.supplier),
        norm(offer.brand),
        norm(offer.model_name),
        offer.capacity_gb,
        norm(offer.color),
        norm(offer.grade),
        offer.lock_state,
        offer.currency,
    )


def price_label(value) -> str:
    """Compact canonical label for one readable price (choice round-trip).

    Plain fixed notation: Decimal.normalize() alone would render 20 as
    "2E+1", which is correct but unreadable as a review choice.
    """
    return format(Decimal(str(value)).normalize(), "f")


def find_price_conflicts(offers) -> list:
    """Group non-excluded offers whose identical products disagree on price.

    Returns [{"offers", "prices"}] with prices as normalized labels
    sorted numerically. Rows without a readable price keep their own
    value gap and never join a conflict.
    """
    groups: dict = {}
    for offer in offers:
        if offer.status == OfferStatus.EXCLUDED:
            continue
        groups.setdefault(_conflict_key(offer), []).append(offer)
    conflicts = []
    for members in groups.values():
        priced = [offer for offer in members if offer.unit_price is not None]
        distinct: list = []
        for offer in priced:
            if any(
                prices_match(offer.unit_price, other.unit_price)
                for other in distinct
            ):
                continue
            distinct.append(offer)
        if len(distinct) > 1:
            labels = sorted(
                {price_label(offer.unit_price) for offer in distinct},
                key=Decimal,
            )
            conflicts.append({"offers": priced, "prices": labels})
    return conflicts


def _run_domain(run) -> str:
    return (run.mapping or {}).get("domain", DOMAIN_DEVICE)


def _expected_fields(
    offer, document_policy: str = "stock", domain: str = DOMAIN_DEVICE
) -> dict:
    """Normalized evidence for one offer, model cells folded in narrowly."""
    row = offer.source_row
    cells = merge_interpreted_cells(
        dict(row.data or {}), offer.interpreted or {},
        document_policy=document_policy,
        domain=domain,
    )
    return normalize_cells(cells)


def _result(run, name: str, passed: bool, message: str = "", details: dict | None = None):
    return ValidationResult(
        organization_id=run.organization_id,
        run=run,
        name=name,
        passed=passed,
        message=message,
        details=details or {},
    )


def evaluate_validations(run) -> list:
    """Evaluate every gate without writing rows (release rechecks)."""
    return [
        _check_review_coverage(run),
        _check_criticals_complete(run),
        _check_quantities_reconcile(run),
        _check_provenance_complete(run),
        _check_rows_reconcile_with_evidence(run),
        _check_roles_bound_to_evidence(run),
        _check_no_merge(run),
        _check_price_conflicts_resolved(run),
        _check_releasable_or_explicit_empty(run),
    ]


def run_validations(run) -> tuple[bool, list]:
    """Replace the run's validation results with a fresh full evaluation."""
    ValidationResult.objects.filter(run=run).delete()
    ValidationResult.objects.bulk_create(evaluate_validations(run))
    stored = list(ValidationResult.objects.filter(run=run).order_by("name"))
    return all(item.passed for item in stored), stored


def _check_review_coverage(run):
    open_count = run.reviews.filter(status="open").count()
    if open_count:
        return _result(
            run, "review_coverage", False, f"{open_count} open review requests."
        )
    return _result(run, "review_coverage", True, "No open review requests.")


def _check_criticals_complete(run):
    pending = list(
        run.offers.exclude(status=OfferStatus.OK)
        .exclude(status=OfferStatus.EXCLUDED)
        .values_list("id", flat=True)
    )
    if pending:
        return _result(
            run,
            "criticals_complete",
            False,
            f"{len(pending)} offers still need input.",
            {"offer_ids": pending},
        )
    return _result(run, "criticals_complete", True, "All offers resolved or excluded.")


def _check_quantities_reconcile(run):
    decisions = _live_decisions(run)
    rows = list(run.source_rows.select_related("offer").order_by("index"))
    expected_total = 0
    unparseable = 0
    for row in rows:
        raw = parse_quantity((row.data or {}).get("quantity"))
        decided = None
        offer = getattr(row, "offer", None)
        if offer is not None:
            decision = decisions.get((offer.id, "quantity"))
            if decision is not None:
                decided = _decided_value(decision, "quantity")
        if raw is None and decided is None and offer is not None:
            # No readable cell and no human decision: reconcile against
            # the same merged evidence the per-row gate authorizes, so a
            # model-filled quantity gap counts exactly once on each side.
            # Without a proposal this still yields None (unparseable).
            raw = _expected_fields(
                offer,
                (run.mapping or {}).get("document_policy", "stock"),
                _run_domain(run),
            )["quantity"]
        # An approved clarification supplies the quantity the raw
        # cell could not; only rows with neither stay unparseable.
        if raw is None and decided is None:
            unparseable += 1
        else:
            expected_total += decided if decided is not None else raw
    offers = list(run.offers.all())
    offer_total = sum(offer.quantity or 0 for offer in offers)
    missing = sum(1 for offer in offers if offer.quantity is None)
    passed = (
        offer_total == expected_total
        and missing == unparseable
        and len(offers) == len(rows)
    )
    return _result(
        run,
        "quantities_reconcile",
        passed,
        "Quantities reconcile across rows and offers."
        if passed
        else "Quantity totals disagree between source rows and offers.",
        {
            "source_total": expected_total,
            "offer_total": offer_total,
            "rows": len(rows),
            "offers": len(offers),
        },
    )


def _check_provenance_complete(run):
    bad = []
    for offer in run.offers.select_related("source_row").order_by("id"):
        row = offer.source_row
        reference = offer.provenance or {}
        # A reference counts only when it resolves to the offer's own
        # source row; a well-formed pointer elsewhere is forgery.
        if (
            row.run_id != run.id
            or reference.get("sheet") != row.sheet
            or reference.get("row") != row.index
        ):
            bad.append(offer.id)
    if bad:
        return _result(
            run,
            "provenance_complete",
            False,
            f"{len(bad)} offers lack a resolvable source reference.",
            {"offer_ids": bad},
        )
    return _result(
        run, "provenance_complete", True, "Every offer resolves to its source row."
    )


def _check_rows_reconcile_with_evidence(run):
    decisions = _live_decisions(run)
    failures = []
    offers = (
        run.offers.select_related("source_row")
        .exclude(status=OfferStatus.EXCLUDED)
        .order_by("id")
    )
    policy = (run.mapping or {}).get("document_policy", "stock")
    domain = _run_domain(run)
    for offer in offers:
        row = offer.source_row
        expected = _expected_fields(offer, policy, domain)
        bad_fields = []
        actual_map = {}
        expected_map = {}

        def note(field, actual, want):
            bad_fields.append(field)
            actual_map[field] = "" if actual is None else str(actual)
            expected_map[field] = "" if want is None else str(want)

        for attr in _EVIDENCE_FIELDS:
            actual = getattr(offer, attr)
            decision = decisions.get((offer.id, attr))
            decided = (
                _decided_value(decision, attr) if decision is not None else None
            )
            want = decided if decided is not None else expected[attr]
            if attr == "unit_price":
                authorized = prices_match(actual, want)
            else:
                authorized = actual == want
            if not authorized:
                note(attr, actual, want)
        for attr in ("brand", "color", "conditions"):
            # Optional cells may be adopted from the recorded proposal
            # when the source left them empty (see
            # merge_interpreted_cells), so they reconcile against the
            # same merged expectation as every other field — not
            # against raw source cells alone.
            actual = getattr(offer, attr)
            want = expected[attr]
            if actual != want:
                note(attr, actual, want)
        if offer.lock_state == LOCKED:
            # Locked stock is never releasable, whatever the status says.
            if "lock_state" not in bad_fields:
                note("lock_state", LOCKED, "not locked")
        if bad_fields:
            failures.append(
                {
                    "offer_id": offer.id,
                    "sheet": row.sheet,
                    "row": row.index,
                    "fields": bad_fields,
                    "expected": expected_map,
                    "actual": actual_map,
                }
            )
    if failures:
        return _result(
            run,
            "rows_reconcile_with_evidence",
            False,
            f"{len(failures)} offer(s) differ from their source evidence "
            "without a recorded decision.",
            {"offers": failures},
        )
    return _result(
        run,
        "rows_reconcile_with_evidence",
        True,
        "Every offer traces to its source row or a recorded decision.",
    )


def _check_roles_bound_to_evidence(run):
    """Terminal-scoped rows bind price and quantity to source spans.

    Numeric presence is necessary but insufficient: a terminal-scoped
    offer's price must equal the terminal token re-read from its own
    line at the recorded span, and its quantity must be absent, marked
    on the line, human-decided, or matching a reviewed row cell.
    Capacity/model digits can never satisfy either role through this
    gate.
    """
    if (run.mapping or {}).get("free_text") is not True:
        return _result(
            run,
            "roles_bound_to_evidence",
            True,
            "No message-lane numeric roles to bind.",
        )
    decisions = _live_decisions(run)
    failures = []
    scoped = 0
    offers = (
        run.offers.select_related("source_row")
        .exclude(status=OfferStatus.EXCLUDED)
        .order_by("id")
    )
    for offer in offers:
        row = offer.source_row
        provenance = row.provenance or {}
        if not provenance.get("terminal_scope"):
            continue
        scoped += 1
        text = (row.data or {}).get("message") or ""
        bound = provenance.get("terminal") or {}
        fields = []
        expected = {}
        actual = {}
        found = _terminal_span(text)
        price_decision = decisions.get((offer.id, "unit_price"))
        price_decided = (
            price_decision is not None
            and _decided_value(price_decision, "unit_price") is not None
        )
        if not price_decided:
            if found is None:
                fields.append("unit_price")
                expected["unit_price"] = "resolvable terminal amount"
                actual["unit_price"] = "unresolvable"
            else:
                token, start, end = found
                want = parse_price(token)
                recorded = bound.get("span")
                if recorded is not None and recorded != [start, end]:
                    fields.append("unit_price")
                    expected["unit_price"] = f"span {recorded}"
                    actual["unit_price"] = f"span {[start, end]}"
                elif want is None or not prices_match(offer.unit_price, want):
                    fields.append("unit_price")
                    expected["unit_price"] = "" if want is None else str(want)
                    actual["unit_price"] = (
                        "" if offer.unit_price is None else str(offer.unit_price)
                    )
        currency_decision = decisions.get((offer.id, "currency"))
        if bound.get("currency") and (
            (currency_decision is None)
            or _decided_value(currency_decision, "currency") is None
        ):
            if offer.currency != bound["currency"]:
                fields.append("currency")
                expected["currency"] = bound["currency"]
                actual["currency"] = offer.currency or ""
        quantity_decision = decisions.get((offer.id, "quantity"))
        quantity_decided = (
            quantity_decision is not None
            and _decided_value(quantity_decision, "quantity") is not None
        )
        # Row cells carry only source values and reviewed corrections:
        # the model fills gaps into offer.interpreted, never back into
        # row.data. A quantity matching its cell (for example an
        # accepted "out of stock means zero" correction) is reviewed
        # evidence, not an invention.
        cell_quantity = parse_quantity((row.data or {}).get("quantity"))
        quantity_cell_ok = (
            cell_quantity is not None and cell_quantity == offer.quantity
        )
        if (
            offer.quantity is not None
            and not quantity_decided
            and not quantity_cell_ok
            and not has_quantity_marking(text)
        ):
            fields.append("quantity")
            expected["quantity"] = "marked on the line or reviewed"
            actual["quantity"] = str(offer.quantity)
        if fields:
            failures.append(
                {
                    "offer_id": offer.id,
                    "sheet": row.sheet,
                    "row": row.index,
                    "fields": fields,
                    "expected": expected,
                    "actual": actual,
                }
            )
    if failures:
        return _result(
            run,
            "roles_bound_to_evidence",
            False,
            f"{len(failures)} terminal-scoped offer(s) misbind numeric roles.",
            {"offers": failures},
        )
    if not scoped:
        return _result(
            run, "roles_bound_to_evidence", True, "No terminal-scoped rows."
        )
    return _result(
        run,
        "roles_bound_to_evidence",
        True,
        f"{scoped} terminal-scoped offer(s) bind to their source spans.",
    )


def _check_no_merge(run):
    offer_rows = set(run.offers.values_list("source_row_id", flat=True))
    row_ids = set(run.source_rows.values_list("id", flat=True))
    passed = offer_rows == row_ids and run.offers.count() == run.source_rows.count()
    return _result(
        run,
        "no_incompatible_merge",
        passed,
        "Each offer maps to exactly one source row; nothing was merged."
        if passed
        else "Offer-to-row mapping is not one to one.",
    )


def _check_price_conflicts_resolved(run):
    offers = list(
        run.offers.select_related("source_row").order_by("id")
    )
    conflicts = find_price_conflicts(offers)
    if conflicts:
        detail = [
            {
                "offer_ids": [offer.id for offer in item["offers"]],
                "prices": item["prices"],
            }
            for item in conflicts
        ]
        return _result(
            run,
            "price_conflicts_resolved",
            False,
            f"{len(conflicts)} product(s) carry conflicting prices.",
            {"conflicts": detail},
        )
    return _result(
        run,
        "price_conflicts_resolved",
        True,
        "No conflicting prices among quotable offers.",
    )


def _check_releasable_or_explicit_empty(run):
    ok_count = run.offers.filter(status=OfferStatus.OK).count()
    excluded_count = run.offers.filter(status=OfferStatus.EXCLUDED).count()
    pending_count = run.offers.filter(status=OfferStatus.NEEDS_INPUT).count()
    if ok_count > 0:
        return _result(
            run,
            "releasable_or_explicit_empty",
            True,
            f"{ok_count} releasable offers.",
        )
    if pending_count == 0 and excluded_count > 0:
        return _result(
            run,
            "releasable_or_explicit_empty",
            True,
            "All rows explicitly excluded; nothing releasable.",
        )
    return _result(
        run,
        "releasable_or_explicit_empty",
        False,
        "No releasable offers and rows still need input.",
    )
