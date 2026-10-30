"""Mandatory deterministic gates.

The application invokes these at the validation boundary; no caller can
skip them. A failed critical invariant blocks release regardless of any
other signal, and a validator that errors means unverified, never approved.
"""

from __future__ import annotations

from pipeline.models import OfferStatus, ValidationResult
from pipeline.normalize import parse_quantity


def _result(run, name: str, passed: bool, message: str = "", details: dict | None = None):
    return ValidationResult(
        organization_id=run.organization_id,
        run=run,
        name=name,
        passed=passed,
        message=message,
        details=details or {},
    )


def run_validations(run) -> tuple[bool, list]:
    """Replace the run's validation results with a fresh full evaluation."""
    ValidationResult.objects.filter(run=run).delete()
    results = [
        _check_review_coverage(run),
        _check_criticals_complete(run),
        _check_quantities_reconcile(run),
        _check_provenance_complete(run),
        _check_no_merge(run),
        _check_releasable_or_explicit_empty(run),
    ]
    ValidationResult.objects.bulk_create(results)
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
    rows = list(run.source_rows.all())
    expected_total = 0
    unparseable = 0
    for row in rows:
        quantity = parse_quantity((row.data or {}).get("quantity"))
        if quantity is None:
            unparseable += 1
        else:
            expected_total += quantity
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
    bad = [
        offer.id
        for offer in run.offers.all()
        if not (offer.provenance or {}).get("sheet")
        or not (offer.provenance or {}).get("row")
    ]
    if bad:
        return _result(
            run,
            "provenance_complete",
            False,
            f"{len(bad)} offers lack source references.",
            {"offer_ids": bad},
        )
    return _result(
        run, "provenance_complete", True, "Every offer carries source references."
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
