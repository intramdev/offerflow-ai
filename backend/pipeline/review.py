"""Clarification review: answers, re-evaluation, and approval binding."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils import timezone

from pipeline.export import normalized_digest
from pipeline.models import (
    NormalizedOffer,
    OfferStatus,
    ReviewDecision,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    RunStatus,
    transition_run,
)
from pipeline.normalize import (
    DOMAIN_BY_CHOICE,
    DOMAIN_CHOICES,
    DOMAIN_DEVICE,
    DOMAIN_GENERAL,
    LOCKED,
    UNLOCKED,
    compute_offer_status,
    parse_review_value,
)
from pipeline.validate import (
    find_price_conflicts,
    price_label,
    prices_match,
    run_validations,
)

_MISSING = object()

#: File-wide question asking whether device fields apply to the file.
DOMAIN_FIELD = "domain"

#: Per-offer question choosing the authoritative price within a file.
CONFLICT_FIELD = "price_conflict"

#: Device-only gap fields a general-domain answer stands down.
_DEVICE_GAP_FIELDS = ("capacity_gb", "grade")


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


def raise_domain_question(*, run):
    """Ask once whether a device-evidence-free file is general-domain.

    Returns the created review, or None when the file already shows
    device data, asks no device gaps, or already carries the question.
    Table lane only: a spreadsheet without capacity/grade columns is
    decisive evidence, while message prose may carry device meaning
    the parser has not extracted yet, so pasted lines keep the
    established per-row questions.
    """
    stored = run.mapping or {}
    if stored.get("free_text") is True:
        return None
    if stored.get("domain") in (DOMAIN_DEVICE, DOMAIN_GENERAL):
        return None
    if run.reviews.filter(offer__isnull=True, field=DOMAIN_FIELD).exists():
        return None
    if not run.reviews.filter(
        status=ReviewStatus.OPEN,
        offer__isnull=False,
        field__in=_DEVICE_GAP_FIELDS,
    ).exists():
        return None
    if (
        run.offers.exclude(capacity_gb__isnull=True).exists()
        or run.offers.exclude(grade="").exists()
    ):
        return None
    count = run.offers.count()
    rows = "1 row" if count == 1 else f"{count} rows"
    question = (
        f"This file gives no capacity or grade information on any of "
        f"its {rows}. Are these phones or devices, where capacity and "
        f"grade are required for each row, or other products, where "
        f"capacity and grade do not apply?"
    )
    return ReviewRequest.objects.create(
        organization_id=run.organization_id,
        run=run,
        offer=None,
        kind=ReviewKind.CHOICE,
        field=DOMAIN_FIELD,
        question=question,
        evidence={"rows": count},
        choices=list(DOMAIN_CHOICES),
    )


def _mark_answered(review: ReviewRequest) -> None:
    review.status = ReviewStatus.ANSWERED
    review.resolved_at = timezone.now()
    review.save(update_fields=["status", "resolved_at"])


def _answer_domain(*, review: ReviewRequest, user, choice: str = ""):
    """Record the file's product domain; stand down N/A device gaps."""
    from pipeline.jobs import record_audit

    run = review.run
    from organizations.context import set_tenant_context

    set_tenant_context(run.organization_id, user.id if user is not None else 0)
    if run.status != RunStatus.NEEDS_INPUT:
        raise ValidationError("That run is not awaiting input.")
    if not choice or choice not in (review.choices or []):
        raise ValidationError("Pick one of the offered answers.")
    domain = DOMAIN_BY_CHOICE.get(choice)
    if domain is None:
        raise ValidationError("Pick one of the offered answers.")
    stored = dict(run.mapping or {})
    stored["domain"] = domain
    run.mapping = stored
    run.save(update_fields=["mapping", "updated_at"])
    _mark_answered(review)
    decision = ReviewDecision.objects.create(
        organization_id=run.organization_id,
        request=review,
        decided_by=user,
        choice=choice,
        value={"domain": domain},
    )
    if domain == DOMAIN_GENERAL:
        for open_review in run.reviews.filter(
            status=ReviewStatus.OPEN,
            offer__isnull=False,
            field__in=_DEVICE_GAP_FIELDS,
        ):
            _mark_answered(open_review)
            ReviewDecision.objects.create(
                organization_id=run.organization_id,
                request=open_review,
                decided_by=user,
                choice="",
                value={"not_applicable": True},
            )
        policy = stored.get("document_policy", "stock")
        for offer in run.offers.all():
            if offer.status == OfferStatus.EXCLUDED:
                continue
            status, _gaps, reason = compute_offer_status(
                _offer_fields(offer),
                document_policy=policy,
                domain=domain,
            )
            offer.status = status
            offer.exclusion_reason = reason
            if run.reviews.filter(
                offer=offer, status=ReviewStatus.OPEN
            ).exists():
                offer.status = OfferStatus.NEEDS_INPUT
            offer.save()
    _settle_run(run, user)
    record_audit(
        run.organization_id, user, "review_answered", "review", review.id,
        {"field": review.field, "domain": domain},
    )
    return decision


def _conflict_question(offers: list, prices: list) -> str:
    first = offers[0]
    clauses = []
    for offer in sorted(offers, key=lambda item: item.source_row.index):
        label = next(
            label
            for label in prices
            if prices_match(offer.unit_price, Decimal(label))
        )
        clauses.append(
            f"{label} {offer.currency} on sheet row {offer.source_row.index}"
        )
    return (
        f"\u201c{first.model_name}\u201d from {first.supplier} is priced "
        f"differently in this file: {'; '.join(clauses)}. Choose the "
        f"authoritative current price; the other row stays on record but "
        f"leaves the quote. If neither price is confirmed, leave this "
        f"open and ask the supplier."
    )


def reconcile_price_conflicts(*, run) -> None:
    """Create, refresh, or retire same-file price-conflict questions.

    Open questions always reflect the live group: changed groups get
    refreshed wording and choices, and dissolved groups retire as moot
    instead of dangling. Answered decisions are never rewritten.
    """
    conflicts = find_price_conflicts(
        run.offers.select_related("source_row").order_by("id")
    )
    live_offer_ids: set = set()
    for item in conflicts:
        offers = item["offers"]
        prices = item["prices"]
        question = _conflict_question(offers, prices)
        offer_ids = [offer.id for offer in offers]
        for offer in offers:
            live_offer_ids.add(offer.id)
            evidence = {
                "provenance": offer.provenance,
                "price_conflict": True,
                "offer_ids": offer_ids,
                "prices": prices,
            }
            existing = run.reviews.filter(
                offer=offer, field=CONFLICT_FIELD, status=ReviewStatus.OPEN
            ).first()
            if existing is None:
                ReviewRequest.objects.create(
                    organization_id=run.organization_id,
                    run=run,
                    offer=offer,
                    kind=ReviewKind.CHOICE,
                    field=CONFLICT_FIELD,
                    question=question,
                    evidence=evidence,
                    choices=list(prices),
                )
            else:
                existing.question = question
                existing.choices = list(prices)
                existing.evidence = evidence
                existing.save(
                    update_fields=["question", "choices", "evidence"]
                )
    for stale in run.reviews.filter(
        field=CONFLICT_FIELD, status=ReviewStatus.OPEN
    ).exclude(offer_id__in=live_offer_ids):
        _mark_answered(stale)
        ReviewDecision.objects.create(
            organization_id=run.organization_id,
            request=stale,
            decided_by=None,
            choice="",
            value={"moot": True},
        )


def _answer_price_conflict(*, review: ReviewRequest, user, choice: str = ""):
    """Authorize one conflicting price; supersede the losing rows.

    Losing rows are excluded with their provenance intact, never
    deleted or revalued. Every row is preserved for the record.
    """
    from pipeline.jobs import record_audit

    run = review.run
    from organizations.context import set_tenant_context

    set_tenant_context(run.organization_id, user.id if user is not None else 0)
    if run.status != RunStatus.NEEDS_INPUT:
        raise ValidationError("That run is not awaiting input.")
    if not choice or choice not in (review.choices or []):
        raise ValidationError("Pick one of the offered prices.")
    try:
        chosen = Decimal(choice)
    except InvalidOperation:
        raise ValidationError("That price is not valid.") from None
    offer_ids = (review.evidence or {}).get("offer_ids") or []
    group = [
        offer
        for offer in NormalizedOffer.objects.filter(
            id__in=offer_ids, run=run
        ).select_related("source_row")
        if offer.status != OfferStatus.EXCLUDED
    ]
    if not group:
        raise ValidationError("Those rows are no longer part of this run.")
    survivors = {
        offer.id
        for offer in group
        if offer.unit_price is not None
        and prices_match(offer.unit_price, chosen)
    }
    if not survivors:
        raise ValidationError(
            "That price matches no remaining row; the conflict changed."
        )
    stored = run.mapping or {}
    policy = stored.get("document_policy", "stock")
    domain = stored.get("domain", DOMAIN_DEVICE)
    currency = group[0].currency
    display = price_label(chosen)
    answered_decision = None
    for offer in group:
        if offer.id in survivors:
            for conflict in run.reviews.filter(
                offer=offer, field=CONFLICT_FIELD, status=ReviewStatus.OPEN
            ):
                _mark_answered(conflict)
                decision = ReviewDecision.objects.create(
                    organization_id=run.organization_id,
                    request=conflict,
                    decided_by=user,
                    choice=choice,
                    value=None,
                )
                if conflict.id == review.id:
                    answered_decision = decision
            status, _gaps, reason = compute_offer_status(
                _offer_fields(offer),
                document_policy=policy,
                domain=domain,
            )
            offer.status = status
            offer.exclusion_reason = reason
            if run.reviews.filter(
                offer=offer, status=ReviewStatus.OPEN
            ).exists():
                offer.status = OfferStatus.NEEDS_INPUT
            offer.save()
        else:
            offer.status = OfferStatus.EXCLUDED
            offer.exclusion_reason = (
                "Superseded by price-conflict decision "
                f"(review {review.id}): chose {display} {currency}."
            )[:255]
            offer.save()
            for open_review in run.reviews.filter(
                offer=offer, status=ReviewStatus.OPEN
            ):
                _mark_answered(open_review)
                if open_review.field == CONFLICT_FIELD:
                    value: dict | None = {"superseded_by": choice}
                    decided_choice = choice
                else:
                    value = {"ignored": True, "superseded_by": choice}
                    decided_choice = ""
                decision = ReviewDecision.objects.create(
                    organization_id=run.organization_id,
                    request=open_review,
                    decided_by=user,
                    choice=decided_choice,
                    value=value,
                )
                if open_review.id == review.id:
                    answered_decision = decision
    reconcile_price_conflicts(run=run)
    _settle_run(run, user)
    record_audit(
        run.organization_id, user, "review_answered", "review", review.id,
        {"field": review.field, "choice": choice},
    )
    record_audit(
        run.organization_id, user, "price_conflict_resolved", "run", run.id,
        {"choice": choice, "offer_ids": [offer.id for offer in group]},
    )
    return answered_decision


def answer_request(*, review: ReviewRequest, user, choice: str = "", value=_MISSING):
    """Record an answer, re-evaluate the offer, and advance the run."""
    from pipeline.jobs import record_audit

    if review.status != ReviewStatus.OPEN:
        raise ValidationError("That review request is already answered.")
    if review.offer_id is None:
        if review.field == DOMAIN_FIELD:
            return _answer_domain(review=review, user=user, choice=choice)
        return _answer_mapping(review=review, user=user, choice=choice)
    if review.kind == ReviewKind.CHOICE and review.field == CONFLICT_FIELD:
        return _answer_price_conflict(review=review, user=user, choice=choice)
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

    stored = review.run.mapping or {}
    policy = stored.get("document_policy", "stock")
    domain = stored.get("domain", DOMAIN_DEVICE)
    status, gaps, reason = compute_offer_status(
        _offer_fields(offer), document_policy=policy, domain=domain
    )
    if (
        run.reviews.filter(offer=offer, status=ReviewStatus.OPEN)
        .exclude(id=review.id)
        .exists()
    ):
        # Another question (for example a price conflict) still
        # constrains this row, so it cannot go OK yet.
        status, reason = OfferStatus.NEEDS_INPUT, ""
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
    # An answer can form a new conflict (a filled price joining a
    # same-identity row) or leave a changed group behind.
    reconcile_price_conflicts(run=run)
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
    # Excluding a row can dissolve its conflict group; the surviving
    # rows' questions go moot instead of dangling.
    reconcile_price_conflicts(run=run)
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
