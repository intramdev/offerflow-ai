"""Pipeline records: files, runs, rows, offers, reviews, artifacts, audit.

Every tenant-owned record carries a direct organization owner so row
policies stay simple equality checks. Rule set versions are the one global
reference table: every run binds to an exact version at creation.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class RunStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    PARSING = "parsing", "Parsing"
    INTERPRETATION = "interpretation", "Interpretation"
    VALIDATION = "validation", "Validation"
    NEEDS_INPUT = "needs_input", "Needs input"
    READY_FOR_REVIEW = "ready_for_review", "Ready for review"
    APPROVED = "approved", "Approved"
    EXPORTING = "exporting", "Exporting"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)

ALLOWED_TRANSITIONS = {
    RunStatus.UPLOADED: {RunStatus.PARSING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.PARSING: {
        RunStatus.INTERPRETATION,
        RunStatus.NEEDS_INPUT,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.INTERPRETATION: {RunStatus.VALIDATION, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.VALIDATION: {
        RunStatus.NEEDS_INPUT,
        RunStatus.READY_FOR_REVIEW,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    # UPLOADED from NEEDS_INPUT, READY_FOR_REVIEW, APPROVED, or COMPLETED
    # restarts processing after mapping answers or a remap replaces the
    # column mapping. Remapping an approved or completed run discards its
    # approval and export artifacts; the audit trail keeps the history.
    RunStatus.NEEDS_INPUT: {
        RunStatus.VALIDATION,
        RunStatus.UPLOADED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.READY_FOR_REVIEW: {
        RunStatus.APPROVED,
        RunStatus.UPLOADED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.APPROVED: {
        RunStatus.EXPORTING,
        RunStatus.UPLOADED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.EXPORTING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COMPLETED: {RunStatus.EXPORTING, RunStatus.UPLOADED},
    RunStatus.FAILED: {RunStatus.UPLOADED},
    RunStatus.CANCELLED: set(),
}


def transition_run(run: ImportRun, new_status: str) -> ImportRun:
    """Move a run to a new status, refusing illegal transitions."""
    allowed = ALLOWED_TRANSITIONS.get(run.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Cannot move run from {run.status} to {new_status}.")
    run.status = new_status
    run.save(update_fields=["status", "updated_at"])
    return run


class OfferStatus(models.TextChoices):
    OK = "ok", "OK"
    EXCLUDED = "excluded", "Excluded"
    NEEDS_INPUT = "needs_input", "Needs input"


class LockState(models.TextChoices):
    LOCKED = "locked", "Locked"
    UNLOCKED = "unlocked", "Unlocked"
    UNKNOWN = "unknown", "Unknown"


class ReviewKind(models.TextChoices):
    CHOICE = "choice", "Choice"
    VALUE = "value", "Value"


class ReviewStatus(models.TextChoices):
    OPEN = "open", "Open"
    ANSWERED = "answered", "Answered"


class ArtifactFormat(models.TextChoices):
    XLSX = "xlsx", "XLSX"
    CSV = "csv", "CSV"


class RuleSetVersion(models.Model):
    """A frozen, globally readable set of normalization rules."""

    version = models.CharField(max_length=32, unique=True)
    spec = models.JSONField(default=dict)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.version


class SourceFile(models.Model):
    """An uploaded supplier file, stored under organization scope."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="files"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_files",
    )
    original_name = models.CharField(max_length=255)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=512)
    # Exchange-rate snapshot for this file: the calendar date the rates
    # were fetched for (upload day by default), the destination they
    # convert to, and the recorded FROM -> rate table. Quotes convert
    # explicitly from this table; nothing here is ever re-fetched
    # implicitly, so a file's numbers stay reproducible.
    currency_date = models.DateField(null=True, blank=True)
    category = models.ForeignKey(
        "pipeline.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="files",
        help_text="Optional: every product in this file belongs here.",
    )
    fx_destination = models.CharField(max_length=3, blank=True, default="")
    fx_rates = models.JSONField(default=dict)
    fx_source = models.CharField(max_length=64, blank=True, default="")
    fx_simulated = models.BooleanField(default=True)
    fx_fetched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "created_at"])]


class ImportRun(models.Model):
    """One processing pass over one uploaded file."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="runs"
    )
    source_file = models.OneToOneField(
        SourceFile, on_delete=models.CASCADE, related_name="run"
    )
    rule_version = models.ForeignKey(
        RuleSetVersion, on_delete=models.PROTECT, related_name="runs"
    )
    status = models.CharField(
        max_length=16, choices=RunStatus.choices, default=RunStatus.UPLOADED
    )
    provider_name = models.CharField(max_length=32, default="fixture")
    simulated = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_runs",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_source_sha = models.CharField(max_length=64, blank=True)
    approved_normalized_digest = models.CharField(max_length=64, blank=True)
    approved_rules_digest = models.CharField(max_length=64, blank=True)
    # Column-mapping report: {"headers": [...], "detected": {canonical: pos},
    # "confirmed": {canonical: pos}}. The effective mapping is detected
    # updated with confirmed answers; confirmed positions are the parse
    # overrides on reprocessing.
    mapping = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status"])]


class SourceRow(models.Model):
    """Raw parsed values of one spreadsheet row with cell provenance."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="source_rows"
    )
    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="source_rows")
    index = models.PositiveIntegerField()
    sheet = models.CharField(max_length=64)
    data = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)

    class Meta:
        ordering = ["index"]
        constraints = [
            models.UniqueConstraint(fields=["run", "index"], name="unique_run_row")
        ]


class NormalizedOffer(models.Model):
    """One interpreted offer. Raw terms stay on the source row; nothing here
    is ever inferred — gaps become review requests or exclusions."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="offers"
    )
    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="offers")
    source_row = models.OneToOneField(
        SourceRow, on_delete=models.CASCADE, related_name="offer"
    )
    supplier = models.CharField(max_length=255, blank=True)
    brand = models.CharField(max_length=255, blank=True)
    model_name = models.CharField(max_length=255, blank=True)
    capacity_gb = models.PositiveIntegerField(null=True, blank=True)
    color = models.CharField(max_length=64, blank=True)
    grade = models.CharField(max_length=64, blank=True)
    grade_raw = models.CharField(max_length=64, blank=True)
    lock_state = models.CharField(
        max_length=16, choices=LockState.choices, default=LockState.UNKNOWN
    )
    quantity = models.PositiveIntegerField(null=True, blank=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    conditions = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=OfferStatus.choices, default=OfferStatus.NEEDS_INPUT
    )
    exclusion_reason = models.CharField(max_length=255, blank=True)
    provenance = models.JSONField(default=dict)
    # Model-returned cells on paid runs (evidence for re-evaluation);
    # null on the fixture path, which normalizes source cells directly.
    interpreted = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["id"]


class ValidationResult(models.Model):
    """One mandatory check outcome bound to its exact run."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="validation_results",
    )
    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="validations")
    name = models.CharField(max_length=64)
    passed = models.BooleanField()
    message = models.TextField(blank=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["run", "name"], name="unique_run_check")
        ]


class ReviewRequest(models.Model):
    """One open question, per offer or file-wide when the offer is null."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="review_requests",
    )
    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="reviews")
    offer = models.ForeignKey(
        NormalizedOffer,
        on_delete=models.CASCADE,
        related_name="review_requests",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=16, choices=ReviewKind.choices)
    field = models.CharField(max_length=64, blank=True)
    question = models.TextField()
    evidence = models.JSONField(default=dict)
    choices = models.JSONField(default=list)
    status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.OPEN
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class ReviewDecision(models.Model):
    """The recorded answer to one review request."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="review_decisions",
    )
    request = models.OneToOneField(
        ReviewRequest, on_delete=models.CASCADE, related_name="decision"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_decisions",
    )
    choice = models.CharField(max_length=255, blank=True)
    value = models.JSONField(null=True, blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)


class ExportArtifact(models.Model):
    """Immutable validated export bytes. Downloads serve these exact bytes."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="artifacts")
    format = models.CharField(max_length=8, choices=ArtifactFormat.choices)
    storage_key = models.CharField(max_length=512)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    source_sha = models.CharField(max_length=64)
    rule_version = models.CharField(max_length=32)
    normalized_digest = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_artifacts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class UsageEvent(models.Model):
    """Metered per-organization activity. Provider-call accounting joins in
    Package E; run and export completions are recorded from the start."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="usage_events"
    )
    run = models.ForeignKey(
        ImportRun, on_delete=models.CASCADE, null=True, blank=True, related_name="usage_events"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_events",
    )
    kind = models.CharField(max_length=64)
    quantity = models.PositiveIntegerField(default=1)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["user", "created_at"]),
        ]


class AuditEvent(models.Model):
    """Security-relevant actions with tenant and actor correlation."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="audit_events"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "created_at"])]


class ColumnMappingMemory(models.Model):
    """A confirmed header-signature to column-mapping reuse record.

    When an upload's ordered headers fingerprint matches a remembered
    signature, the stored mapping is proposed instead of asking again.
    Profiles are scoped by organization, signature, and supplier, so one
    supplier's interpretation never overwrites or leaks into another's;
    every proposal names its source profile.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="mapping_memories",
    )
    signature = models.CharField(max_length=64)
    headers = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    constants = models.JSONField(default=dict)
    ignored = models.JSONField(default=list)
    supplier_hint = models.CharField(max_length=255, blank=True)
    use_count = models.PositiveIntegerField(default=1)
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "signature", "supplier_hint"],
                name="unique_org_mapping_memory",
            )
        ]
        ordering = ["-last_used_at"]


class Category(models.Model):
    """One company-owned product category (phones, accessories, ...).

    Categories are optional: companies without any quote a single
    uncategorized list. Names are unique per company up to case.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="categories",
    )
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Categories"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_org_category",
            )
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization_id})"


class Supplier(models.Model):
    """One supplier a company buys from, auto-created on import.

    The first import naming a supplier creates its record; later
    imports reuse it. Names are unique per company up to case.
    favorite_rank orders the favorites shelf (1 is the favorite);
    null means not a favorite. Ratings score five relationship
    aspects from 1 to 5 stars; customs fields record whether a
    shipment needs clearing and at what percentage fee.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="suppliers",
    )
    name = models.CharField(max_length=255)
    favorite_rank = models.PositiveIntegerField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True)
    customs_required = models.BooleanField(default=False)
    customs_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    rating_shipping = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_quality = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_payment = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_quantity = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_rma = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_org_supplier",
            ),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization_id})"


class SupplierNote(models.Model):
    """One timestamped note on a supplier relationship.

    Notes are append-only history: relationship quality degrades
    (or improves) over time, and the trail shows when.
    """

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    author = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="supplier_notes",
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"note {self.id} on supplier {self.supplier_id}"


class ProductAlias(models.Model):
    """One learned product-name spelling, scoped to a single company.

    Each supplier spells the same product the same way, so a confirmed
    (supplier, spelling, brand, capacity, grade) tuple re-matches
    instantly on later quotes. One company's learning never leaks into
    another's: the organization is part of the key. Offers without a
    capacity are never remembered, since "same product" cannot be
    verified for them.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="product_aliases",
    )
    supplier_norm = models.CharField(max_length=255)
    alias_norm = models.CharField(max_length=255)
    brand_norm = models.CharField(max_length=255)
    capacity_gb = models.PositiveIntegerField()
    grade_norm = models.CharField(max_length=32, default="")
    canonical_model = models.CharField(max_length=255)
    category = models.ForeignKey(
        "pipeline.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="aliases",
    )
    use_count = models.PositiveIntegerField(default=1)
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "organization",
                    "supplier_norm",
                    "alias_norm",
                    "brand_norm",
                    "capacity_gb",
                    "grade_norm",
                ],
                name="unique_org_product_alias",
            )
        ]
        ordering = ["-last_used_at"]

    def __str__(self) -> str:
        return f"{self.alias_norm} -> {self.canonical_model} ({self.organization_id})"


class SupplierStrategy(models.TextChoices):
    LOWEST = "lowest", "Lowest price"
    HIGHEST = "highest", "Highest price"
    PREFERRED = "preferred", "Preferred supplier"


class QuoteStatus(models.TextChoices):
    NEEDS_REVIEW = "needs_review", "Needs review"
    READY = "ready", "Ready"


class CustomerQuote(models.Model):
    """One generated customer quote over matched cross-supplier offers.

    source_run_ids freezes which supplier runs fed the quote; groups
    snapshots the confirmed product groups; lines/notes/message are the
    rendered outputs. line_edits holds per-line presentation overrides
    (customer-facing label, hidden flag) keyed by frozen line index;
    files and messages apply them live, while prices and sources stay
    frozen. revision counts confirmed presentations: every line edit
    bumps it and rebinds approved_lines_digest, so a release always
    names the exact revision it shares. source_digests freezes each
    source run's releasable set at confirm time so remapped suppliers
    invalidate the quote instead of leaking stale prices.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="customer_quotes",
    )
    status = models.CharField(
        max_length=16, choices=QuoteStatus.choices, default=QuoteStatus.NEEDS_REVIEW
    )
    source_run_ids = models.JSONField(default=list)
    groups = models.JSONField(default=list)
    lines = models.JSONField(default=list)
    notes = models.JSONField(default=list)
    message = models.TextField(blank=True)
    provider_name = models.CharField(max_length=32, default="fixture")
    simulated = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_quotes",
    )
    line_edits = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    approved_lines_digest = models.CharField(max_length=64, blank=True, default="")
    source_digests = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Quote {self.id} ({self.organization_id})"


class PricingRule(models.Model):
    """A customer-quote rule: pick a supplier per product group, then reprice.

    matcher narrows the rule to products (exact, case-insensitive values
    for brand/model/grade); empty matches everything (global rule).
    Exactly one rule wins per group: most matcher keys first, then lowest
    priority number. ops run in listed order: convert{rate,to},
    percent{value}, round{step}.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="pricing_rules",
    )
    name = models.CharField(max_length=100)
    matcher = models.JSONField(default=dict)
    supplier_strategy = models.CharField(
        max_length=16,
        choices=SupplierStrategy.choices,
        default=SupplierStrategy.LOWEST,
    )
    preferred_supplier = models.CharField(max_length=255, blank=True)
    ops = models.JSONField(default=list)
    priority = models.PositiveIntegerField(default=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_org_pricing_rule",
            )
        ]
        ordering = ["priority", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization_id})"


class SpendAllowance(models.Model):
    """Per-organization token budget with reservation accounting.

    Budgets start at zero, so paid dispatch is refused until an owner sets
    an explicit allowance. Reservation is atomic under row lock; reconcile
    converts reservations into spend after observed usage arrives.

    Credits are tokens 1:1. Each company gets a monthly allowance that
    resets on its own billing cycle (anchored at activation, so a company
    activated on the 10th resets on the 10th). tokens_spent is the grand
    total and is never reset; period_spent tracks the current cycle.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="spend_allowance",
    )
    token_budget = models.PositiveIntegerField(default=0)
    tokens_reserved = models.PositiveIntegerField(default=0)
    tokens_spent = models.PositiveIntegerField(default=0)
    in_flight = models.PositiveIntegerField(default=0)
    paused = models.BooleanField(default=False)
    monthly_credits = models.PositiveIntegerField(default=0)
    cycle_anchor = models.DateField(default=timezone.localdate)
    period_start = models.DateField(default=timezone.localdate)
    period_spent = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class OperatorSpend(models.Model):
    """Operator-wide token ceiling across all organizations (singleton).

    Every reserve/reconcile/release also moves these counters under the
    same row lock as the organization's allowance (operator row first,
    then allowance, then reservation — one global order, no deadlocks),
    so the sum of all tenants can never pass the operator's approved
    monthly ceiling even when each tenant is within its own allowance.
    The ceiling starts at zero: no paid dispatch anywhere until the
    operator approves an explicit ceiling. The calendar-month period
    snaps lazily like organization cycles; lifetime_spent never resets.
    """

    monthly_ceiling = models.PositiveIntegerField(default=0)
    period_start = models.DateField(default=timezone.localdate)
    period_spent = models.PositiveIntegerField(default=0)
    tokens_reserved = models.PositiveIntegerField(default=0)
    lifetime_spent = models.PositiveIntegerField(default=0)
    paused = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Operator spend (ceiling {self.monthly_ceiling})"


class SpendReservation(models.Model):
    """One held token reservation with identity, lifecycle, and expiry.

    States: held (budget + slot committed) -> reconciled (observed or
    estimated spend booked) | released (failed before any possible
    spend) | uncertain (the call may have consumed: timeout, dropped
    connection, worker loss — counters stay held, never freed) |
    expired (swept after expiry; uncertain expiries book the held
    estimate as spend because the provider may have processed them).

    Reconcile/release resolve by reservation id under row lock, so
    retries, redeliveries, and double callbacks are idempotent.
    """

    class State(models.TextChoices):
        HELD = "held", "Held"
        RECONCILED = "reconciled", "Reconciled"
        RELEASED = "released", "Released"
        UNCERTAIN = "uncertain", "Uncertain"
        EXPIRED = "expired", "Expired"

    allowance = models.ForeignKey(
        SpendAllowance,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="spend_reservations",
    )
    tokens = models.PositiveIntegerField()
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.HELD
    )
    purpose = models.CharField(max_length=64, default="", blank=True)
    detail = models.JSONField(default=dict)
    actual_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Reservation {self.id} ({self.state}, {self.tokens}t)"


class AssistantFlag(models.Model):
    """One assistant question refused by the server-side guard.

    The refusal costs the company nothing; the row exists so the
    super-admin can see abuse attempts and ban repeat offenders.
    The username is snapshotted because the user row may be deleted.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="assistant_flags",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    username = models.CharField(max_length=150)
    question = models.TextField()
    reason = models.CharField(
        max_length=16,
        choices=[
            ("sql", "SQL attempt"),
            ("code", "Code attempt"),
            ("destructive", "Destructive command"),
            ("cross_tenant", "Other companies' data"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "created_at"])]


class AssistIdempotency(models.Model):
    """One idempotent preview-assist request.

    The browser generates one key per send. If the connection drops
    after dispatch, retrying with the same key returns the stored
    result instead of paying for a second model call; a key whose
    first attempt is still running answers 409 instead of dispatching
    again. Keys are tenant-scoped and bound to one exact request, so
    a retry can never return another company's answer.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="assist_idempotency",
    )
    key = models.CharField(max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    preview_key = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    state = models.CharField(
        max_length=8,
        choices=[
            ("pending", "Pending"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="pending",
    )
    response = models.JSONField(null=True, blank=True)
    tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "key"],
                name="unique_org_assist_key",
            )
        ]
        indexes = [models.Index(fields=["organization", "updated_at"])]
