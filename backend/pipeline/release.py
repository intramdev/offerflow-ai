"""Shared release eligibility for every customer-facing export.

Every file download, message render, and future integration (including the
planned Telegram bot) funnels through these checks. An authorized member is
not the same as an approved artifact: approval, completed reviews, passing
validation, and freshness of the bound data and generation settings are all
reverified here, at release time, on the exact bytes about to be shared.

Internal previews (run quote JSON, quote drafts) stay readable to members
without release approval, but they are labeled previews and never produce
shareable files; anything that leaves the app as a file or message passes
through this module first.
"""

from __future__ import annotations

import hashlib
import json

from django.core.exceptions import ValidationError

from pipeline.export import normalized_digest
from pipeline.models import (
    CustomerQuote,
    ImportRun,
    OfferStatus,
    QuoteStatus,
    ReviewStatus,
    RunStatus,
)
from pipeline.validate import evaluate_validations


class ReleaseRefused(ValidationError):
    """Release eligibility failed; the caller reports it, never bypasses it."""


def active_rules_digest(organization) -> str:
    """Fingerprint the pricing rules a run quote would apply right now."""
    from pipeline.models import PricingRule

    # By id, not by relation: the 0028 backfill passes historical
    # instances, which cannot query through real related managers.
    rules = PricingRule.objects.filter(
        organization_id=organization.pk, active=True
    ).order_by("id")
    payload = [
        {
            "name": rule.name,
            "matcher": rule.matcher,
            "supplier_strategy": rule.supplier_strategy,
            "preferred_supplier": rule.preferred_supplier,
            "ops": rule.ops,
            "priority": rule.priority,
        }
        for rule in rules
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_run_release(run: ImportRun, *, for_quote: bool = False) -> dict:
    """Refuse unless the run's approval still binds its current content.

    Rejects unapproved runs, stale approvals (offers, source file, or —
    for priced quotes — pricing rules changed since approval), failing
    validation gates, and unanswered reviews. Returns the releasable
    offers plus the digests the release was checked against.
    """
    if run.status not in (RunStatus.APPROVED, RunStatus.COMPLETED):
        raise ReleaseRefused("Only approved runs can be exported.")
    if normalized_digest(run) != (run.approved_normalized_digest or ""):
        raise ReleaseRefused("Approval is stale; re-approval is required.")
    if run.source_file.sha256 != (run.approved_source_sha or ""):
        raise ReleaseRefused("Source changed since approval; re-approval is required.")
    if for_quote:
        if active_rules_digest(run.organization) != (run.approved_rules_digest or ""):
            raise ReleaseRefused(
                "Pricing rules changed since approval; re-approval is required."
            )
    failing = [item.name for item in evaluate_validations(run) if not item.passed]
    if failing:
        raise ReleaseRefused(
            "Run has failing validation checks: " + ", ".join(sorted(failing)) + "."
        )
    if run.reviews.filter(status=ReviewStatus.OPEN).exists():
        raise ReleaseRefused("Run has unanswered review questions.")
    return {
        "offers": list(run.offers.filter(status=OfferStatus.OK).order_by("id")),
        "normalized_digest": run.approved_normalized_digest,
        "source_sha": run.approved_source_sha,
    }


def quote_presentation_digest(lines: list, edits: dict | None) -> str:
    """Fingerprint exactly what customers would see (labels/hidden applied)."""
    from pipeline.customer_quotes import export_lines

    shown = export_lines(lines=lines or [], edits=edits or {})
    canonical = json.dumps(shown, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_digests(organization, run_ids: list) -> dict:
    """Freeze each source run's releasable set for staleness detection."""
    from pipeline.models import ImportRun as Run

    digests = {}
    for run_id in run_ids or []:
        try:
            run = Run.objects.get(pk=run_id, organization_id=organization.pk)
        except Run.DoesNotExist:
            continue
        digests[str(run_id)] = normalized_digest(run)
    return digests


def bind_customer_quote(quote: CustomerQuote) -> CustomerQuote:
    """Bind a confirmed presentation to its exact lines and sources."""
    quote.approved_lines_digest = quote_presentation_digest(
        quote.lines, quote.line_edits
    )
    quote.source_digests = _source_digests(
        quote.organization, quote.source_run_ids
    )
    return quote


def check_customer_quote_release(quote: CustomerQuote) -> dict:
    """Refuse unless the quote's confirmation still binds its content.

    Rejects drafts, empty quotes, unpriced lines, presentations edited
    outside the audited line-edit flow, source runs that were remapped,
    reapproved with different content, deleted, or moved across tenants,
    and quotes that predate release binding. Returns a release manifest
    naming the exact revision shared so files, messages, and previews
    can be compared.
    """
    from pipeline.customer_quotes import effective_template

    if quote.status != QuoteStatus.READY:
        raise ReleaseRefused("Confirm the product groups before releasing.")
    if not quote.lines:
        raise ReleaseRefused("Quote has no lines to release.")
    if not quote.approved_lines_digest or quote_presentation_digest(
        quote.lines, quote.line_edits
    ) != quote.approved_lines_digest:
        raise ReleaseRefused(
            "Quote changed outside the audited edit flow; re-confirm it."
        )
    for position, line in enumerate(quote.lines):
        if not isinstance(line, dict) or not line.get("unit_price") or not line.get(
            "currency"
        ):
            raise ReleaseRefused(f"Quote line {position + 1} has no price.")
    frozen = quote.source_digests or {}
    for run_id in quote.source_run_ids or []:
        key = str(run_id)
        try:
            run = ImportRun.objects.get(pk=run_id)
        except ImportRun.DoesNotExist:
            raise ReleaseRefused(
                "A source import was deleted; start a new quote."
            ) from None
        if run.organization_id != quote.organization_id:
            raise ReleaseRefused("Quote sources span organizations; refused.")
        if run.status not in (RunStatus.APPROVED, RunStatus.COMPLETED):
            raise ReleaseRefused(
                "A source import is no longer approved; start a new quote."
            )
        if frozen.get(key) != normalized_digest(run):
            raise ReleaseRefused(
                "A source import changed since confirmation; start a new quote."
            )
    template = effective_template(quote.organization)
    template_digest = hashlib.sha256(
        json.dumps(template, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "quote_id": quote.id,
        "revision": quote.revision,
        "lines_digest": quote.approved_lines_digest,
        "template_digest": template_digest,
        "sources": sorted(str(run_id) for run_id in quote.source_run_ids or []),
    }
