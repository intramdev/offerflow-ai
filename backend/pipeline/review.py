"""Clarification review: answers, re-evaluation, and approval binding."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone

from pipeline.export import normalized_digest
from pipeline.models import (
    OfferStatus,
    ReviewDecision,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    RunStatus,
    transition_run,
)
from pipeline.normalize import (
    LOCKED,
    UNLOCKED,
    compute_offer_status,
    parse_review_value,
)
from pipeline.validate import run_validations

_MISSING = object()


def _apply_value(offer, field: str, raw) -> None:
    """Apply a reviewer-supplied value through the standard parsers."""
    if field == "supplier":
        text = parse_review_value(field, raw)
        if not text:
            raise ValidationError("Supplier must not be empty.")
        offer.supplier = text
    elif field == "model_name":
        text = parse_review_value(field, raw)
        if not text:
            raise ValidationError("Model must not be empty.")
        offer.model_name = text
    elif field == "capacity_gb":
        value = parse_review_value(field, raw)
        if value is None:
            raise ValidationError("Capacity must be gigabytes as a whole number.")
        offer.capacity_gb = value
    elif field == "grade":
        text = parse_review_value(field, raw)
        if not text:
            raise ValidationError("Grade must not be empty.")
        offer.grade = text
    elif field == "quantity":
        value = parse_review_value(field, raw)
        if value is None:
            raise ValidationError("Quantity must be a whole number.")
        offer.quantity = value
    elif field == "unit_price":
        value = parse_review_value(field, raw)
        if value is None:
            raise ValidationError("Unit price must be a positive price.")
        offer.unit_price = value
    elif field == "currency":
        value = parse_review_value(field, raw)
        if value is None:
            raise ValidationError("Currency must be a three-letter code.")
        offer.currency = value
    else:
        raise ValidationError(f"Field {field!r} cannot be answered with a value.")


def _offer_fields(offer) -> dict:
    if offer.interpreted is not None:
        cells = offer.interpreted
    elif offer.source_row_id:
        cells = offer.source_row.data or {}
    else:
        cells = {}
    return {
        "supplier": offer.supplier,
        "model_name": offer.model_name,
        "capacity_gb": offer.capacity_gb,
        "capacity_raw": cells.get("capacity"),
        "grade": offer.grade,
        "grade_raw": offer.grade_raw,
        "lock_state": offer.lock_state,
        "lock_raw": cells.get("lock"),
        "quantity": offer.quantity,
        "quantity_raw": cells.get("quantity"),
        "unit_price": offer.unit_price,
        "price_raw": cells.get("price"),
        "currency": offer.currency,
        "currency_raw": cells.get("currency"),
    }


def mapping_choice_label(header: str, position: int) -> str:
    """Render one mapping choice. The letter keeps duplicate headers distinct."""
    from pipeline.parsing import column_letter

    return f"{header} ({column_letter(position)})"


def mapping_choice_position(choice: str, headers: list) -> int:
    """Resolve a mapping choice back to its header position, refusing tampering."""
    name, separator, letter = choice.rpartition(" (")
    if not separator or not letter.endswith(")"):
        raise ValidationError("Pick one of the offered columns.")
    letter = letter[:-1]
    if not letter or not letter.isalpha() or not letter.isupper():
        raise ValidationError("Pick one of the offered columns.")
    position = 0
    for char in letter:
        position = position * 26 + (ord(char) - 64)
    position -= 1
    if position < 0 or position >= len(headers) or headers[position] != name:
        raise ValidationError("Pick one of the offered columns.")
    return position


def _answer_mapping(*, review: ReviewRequest, user, choice: str):
    """Record a column-mapping answer; reprocess once mapping is complete."""
    from organizations.context import set_tenant_context

    from pipeline.jobs import enqueue_run, record_audit
    from pipeline.parsing import HEADER_TOKENS

    run = review.run
    if run.status != RunStatus.NEEDS_INPUT:
        raise ValidationError("That run is not awaiting input.")
    if review.kind != ReviewKind.CHOICE:
        raise ValidationError("That question cannot be answered with a choice.")
    if not choice or choice not in (review.choices or []):
        raise ValidationError("Pick one of the offered columns.")
    if not review.field.startswith("mapping:"):
        raise ValidationError("That question is not a mapping question.")
    canonical = review.field[len("mapping:"):]
    if canonical not in HEADER_TOKENS:
        raise ValidationError("That question is not a mapping question.")
    set_tenant_context(run.organization_id, user.id if user is not None else 0)
    stored = run.mapping or {}
    headers = stored.get("headers") or []
    position = mapping_choice_position(choice, headers)
    effective = dict(stored.get("detected") or {})
    effective.update(stored.get("confirmed") or {})
    if position in effective.values() and effective.get(canonical) != position:
        raise ValidationError(f"Column {choice} is already mapped to another field.")
    confirmed = dict(stored.get("confirmed") or {})
    confirmed[canonical] = position
    stored["confirmed"] = confirmed
    run.mapping = stored
    run.save(update_fields=["mapping", "updated_at"])
    review.status = ReviewStatus.ANSWERED
    review.resolved_at = timezone.now()
    review.save(update_fields=["status", "resolved_at"])
    decision = ReviewDecision.objects.create(
        organization_id=run.organization_id,
        request=review,
        decided_by=user,
        choice=choice,
        value=None,
    )
    record_audit(
        run.organization_id, user, "review_answered", "review", review.id,
        {"field": review.field},
    )
    still_open = run.reviews.filter(
        offer__isnull=True, status=ReviewStatus.OPEN
    ).exists()
    if not still_open:
        transition_run(run, RunStatus.UPLOADED)
        enqueue_run(run.id)
    return decision


def answer_request(*, review: ReviewRequest, user, choice: str = "", value=_MISSING):
    """Record an answer, re-evaluate the offer, and advance the run."""
    from pipeline.jobs import record_audit

    if review.status != ReviewStatus.OPEN:
        raise ValidationError("That review request is already answered.")
    if review.offer_id is None:
        return _answer_mapping(review=review, user=user, choice=choice)
    run = review.run
    from organizations.context import set_tenant_context

    set_tenant_context(run.organization_id, user.id if user is not None else 0)
    if run.status != RunStatus.NEEDS_INPUT:
        raise ValidationError("That run is not awaiting input.")
    offer = review.offer
    decision_value = None
    if review.kind == ReviewKind.CHOICE:
        if not choice or choice not in (review.choices or []):
            raise ValidationError("Pick one of the offered choices.")
        if review.field == "grade":
            offer.grade = choice
        elif review.field == "lock_state":
            if choice not in (LOCKED, UNLOCKED):
                raise ValidationError("Lock state must be locked or unlocked.")
            offer.lock_state = choice
        else:
            raise ValidationError(f"Field {review.field!r} has no choice handling.")
    elif review.kind == ReviewKind.VALUE:
        if value is _MISSING:
            raise ValidationError("A value is required.")
        _apply_value(offer, review.field, value)
        decision_value = value
    else:
        raise ValidationError(f"Unknown review kind {review.kind!r}.")
    offer.save()

    policy = (review.run.mapping or {}).get("document_policy", "stock")
    status, gaps, reason = compute_offer_status(
        _offer_fields(offer), document_policy=policy
    )
    offer.status = status
    offer.exclusion_reason = reason
    offer.save()
    review.status = ReviewStatus.ANSWERED
    review.resolved_at = timezone.now()
    review.save(update_fields=["status", "resolved_at"])
    decision = ReviewDecision.objects.create(
        organization_id=run.organization_id,
        request=review,
        decided_by=user,
        choice=choice,
        value=decision_value,
    )
    open_keys = set(
        run.reviews.filter(status=ReviewStatus.OPEN).values_list("offer_id", "field")
    )
    provenance = dict(offer.provenance or {})
    raw_cells = (
        offer.interpreted if offer.interpreted is not None else offer.source_row.data
    )
    for gap in gaps:
        if (offer.id, gap["field"]) in open_keys:
            continue
        ReviewRequest.objects.create(
            organization_id=run.organization_id,
            run=run,
            offer=offer,
            kind=gap["kind"],
            field=gap["field"],
            question=gap["question"],
            evidence={"provenance": provenance, "raw": raw_cells},
            choices=gap.get("choices", []),
        )
    _settle_run(run, user)
    record_audit(
        run.organization_id, user, "review_answered", "review", review.id,
        {"field": review.field},
    )
    return decision


def _settle_run(run, user) -> None:
    """Re-validate and park the run at ready or back at needs-input."""
    from pipeline.jobs import record_audit

    transition_run(run, RunStatus.VALIDATION)
    passed, _results = run_validations(run)
    still_open = run.reviews.filter(status=ReviewStatus.OPEN).exists()
    if passed and not still_open:
        transition_run(run, RunStatus.READY_FOR_REVIEW)
        record_audit(run.organization_id, user, "run_ready", "run", run.id)
    else:
        transition_run(run, RunStatus.NEEDS_INPUT)


def ignore_request(*, review: ReviewRequest, user):
    """Drop one record from the quote instead of answering its question.

    The offer is excluded (it lands in the excluded export sheet, never
    the quote) and every open question on that record is resolved as
    ignored. File-wide mapping questions cannot be ignored: without a
    mapping there is no import at all.
    """
    from pipeline.jobs import record_audit

    if review.status != ReviewStatus.OPEN:
        raise ValidationError("That review request is already answered.")
    if review.offer_id is None:
        raise ValidationError(
            "Mapping questions cannot be ignored; the import needs a mapping."
        )
    run = review.run
    from organizations.context import set_tenant_context

    set_tenant_context(run.organization_id, user.id if user is not None else 0)
    if run.status != RunStatus.NEEDS_INPUT:
        raise ValidationError("That run is not awaiting input.")
    offer = review.offer
    actor = getattr(user, "username", None) or "reviewer"
    offer.status = OfferStatus.EXCLUDED
    offer.exclusion_reason = f"Record ignored by {actor}."
    offer.save()
    resolved = 0
    for open_review in run.reviews.filter(
        offer=offer, status=ReviewStatus.OPEN
    ):
        open_review.status = ReviewStatus.ANSWERED
        open_review.resolved_at = timezone.now()
        open_review.save(update_fields=["status", "resolved_at"])
        ReviewDecision.objects.create(
            organization_id=run.organization_id,
            request=open_review,
            decided_by=user,
            choice="",
            value={"ignored": True},
        )
        resolved += 1
    _settle_run(run, user)
    record_audit(
        run.organization_id, user, "review_ignored", "offer", offer.id,
        {"resolved": resolved},
    )
    return offer


def approve_run(*, run, user):
    """Approve a releasable run, binding approval to exact versions."""
    from pipeline.jobs import record_audit
    from pipeline.release import active_rules_digest

    if run.status != RunStatus.READY_FOR_REVIEW:
        raise ValidationError("Only runs awaiting review can be approved.")
    passed, _results = run_validations(run)
    if not passed or run.reviews.filter(status=ReviewStatus.OPEN).exists():
        raise ValidationError("Run is not releasable.")
    run.approved_by = user
    run.approved_at = timezone.now()
    run.approved_source_sha = run.source_file.sha256
    run.approved_normalized_digest = normalized_digest(run)
    run.approved_rules_digest = active_rules_digest(run.organization)
    run.save(
        update_fields=[
            "approved_by",
            "approved_at",
            "approved_source_sha",
            "approved_normalized_digest",
            "approved_rules_digest",
            "updated_at",
        ]
    )
    transition_run(run, RunStatus.APPROVED)
    record_audit(run.organization_id, user, "run_approved", "run", run.id)
    return run
