"""Token budgets with atomic reservation.

Every paid dispatch reserves a conservative token bound before touching the
network and reconciles observed usage afterwards; failures release without
spending. Reservation and the in-flight check happen under one row lock so
concurrent workers cannot overspend or double-dispatch past the limit.

Credits are tokens 1:1, granted monthly. Each company's billing cycle is
anchored at its activation day and resets lazily (no scheduler): the first
reservation or dashboard read in a new cycle snaps the period counters.
tokens_spent is the grand total and is never reset.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from pipeline.models import SpendAllowance


class BudgetRefused(Exception):
    """The budget declined a paid dispatch. Never retried implicitly."""


@dataclass(frozen=True)
class Reservation:
    allowance_id: int
    estimate_tokens: int


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
    """Defaults for a first-time credit account (activation day = today)."""
    from organizations.models import AIProviderConfig, DEFAULT_MONTHLY_CREDITS

    config = AIProviderConfig.get_solo()
    return {
        "cycle_anchor": timezone.localdate(),
        "monthly_credits": (
            config.default_monthly_credits if config else DEFAULT_MONTHLY_CREDITS
        ),
    }


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
    """Reset cycle counters when a new billing period has begun."""
    period = current_period_start(locked.cycle_anchor)
    if locked.period_start != period:
        locked.period_start = period
        locked.period_spent = 0


def reserve(
    organization_id: int, estimate_tokens: int, max_in_flight: int
) -> Reservation:
    """Hold budget and an in-flight slot, or refuse loudly."""
    if estimate_tokens <= 0:
        raise BudgetRefused("Non-positive token estimate.")
    if max_in_flight <= 0:
        raise BudgetRefused("No in-flight slots configured.")
    allowance = get_allowance(organization_id)
    with transaction.atomic():
        locked = SpendAllowance.objects.select_for_update().get(pk=allowance.pk)
        if locked.paused:
            raise BudgetRefused("Spending is paused.")
        _snap_period(locked)
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
        return Reservation(allowance_id=locked.pk, estimate_tokens=estimate_tokens)


def reconcile(reservation: Reservation, actual_tokens: int) -> SpendAllowance:
    """Convert a reservation into observed spend (period + grand total)."""
    actual = max(0, int(actual_tokens))
    with transaction.atomic():
        locked = SpendAllowance.objects.select_for_update().get(
            pk=reservation.allowance_id
        )
        _snap_period(locked)
        locked.tokens_reserved = max(0, locked.tokens_reserved - reservation.estimate_tokens)
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
        return locked


def release(reservation: Reservation) -> SpendAllowance:
    """Drop a reservation after a failure without recording spend."""
    with transaction.atomic():
        locked = SpendAllowance.objects.select_for_update().get(
            pk=reservation.allowance_id
        )
        locked.tokens_reserved = max(0, locked.tokens_reserved - reservation.estimate_tokens)
        locked.in_flight = max(0, locked.in_flight - 1)
        locked.save(update_fields=["tokens_reserved", "in_flight", "updated_at"])
        return locked
