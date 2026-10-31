"""API shapes for uploads, runs, reviews, and export artifacts."""

from rest_framework import serializers

from pipeline.models import (
    ArtifactFormat,
    Category,
    CustomerQuote,
    ExportArtifact,
    ImportRun,
    NormalizedOffer,
    PricingRule,
    ReviewRequest,
    SourceFile,
    SourceRow,
    Supplier,
    SupplierNote,
    ValidationResult,
)


class UploadSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)
    file = serializers.FileField()


class ArtifactSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExportArtifact
        fields = ["id", "format", "sha256", "size_bytes", "created_at"]
        read_only_fields = fields


class RunSerializer(serializers.ModelSerializer):
    rule_version = serializers.CharField(source="rule_version.version", read_only=True)
    counts = serializers.SerializerMethodField()
    artifacts = ArtifactSummarySerializer(many=True, read_only=True)

    class Meta:
        model = ImportRun
        fields = [
            "id",
            "organization",
            "status",
            "provider_name",
            "simulated",
            "error",
            "mapping",
            "rule_version",
            "approved_at",
            "approved_source_sha",
            "approved_normalized_digest",
            "created_at",
            "updated_at",
            "counts",
            "artifacts",
        ]
        read_only_fields = fields

    def get_counts(self, run):
        return {
            "rows": run.source_rows.count(),
            "offers": run.offers.count(),
            "open_reviews": run.reviews.filter(status="open").count(),
            "artifacts": run.artifacts.count(),
        }


class SourceRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = SourceRow
        fields = ["id", "index", "sheet", "data", "provenance"]
        read_only_fields = fields


class OfferSerializer(serializers.ModelSerializer):
    source_row = serializers.IntegerField(source="source_row.index", read_only=True)

    class Meta:
        model = NormalizedOffer
        fields = [
            "id",
            "source_row",
            "supplier",
            "brand",
            "model_name",
            "capacity_gb",
            "color",
            "grade",
            "grade_raw",
            "lock_state",
            "quantity",
            "unit_price",
            "currency",
            "conditions",
            "status",
            "exclusion_reason",
            "provenance",
        ]
        read_only_fields = fields


class ValidationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ValidationResult
        fields = ["name", "passed", "message", "details", "created_at"]
        read_only_fields = fields


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewRequest
        fields = [
            "id",
            "offer",
            "kind",
            "field",
            "question",
            "evidence",
            "choices",
            "status",
            "created_at",
        ]
        read_only_fields = fields


class AnswerSerializer(serializers.Serializer):
    choice = serializers.CharField(required=False, allow_blank=True, default="")
    value = serializers.JSONField(required=False)


class ExportRequestSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=ArtifactFormat.values)


class ArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExportArtifact
        fields = [
            "id",
            "run",
            "format",
            "sha256",
            "size_bytes",
            "source_sha",
            "rule_version",
            "normalized_digest",
            "created_at",
        ]
        read_only_fields = fields


class SourceFileSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(
        source="organization.name", read_only=True
    )
    run_id = serializers.SerializerMethodField()
    run_status = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()
    currency_date = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()

    class Meta:
        model = SourceFile
        fields = [
            "id",
            "organization",
            "organization_name",
            "original_name",
            "size_bytes",
            "sha256",
            "run_id",
            "run_status",
            "uploaded_by_name",
            "currency_date",
            "fx_destination",
            "fx_rates",
            "fx_source",
            "fx_simulated",
            "fx_fetched_at",
            "category",
            "category_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_category_name(self, obj):
        if obj.category_id and obj.category:
            return obj.category.name
        return ""

    def get_currency_date(self, obj):
        if obj.currency_date is not None:
            return obj.currency_date.isoformat()
        return obj.created_at.date().isoformat()

    def get_run_id(self, obj):
        try:
            return obj.run.id
        except ImportRun.DoesNotExist:
            return None

    def get_run_status(self, obj):
        try:
            return obj.run.status
        except ImportRun.DoesNotExist:
            return None

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by_id and obj.uploaded_by:
            return obj.uploaded_by.username
        return None


class FxDateSerializer(serializers.Serializer):
    currency_date = serializers.DateField()


class FileRenameSerializer(serializers.Serializer):
    original_name = serializers.CharField(max_length=255)

    def validate_original_name(self, value):
        text = (value or "").strip()
        if not text:
            raise serializers.ValidationError("Name must not be blank.")
        return text


class MappingSerializer(serializers.Serializer):
    mapping = serializers.DictField(child=serializers.IntegerField(min_value=0))
    constants = serializers.DictField(required=False, default=dict)
    ignored = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class PricingRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PricingRule
        fields = [
            "id",
            "organization",
            "name",
            "matcher",
            "supplier_strategy",
            "preferred_supplier",
            "ops",
            "priority",
            "active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "organization", "created_at", "updated_at"]


class PricingRuleInputSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1, required=False)
    name = serializers.CharField(max_length=100, required=False)
    matcher = serializers.DictField(required=False)
    supplier_strategy = serializers.CharField(max_length=16, required=False)
    preferred_supplier = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    ops = serializers.ListField(required=False)
    priority = serializers.IntegerField(min_value=0, required=False)
    active = serializers.BooleanField(required=False)


class QuoteStartSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)


class QuoteConfirmSerializer(serializers.Serializer):
    groups = serializers.ListField(
        child=serializers.DictField(), min_length=1, allow_empty=False
    )


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "organization", "name", "created_at"]
        read_only_fields = ["id", "organization", "created_at"]


class CategoryInputSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1, required=False)
    name = serializers.CharField(max_length=100, required=False, allow_blank=True)


class FileCategorySerializer(serializers.Serializer):
    category_id = serializers.IntegerField(
        min_value=1, required=False, allow_null=True
    )


class SupplierSerializer(serializers.ModelSerializer):
    notes_count = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = [
            "id",
            "organization",
            "name",
            "favorite_rank",
            "location",
            "customs_required",
            "customs_fee_percent",
            "rating_shipping",
            "rating_quality",
            "rating_payment",
            "rating_quantity",
            "rating_rma",
            "notes_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_notes_count(self, obj):
        return obj.notes.count()


class SupplierInputSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1, required=False)
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    location = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    customs_required = serializers.BooleanField(required=False)
    customs_fee_percent = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    rating_shipping = serializers.IntegerField(
        required=False, allow_null=True
    )
    rating_quality = serializers.IntegerField(required=False, allow_null=True)
    rating_payment = serializers.IntegerField(required=False, allow_null=True)
    rating_quantity = serializers.IntegerField(required=False, allow_null=True)
    rating_rma = serializers.IntegerField(required=False, allow_null=True)


class SupplierOrderSerializer(serializers.Serializer):
    ordered_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=True
    )


class SupplierNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = SupplierNote
        fields = ["id", "supplier", "author_name", "text", "created_at"]
        read_only_fields = fields

    def get_author_name(self, obj):
        if obj.author_id and obj.author:
            return obj.author.username
        return ""


class SupplierNoteInputSerializer(serializers.Serializer):
    text = serializers.CharField(required=False, allow_blank=True)


class AssistantChatSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)
    messages = serializers.ListField(
        child=serializers.DictField(), min_length=1, max_length=10
    )


class CustomerQuoteSerializer(serializers.ModelSerializer):
    sources = serializers.SerializerMethodField()

    class Meta:
        model = CustomerQuote
        fields = [
            "id",
            "organization",
            "status",
            "source_run_ids",
            "sources",
            "groups",
            "lines",
            "notes",
            "message",
            "provider_name",
            "simulated",
            "created_at",
        ]
        read_only_fields = fields

    def get_sources(self, obj):
        runs = {
            run.id: run
            for run in ImportRun.objects.filter(
                id__in=obj.source_run_ids or []
            ).select_related("source_file")
        }
        return [
            {
                "run_id": run_id,
                "filename": runs[run_id].source_file.original_name,
            }
            for run_id in (obj.source_run_ids or [])
            if run_id in runs
        ]
