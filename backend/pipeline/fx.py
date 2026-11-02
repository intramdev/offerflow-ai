"""Date-based exchange rates with fail-closed provider selection.

Simulated mode is the default: the fixture provider is a pure function of
(date, destination) and performs zero network I/O. Its rates are synthetic
demo data, always labeled as such, and exist so the destination-currency
flow is fully usable without any external account. A real rates provider
plugs in behind OFFERFLOW_FX_PROVIDER with the same explicit-gating
discipline as the interpretation providers: anything unconfigured is
refused, never silently downgraded to fixtures.
"""

from __future__ import annotations

import hashlib
import os
import random
from datetime import date

from django.core.exceptions import ValidationError
from django.utils import timezone

# Synthetic anchors (units per USD) for the fixture provider. These are
# clearly demo data, not market rates; the date-seeded jitter below is
# what makes each day's table differ. The original six entries stay
# first so long-standing demo rates keep their sequence; the rest are
# alphabetical.
BASE_UNITS_PER_USD = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "CZK": 23.1,
    "CHF": 0.88,
    "PLN": 4.02,
    "AUD": 1.52,
    "BGN": 1.80,
    "BRL": 5.42,
    "CAD": 1.39,
    "CNY": 7.24,
    "DKK": 6.87,
    "HKD": 7.79,
    "HRK": 6.93,
    "HUF": 368.0,
    "IDR": 15850.0,
    "ILS": 3.72,
    "INR": 84.1,
    "ISK": 138.5,
    "JPY": 151.5,
    "KRW": 1378.0,
    "MXN": 19.8,
    "MYR": 4.46,
    "NOK": 10.9,
    "NZD": 1.66,
    "PHP": 58.3,
    "RON": 4.57,
    "RUB": 97.5,
    "SEK": 10.5,
    "SGD": 1.34,
    "THB": 34.2,
    "TRY": 34.5,
    "XDR": 0.76,
    "ZAR": 18.1,
}

# The currencies OfferFlow supports, alphabetical. This is the single
# source of truth: settings validation and the currency dropdowns
# (served via the organization settings response) both read it, and
# the fixture provider above rates every entry.
SUPPORTED_CURRENCIES = tuple(sorted(BASE_UNITS_PER_USD))

FIXTURE_SOURCE = "fixture (synthetic demo rates)"


class FxNotConfigured(Exception):
    """Raised when the requested FX provider path cannot be fully approved."""


class FxRateMissing(Exception):
    """Raised when no recorded rate covers a conversion (never guessed)."""

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


class FixtureFxProvider:
    """Deterministic synthetic rates. Pure function; no network I/O."""

    name = "fixture"
    is_simulated = True

    def rates_for(self, day: date, destination: str) -> dict:
        """Return the rate table for one day and destination.

        Rates map each source code to a decimal string: one unit of the
        source buys that many units of the destination.
        """
        if not isinstance(day, date):
            raise ValidationError("Currency date must be a calendar date.")
        dest = (destination or "").strip().upper()
        if len(dest) != 3 or not dest.isalpha():
            raise ValidationError("Destination must be a three-letter code.")
        seed = int(
            hashlib.sha256(f"{day.isoformat()}|{dest}".encode()).hexdigest()[:16],
            16,
        )
        jitter = random.Random(seed)
        units = {
            code: base * (1 + (jitter.random() - 0.5) * 0.01)
            for code, base in BASE_UNITS_PER_USD.items()
        }
        dest_units = units.get(dest)
        if dest_units is None:
            dest_units = 1.0 * (1 + (jitter.random() - 0.5) * 0.01)
        rates = {
            code: f"{dest_units / value:.6f}" for code, value in units.items()
        }
        rates[dest] = "1.000000"
        return {"rates": rates, "source": FIXTURE_SOURCE, "simulated": True}


def get_fx_provider():
    """Return the configured FX provider, refusing anything unapproved."""
    requested = (os.environ.get("OFFERFLOW_FX_PROVIDER", "fixture") or "").strip().lower()
    if requested == "fixture":
        return FixtureFxProvider()
    raise FxNotConfigured(
        f"Unknown FX provider {requested!r}; refusing. "
        'Set OFFERFLOW_FX_PROVIDER="fixture" for simulated rates.'
    )


def snapshot_upload_rates(*, source_file, organization) -> None:
    """Stamp a new file with its upload-date rate table.

    The currency date always defaults to the upload day. Rates are only
    recorded when the organization has a destination currency; without
    one there is nothing to convert to, so the table stays empty.
    """
    from pipeline.jobs import record_audit

    day = timezone.localdate()
    source_file.currency_date = day
    destination = (organization.destination_currency or "").strip().upper()
    if not destination:
        source_file.save(update_fields=["currency_date"])
        return
    try:
        provider = get_fx_provider()
    except FxNotConfigured as exc:
        raise ValidationError(str(exc)) from exc
    snapshot = provider.rates_for(day, destination)
    source_file.fx_destination = destination
    source_file.fx_rates = snapshot["rates"]
    source_file.fx_source = snapshot["source"]
    source_file.fx_simulated = snapshot["simulated"]
    source_file.fx_fetched_at = timezone.now()
    source_file.save(
        update_fields=[
            "currency_date",
            "fx_destination",
            "fx_rates",
            "fx_source",
            "fx_simulated",
            "fx_fetched_at",
        ]
    )
    record_audit(
        organization.id,
        None,
        "fx_rates_recorded",
        "file",
        source_file.id,
        {"currency_date": day.isoformat(), "destination": destination},
    )


def refresh_file_rates(*, source_file, user, currency_date: date):
    """Re-fetch a file's rate table for a user-picked calendar date."""
    from organizations.services import require_organization_role

    from pipeline.jobs import record_audit

    require_organization_role(user, source_file.organization_id)
    organization = source_file.organization
    destination = (organization.destination_currency or "").strip().upper()
    if not destination:
        raise ValidationError(
            "Set a destination currency in Settings before fetching rates."
        )
    if currency_date > timezone.localdate():
        raise ValidationError("Currency date cannot be in the future.")
    try:
        provider = get_fx_provider()
    except FxNotConfigured as exc:
        raise ValidationError(str(exc)) from exc
    snapshot = provider.rates_for(currency_date, destination)
    source_file.currency_date = currency_date
    source_file.fx_destination = destination
    source_file.fx_rates = snapshot["rates"]
    source_file.fx_source = snapshot["source"]
    source_file.fx_simulated = snapshot["simulated"]
    source_file.fx_fetched_at = timezone.now()
    source_file.save(
        update_fields=[
            "currency_date",
            "fx_destination",
            "fx_rates",
            "fx_source",
            "fx_simulated",
            "fx_fetched_at",
        ]
    )
    record_audit(
        organization.id,
        user,
        "fx_rates_updated",
        "file",
        source_file.id,
        {"currency_date": currency_date.isoformat(), "destination": destination},
    )
    return source_file
