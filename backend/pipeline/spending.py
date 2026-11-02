"""Token budgets with atomic reservation.

Every paid dispatch reserves a conservative token bound before touching the
network and reconciles observed usage afterwards. Each reservation has
identity (a SpendReservation row), a lifecycle state, and an expiry, so
retries, redeliveries, and worker loss settle exactly once:

- held: budget and an in-flight slot are committed.
- reconciled: observed (or, when usage is missing, estimated) spend booked.
- released: the call failed before any possible spend (auth/validation
  refusal, never dispatched).
- uncertain: the call may have consumed (timeout, dropped connection,
  worker loss). Counters stay held — uncertain is never free.
- expired: an uncertain reservation past its grace horizon. The held
  estimate books as spend because the provider may have processed it.

Reservation, the live-slot check, and settlement all happen under row
locks, so concurrent workers cannot overspend, double-settle, or free a
live request's budget. Settling an already-terminal reservation is a
no-op returning the current allowance.

Credits are tokens 1:1, granted monthly. Each company's billing cycle is
anchored at its activation day and resets lazily (no scheduler): the first
reservation or dashboard read in a new cycle snaps the period counters.
tokens_spent is the grand total and is never reset. New paid allowances
start at zero and require explicit per-organization approval: refusing
loudly is the default, spending is the opt-in.

Token estimates are conservative upper bounds (see estimate_text_tokens),
never predictions: over-reserving only costs temporary headroom, while
under-reserving could overspend. Observed usage always wins at reconcile.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from pipeline.models import OperatorSpend, SpendAllowance, SpendReservation


class BudgetRefused(Exception):
    """The budget declined a paid dispatch. Never retried implicitly."""


@dataclass(frozen=True)
class Reservation:
    """Handle to a held SpendReservation row."""

    id: int
    allowance_id: int
    estimate_tokens: int


# --- Conservative token estimation ---------------------------------------
# Without the serving model's tokenizer, estimates must bound real usage
# from above for the content this app sends: supplier JSON that mixes
# latin text, numbers, and occasionally CJK characters. Typical BPE rates
# are ~4 chars/token for English but ~1-2 chars/token for CJK, so ceil
# (chars/3) covers both with margin. REQUEST_OVERHEAD_TOKENS covers
# message framing plus the JSON-schema preamble the provider attaches to
# every structured-output call. Observed usage reconciles exactly, so a
# generous bound only costs temporary headroom — never real spend.
CHARS_PER_TOKEN_BOUND = 3
REQUEST_OVERHEAD_TOKENS = 64


def estimate_text_tokens(text: str) -> int:
    """Conservative upper bound on tokens for one text payload."""
    if not text:
        return 0
    return max(1, -(-len(text) // CHARS_PER_TOKEN_BOUND))


def estimate_request_tokens(*, texts: list[str], max_output_tokens: int) -> int:
    """Conservative reservation size for one permitted request.

    Covers the bounded input, the full permitted output (the model may
    legally emit up to max_output_tokens), and protocol overhead. Callers
    that cannot afford this must shrink their request limits first —
    the reservation never silently assumes a smaller call.
    """
    if max_output_tokens < 0:
        raise ValueError("max_output_tokens must be non-negative.")
    incoming = sum(estimate_text_tokens(text or "") for text in texts)
    return incoming + int(max_output_tokens) + REQUEST_OVERHEAD_TOKENS


# --- Reservation lifecycle -------------------------------------------------
# Synchronous calls settle in the same process, so the hold TTL only guards
# worker loss: a held reservation whose owner died becomes uncertain, never
# free. Uncertain reservations hold their counters for a grace horizon and
# then book the held estimate as spend (the provider may have processed
# them). Both horizons are settings so operators can tighten them.
def _hold_ttl() -> timedelta:
    return timedelta(
        seconds=int(getattr(settings, "OFFERFLOW_RESERVATION_TTL_SECONDS", 3600))
    )


def _uncertain_grace() -> timedelta:
    return timedelta(
        seconds=int(
            getattr(settings, "OFFERFLOW_RESERVATION_UNCERTAIN_GRACE_SECONDS", 86400)
        )
    )


def _nominal_day(year: int, month: int, anchor_day: int) -> date:
    """The anchor's day-of-month, clamped to short months (Jan 31 -> Feb 28)."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(anchor_day, last_day))


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def current_period_start(anchor: date | None, today: date | None = None) -> date:
    """First day of the billing cycle containing today."""
    anchor = anchor or timezone.localdate()
    today = today or timezone.localdate()
    nominal = _nominal_day(today.year, today.month, anchor.day)
    if today >= nominal:
        return nominal
    year, month = _previous_month(today.year, today.month)
    return _nominal_day(year, month, anchor.day)


def next_reset_date(anchor: date | None, today: date | None = None) -> date:
    """First day of the next billing cycle (when credits refill)."""
    anchor = anchor or timezone.localdate()
    today = today or timezone.localdate()
    start = current_period_start(anchor, today)
    year, month = _next_month(start.year, start.month)
    return _nominal_day(year, month, anchor.day)


def _creation_defaults() -> dict:
    """Defaults for a first-time credit account (activation day = today).

    The monthly allowance starts at zero: paid dispatch stays refused
    until an owner approves an explicit allowance. The configured
    default is an admin template to copy from, not an entitlement.
    """
    return {"cycle_anchor": timezone.localdate(), "monthly_credits": 0}


def get_operator_spend() -> OperatorSpend:
    """Return the singleton operator row, creating it at zero if needed."""
    try:
        row, _ = OperatorSpend.objects.get_or_create(pk=1)
    except IntegrityError:
        row = OperatorSpend.objects.get(pk=1)
    return row


def _locked_operator() -> OperatorSpend:
    try:
        return OperatorSpend.objects.select_for_update().get(pk=1)
    except OperatorSpend.DoesNotExist:
        try:
            with transaction.atomic():
                return OperatorSpend.objects.create(pk=1)
        except IntegrityError:
            return OperatorSpend.objects.select_for_update().get(pk=1)


def _snap_operator_month(locked: OperatorSpend) -> None:
    """Reset the operator's calendar-month counters when a new month began."""
    month = timezone.localdate().replace(day=1)
    if locked.period_start != month:
        locked.period_start = month
        locked.period_spent = 0


def _check_operator(locked: OperatorSpend, estimate_tokens: int) -> None:
    if locked.paused:
        raise BudgetRefused("Operator spending is paused.")
    if locked.monthly_ceiling <= 0:
        raise BudgetRefused("No operator ceiling approved.")
    committed = locked.period_spent + locked.tokens_reserved + estimate_tokens
    if committed > locked.monthly_ceiling:
        raise BudgetRefused("Operator ceiling exhausted.")


def get_allowance(organization_id: int) -> SpendAllowance:
    """Return the allowance, creating the default credit account if needed."""
    try:
        allowance, _created = SpendAllowance.objects.get_or_create(
            organization_id=organization_id, defaults=_creation_defaults()
        )
    except IntegrityError:
        allowance = SpendAllowance.objects.get(organization_id=organization_id)
    if allowance.cycle_anchor is None or allowance.period_start is None:
        today = timezone.localdate()
        allowance.cycle_anchor = allowance.cycle_anchor or today
        allowance.period_start = allowance.period_start or today
        allowance.save(update_fields=["cycle_anchor", "period_start", "updated_at"])
    return allowance


def _snap_period(locked: SpendAllowance) -> None:
    """Reset cycle counters when a new billing period has begun.

    The lifetime token_budget is intentionally untouched: a monthly
    reset must never silently replenish the lifetime safety ceiling.
    """
    period = current_period_start(locked.cycle_anchor)
    if locked.period_start != period:
        locked.period_start = period
        locked.period_spent = 0


def _sweep_expired_locked(
    locked: SpendAllowance, operator: OperatorSpend, *, now
) -> dict:
    """Settle this allowance's expired reservations under its lock.

    Held expiries become uncertain (worker loss may have spent); uncertain
    expiries book their held estimate as spend on both the allowance and
    the operator row. Counters reconcile from live rows afterwards, so
    legacy phantom counters (written before reservations had identity)
    heal without touching live holds.
    """
    swept = {"uncertain": 0, "expired": 0, "tokens_booked": 0}
    reserved_before = locked.tokens_reserved
    stale_held = list(
        SpendReservation.objects.select_for_update()
        .filter(
            allowance_id=locked.id,
            state=SpendReservation.State.HELD,
            expires_at__lte=now,
        )
        .order_by("id")
    )
    for row in stale_held:
        row.state = SpendReservation.State.UNCERTAIN
        row.settled_at = None
        row.detail = {**(row.detail or {}), "swept": "held-expired"}
        row.save(update_fields=["state", "settled_at", "detail"])
        swept["uncertain"] += 1
    horizon = now - _uncertain_grace()
    stale_uncertain = list(
        SpendReservation.objects.select_for_update()
        .filter(
            allowance_id=locked.id,
            state=SpendReservation.State.UNCERTAIN,
            expires_at__lte=horizon,
        )
        .order_by("id")
    )
    for row in stale_uncertain:
        from pipeline.jobs import record_usage

        row.state = SpendReservation.State.EXPIRED
        row.actual_tokens = row.tokens
        row.settled_at = now
        row.detail = {**(row.detail or {}), "swept": "uncertain-expired"}
        row.save(update_fields=["state", "actual_tokens", "settled_at", "detail"])
        locked.tokens_spent += row.tokens
        locked.period_spent += row.tokens
        # Booked spend must show in the credits log like any provider call.
        record_usage(
            locked.organization_id,
            None,
            "provider_uncertain",
            row.tokens,
            {
                "reservation_id": row.id,
                "purpose": row.purpose,
                "reason": (row.detail or {}).get("uncertain_reason", ""),
            },
        )
        swept["expired"] += 1
        swept["tokens_booked"] += row.tokens
    # Rows are truth: reconcile the counters from live reservations. This
    # only frees budget no live reservation holds, so it can neither
    # clear a live request nor enable extra concurrent spending.
    live = SpendReservation.objects.filter(
        allowance_id=locked.id,
        state__in=[
            SpendReservation.State.HELD,
            SpendReservation.State.UNCERTAIN,
        ],
    ).aggregate(total=Sum("tokens"), slots=Count("id"))
    locked.tokens_reserved = live["total"] or 0
    locked.in_flight = live["slots"] or 0
    locked.save(
        update_fields=[
            "period_start",
            "period_spent",
            "tokens_spent",
            "tokens_reserved",
            "in_flight",
            "updated_at",
        ]
    )
    # Mirror the allowance's net reserved movement on the operator row so
    # the operator total stays the sum of live holds even when healing
    # drifted counters in either direction.
    operator.tokens_reserved = max(
        0, operator.tokens_reserved + (locked.tokens_reserved - reserved_before)
    )
    operator.period_spent += swept["tokens_booked"]
    operator.lifetime_spent += swept["tokens_booked"]
    operator.save(
        update_fields=[
            "period_start",
            "period_spent",
            "tokens_reserved",
            "lifetime_spent",
            "updated_at",
        ]
    )
    return swept


def sweep_expired(*, organization_id: int | None = None) -> dict:
    """Sweep expired reservations; safe to run concurrently or repeatedly.

    Locks one allowance at a time (allowance id order) and settles only
    that allowance's rows, so parallel sweeps and dispatches serialize
    instead of double-settling.
    """
    query = SpendAllowance.objects.order_by("id")
    if organization_id is not None:
        query = query.filter(organization_id=organization_id)
    totals = {"uncertain": 0, "expired": 0, "tokens_booked": 0}
    for allowance_id in query.values_list("id", flat=True):
        with transaction.atomic():
            # Global lock order: operator, then allowance, then rows.
            operator = _locked_operator()
            _snap_operator_month(operator)
            locked = SpendAllowance.objects.select_for_update().get(
                id=allowance_id
            )
            _snap_period(locked)
            swept = _sweep_expired_locked(locked, operator, now=timezone.now())
            for key in totals:
                totals[key] += swept[key]
    return totals


def reserve(
    organization_id: int,
    estimate_tokens: int,
    max_in_flight: int,
    *,
    purpose: str = "",
    detail: dict | None = None,
    ttl_seconds: int | None = None,
) -> Reservation:
    """Hold budget and an in-flight slot, or refuse loudly.

    The estimate must already cover the request's permitted input,
    permitted output, and protocol overhead (see estimate_request_tokens).
    Sweeps this allowance's expired reservations first so dead holders
    convert to uncertain instead of blocking forever.
    """
    if estimate_tokens <= 0:
        raise BudgetRefused("Non-positive token estimate.")
    if max_in_flight <= 0:
        raise BudgetRefused("No in-flight slots configured.")
    allowance = get_allowance(organization_id)
    with transaction.atomic():
        # Global lock order: operator, then allowance, then rows.
        operator = _locked_operator()
        _snap_operator_month(operator)
        locked = SpendAllowance.objects.select_for_update().get(pk=allowance.pk)
        if locked.paused:
            raise BudgetRefused("Spending is paused.")
        _snap_period(locked)
        _sweep_expired_locked(locked, operator, now=timezone.now())
        # Checks run on post-sweep counters so healed drift cannot refuse
        # a dispatch the live holds would allow.
        _check_operator(operator, estimate_tokens)
        if locked.monthly_credits <= 0:
            raise BudgetRefused("No monthly credits approved.")
        monthly = locked.period_spent + locked.tokens_reserved + estimate_tokens
        if monthly > locked.monthly_credits:
            raise BudgetRefused("Monthly credits exhausted.")
        if locked.token_budget > 0:
            committed = locked.tokens_spent + locked.tokens_reserved + estimate_tokens
            if committed > locked.token_budget:
                raise BudgetRefused("Insufficient budget headroom.")
        if locked.in_flight >= max_in_flight:
            raise BudgetRefused("No free in-flight slot.")
        ttl = timedelta(
            seconds=(
                ttl_seconds if ttl_seconds is not None else _hold_ttl().total_seconds()
            )
        )
        row = SpendReservation.objects.create(
            allowance_id=locked.id,
            organization_id=organization_id,
            tokens=estimate_tokens,
            state=SpendReservation.State.HELD,
            purpose=(purpose or "")[:64],
            detail=dict(detail or {}),
            expires_at=timezone.now() + ttl,
        )
        locked.tokens_reserved += estimate_tokens
        locked.in_flight += 1
        locked.save(
            update_fields=[
                "period_start",
                "period_spent",
                "tokens_reserved",
                "in_flight",
                "updated_at",
            ]
        )
        operator.tokens_reserved += estimate_tokens
        operator.save(update_fields=["tokens_reserved", "updated_at"])
        return Reservation(
            id=row.id, allowance_id=locked.pk, estimate_tokens=estimate_tokens
        )


def reconcile(reservation: Reservation, actual_tokens: int) -> SpendAllowance:
    """Convert a reservation into observed spend (idempotent).

    Late usage for an uncertain reservation books here too. Settling an
    already-terminal reservation is a no-op returning the allowance, so
    retried callbacks cannot double-spend. Works while paused: requests
    already accepted must still account honestly.
    """
    actual = max(0, int(actual_tokens))
    with transaction.atomic():
        # Global lock order: operator, then allowance, then rows.
        operator = _locked_operator()
        _snap_operator_month(operator)
        locked = SpendAllowance.objects.select_for_update().get(
            pk=reservation.allowance_id
        )
        _snap_period(locked)
        try:
            row = SpendReservation.objects.select_for_update().get(
                id=reservation.id, allowance_id=locked.id
            )
        except SpendReservation.DoesNotExist:
            # Pre-identity reservation handle: nothing to settle twice.
            return locked
        if row.state not in (
            SpendReservation.State.HELD,
            SpendReservation.State.UNCERTAIN,
        ):
            return locked
        row.state = SpendReservation.State.RECONCILED
        row.actual_tokens = actual
        row.settled_at = timezone.now()
        row.save(update_fields=["state", "actual_tokens", "settled_at"])
        locked.tokens_reserved = max(0, locked.tokens_reserved - row.tokens)
        locked.tokens_spent += actual
        locked.period_spent += actual
        locked.in_flight = max(0, locked.in_flight - 1)
        locked.save(
            update_fields=[
                "period_start",
                "period_spent",
                "tokens_reserved",
                "tokens_spent",
                "in_flight",
                "updated_at",
            ]
        )
        operator.tokens_reserved = max(0, operator.tokens_reserved - row.tokens)
        operator.period_spent += actual
        operator.lifetime_spent += actual
        operator.save(
            update_fields=[
                "period_start",
                "period_spent",
                "tokens_reserved",
                "lifetime_spent",
                "updated_at",
            ]
        )
        return locked


def release(reservation: Reservation) -> SpendAllowance:
    """Drop a reservation that failed before any possible spend.

    Valid only for failures where the provider cannot have processed the
    request (auth/validation refusal, never dispatched). Timeouts and
    dropped connections must use mark_uncertain instead. Idempotent.
    """
    with transaction.atomic():
        # Global lock order: operator, then allowance, then rows.
        operator = _locked_operator()
        _snap_operator_month(operator)
        locked = SpendAllowance.objects.select_for_update().get(
            pk=reservation.allowance_id
        )
        try:
            row = SpendReservation.objects.select_for_update().get(
                id=reservation.id, allowance_id=locked.id
            )
        except SpendReservation.DoesNotExist:
            return locked
        if row.state != SpendReservation.State.HELD:
            return locked
        row.state = SpendReservation.State.RELEASED
        row.actual_tokens = 0
        row.settled_at = timezone.now()
        row.save(update_fields=["state", "actual_tokens", "settled_at"])
        locked.tokens_reserved = max(0, locked.tokens_reserved - row.tokens)
        locked.in_flight = max(0, locked.in_flight - 1)
        locked.save(update_fields=["tokens_reserved", "in_flight", "updated_at"])
        operator.tokens_reserved = max(0, operator.tokens_reserved - row.tokens)
        operator.save(update_fields=["tokens_reserved", "updated_at"])
        return locked


def mark_uncertain(reservation: Reservation, reason: str) -> SpendAllowance:
    """Hold a reservation whose spend is unknown (idempotent).

    Timeouts, dropped connections, and interrupted workers land here:
    counters stay committed until usage arrives (reconcile) or the grace
    horizon books the held estimate (sweep). Never frees budget.
    """
    with transaction.atomic():
        locked = SpendAllowance.objects.select_for_update().get(
            pk=reservation.allowance_id
        )
        try:
            row = SpendReservation.objects.select_for_update().get(
                id=reservation.id, allowance_id=locked.id
            )
        except SpendReservation.DoesNotExist:
            return locked
        if row.state != SpendReservation.State.HELD:
            return locked
        row.state = SpendReservation.State.UNCERTAIN
        row.detail = {**(row.detail or {}), "uncertain_reason": reason[:200]}
        row.save(update_fields=["state", "detail"])
        return locked
