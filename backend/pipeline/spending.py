"""Token budgets with atomic reservation.

Every paid dispatch reserves a conservative token bound before touching the
network and reconciles observed usage afterwards; failures release without
spending. Reservation and the in-flight check happen under one row lock so
concurrent workers cannot overspend or double-dispatch past the limit.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

from pipeline.models import SpendAllowance


class BudgetRefused(Exception):
    """The budget declined a paid dispatch. Never retried implicitly."""


@dataclass(frozen=True)
class Reservation:
    allowance_id: int
    estimate_tokens: int


def get_allowance(organization_id: int) -> SpendAllowance:
    """Return the allowance, creating the zero-budget default if needed."""
    allowance, _created = SpendAllowance.objects.get_or_create(
        organization_id=organization_id
    )
    return allowance


def reserve(
    organization_id: int, estimate_tokens: int, max_in_flight: int
) -> Reservation:
    """Hold budget and an in-flight slot, or refuse loudly."""
    if estimate_tokens <= 0:
        raise BudgetRefused("Non-positive token estimate.")
    if max_in_flight <= 0:
        raise BudgetRefused("No in-flight slots configured.")
    try:
        allowance, _created = SpendAllowance.objects.get_or_create(
            organization_id=organization_id
        )
    except IntegrityError:
        allowance = SpendAllowance.objects.get(organization_id=organization_id)
    with transaction.atomic():
        locked = SpendAllowance.objects.select_for_update().get(pk=allowance.pk)
        if locked.paused:
            raise BudgetRefused("Spending is paused.")
        if locked.token_budget <= 0:
            raise BudgetRefused("No token budget approved.")
        committed = locked.tokens_spent + locked.tokens_reserved + estimate_tokens
        if committed > locked.token_budget:
            raise BudgetRefused("Insufficient budget headroom.")
        if locked.in_flight >= max_in_flight:
            raise BudgetRefused("No free in-flight slot.")
        locked.tokens_reserved += estimate_tokens
        locked.in_flight += 1
        locked.save(update_fields=["tokens_reserved", "in_flight", "updated_at"])
        return Reservation(allowance_id=locked.pk, estimate_tokens=estimate_tokens)


def reconcile(reservation: Reservation, actual_tokens: int) -> SpendAllowance:
    """Convert a reservation into observed spend."""
    actual = max(0, int(actual_tokens))
    with transaction.atomic():
        locked = SpendAllowance.objects.select_for_update().get(
            pk=reservation.allowance_id
        )
        locked.tokens_reserved = max(0, locked.tokens_reserved - reservation.estimate_tokens)
        locked.tokens_spent += actual
        locked.in_flight = max(0, locked.in_flight - 1)
        locked.save(
            update_fields=["tokens_reserved", "tokens_spent", "in_flight", "updated_at"]
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
