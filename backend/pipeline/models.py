"""Pipeline records: files, runs, rows, offers, reviews, artifacts, audit.

Every tenant-owned record carries a direct organization owner so row
policies stay simple equality checks. Rule set versions are the one global
reference table: every run binds to an exact version at creation.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


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
    # UPLOADED from NEEDS_INPUT or READY_FOR_REVIEW restarts processing
    # after mapping answers or a remap replaces the column mapping.
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
    RunStatus.APPROVED: {RunStatus.EXPORTING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.EXPORTING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COMPLETED: {RunStatus.EXPORTING},
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
    kind = models.CharField(max_length=64)
    quantity = models.PositiveIntegerField(default=1)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "created_at"])]


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
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="mapping_memories",
    )
    signature = models.CharField(max_length=64)
    headers = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    supplier_hint = models.CharField(max_length=255, blank=True)
    use_count = models.PositiveIntegerField(default=1)
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "signature"],
                name="unique_org_mapping_memory",
            )
        ]
        ordering = ["-last_used_at"]


class SupplierStrategy(models.TextChoices):
    LOWEST = "lowest", "Lowest price"
    HIGHEST = "highest", "Highest price"
    PREFERRED = "preferred", "Preferred supplier"


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
    updated_at = models.DateTimeField(auto_now=True)
