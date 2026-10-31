"""Pipeline HTTP surface.

Object endpoints scope every lookup to the caller's organizations, so
cross-tenant identifiers answer 404 without leaking existence. Uploads name
their organization explicitly and are membership-checked before any byte is
stored.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from common.storage import LocalFileAdapter
from organizations import services as org_services
from pipeline import review as review_services
from pipeline.export import create_export
from pipeline.jobs import cancel_run, remap_run
from pipeline.models import (
    AssistantFlag,
    Category,
    CustomerQuote,
    ExportArtifact,
    ImportRun,
    PricingRule,
    ReviewRequest,
    ReviewStatus,
    SourceFile,
    Supplier,
    SupplierNote,
)
from pipeline.quotes import (
    build_quote,
    create_rule,
    delete_rule,
    render_quote_csv,
    render_quote_xlsx,
    update_rule,
)
from pipeline.previews import (
    cancel_preview,
    confirm_preview,
    create_preview,
    render_preview,
)
from pipeline.serializers import (
    AnswerSerializer,
    ArtifactSerializer,
    AssistantChatSerializer,
    CategoryInputSerializer,
    CategorySerializer,
    CustomerQuoteSerializer,
    ExportRequestSerializer,
    FileCategorySerializer,
    QuoteConfirmSerializer,
    SupplierInputSerializer,
    SupplierNoteInputSerializer,
    SupplierNoteSerializer,
    SupplierOrderSerializer,
    SupplierSerializer,
    QuoteStartSerializer,
    FileRenameSerializer,
    FxDateSerializer,
    MappingSerializer,
    OfferSerializer,
    PricingRuleInputSerializer,
    PricingRuleSerializer,
    ReviewSerializer,
    RunSerializer,
    SourceFileSerializer,
    UploadSerializer,
    ValidationSerializer,
)
from pipeline.uploads import create_upload, delete_upload, rename_upload

DOWNLOAD_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


def _as_drf_error(error: DjangoValidationError) -> DRFValidationError:
    messages = getattr(error, "messages", [str(error)])
    return DRFValidationError({"detail": messages[0] if len(messages) == 1 else messages})


def _run_for_user(user, run_id: int) -> ImportRun:
    return get_object_or_404(
        ImportRun.objects.filter(organization__memberships__user=user), pk=run_id
    )


def _review_for_user(user, review_id: int) -> ReviewRequest:
    return get_object_or_404(
        ReviewRequest.objects.filter(
            run__organization__memberships__user=user
        ).select_related("run", "offer"),
        pk=review_id,
    )


def _artifact_for_user(user, artifact_id: int) -> ExportArtifact:
    return get_object_or_404(
        ExportArtifact.objects.filter(organization__memberships__user=user),
        pk=artifact_id,
    )


def _file_for_user(user, file_id: int) -> SourceFile:
    return get_object_or_404(
        SourceFile.objects.filter(
            organization__memberships__user=user
        ).select_related("organization", "uploaded_by", "category"),
        pk=file_id,
    )


def _rule_for_user(user, rule_id: int) -> PricingRule:
    return get_object_or_404(
        PricingRule.objects.filter(organization__memberships__user=user),
        pk=rule_id,
    )


def _category_for_user(user, category_id: int) -> Category:
    return get_object_or_404(
        Category.objects.filter(organization__memberships__user=user),
        pk=category_id,
    )


def _supplier_for_user(user, supplier_id: int) -> Supplier:
    return get_object_or_404(
        Supplier.objects.filter(organization__memberships__user=user),
        pk=supplier_id,
    )


def _supplier_note_for_user(user, supplier_id: int, note_id: int) -> SupplierNote:
    supplier = _supplier_for_user(user, supplier_id)
    return get_object_or_404(
        SupplierNote.objects.filter(supplier=supplier), pk=note_id
    )


class UploadView(APIView):
    """Store an upload and start its processing run."""

    def post(self, request):
        serializer = UploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            run = create_upload(
                organization=organization,
                user=request.user,
                uploaded_file=data["file"],
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(RunSerializer(run).data, status=status.HTTP_201_CREATED)


class RunDetailView(APIView):
    """Read one run with its validation results."""

    def get(self, request, run_id: int):
        run = _run_for_user(request.user, run_id)
        payload = RunSerializer(run).data
        payload["validations"] = ValidationSerializer(
            run.validations.order_by("name"), many=True
        ).data
        return Response(payload)


class OfferListView(APIView):
    """List a run's normalized offers with provenance."""

    def get(self, request, run_id: int):
        run = _run_for_user(request.user, run_id)
        offers = run.offers.order_by("id").select_related("source_row")
        return Response(OfferSerializer(offers, many=True).data)


class ReviewListView(APIView):
    """List a run's review requests, open ones by default."""

    def get(self, request, run_id: int):
        run = _run_for_user(request.user, run_id)
        reviews = run.reviews.order_by("created_at")
        if request.query_params.get("status", "open") != "all":
            reviews = reviews.filter(status=ReviewStatus.OPEN)
        return Response(ReviewSerializer(reviews, many=True).data)


class AnswerView(APIView):
    """Answer one open review request."""

    def post(self, request, review_id: int):
        review = _review_for_user(request.user, review_id)
        serializer = AnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        kwargs = {"choice": data.get("choice", "")}
        if "value" in data:
            kwargs["value"] = data["value"]
        try:
            decision = review_services.answer_request(
                review=review, user=request.user, **kwargs
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(
            {
                "review": review.id,
                "decision": decision.id,
                "run_status": review.run.status,
            }
        )


class IgnoreView(APIView):
    """Ignore one record instead of answering its question."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, review_id: int):
        review = _review_for_user(request.user, review_id)
        try:
            offer = review_services.ignore_request(
                review=review, user=request.user
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(
            {
                "review": review.id,
                "offer": offer.id,
                "run_status": review.run.status,
            }
        )


class ApproveView(APIView):
    """Approve a releasable run, binding exact versions."""

    def post(self, request, run_id: int):
        run = _run_for_user(request.user, run_id)
        try:
            review_services.approve_run(run=run, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        run.refresh_from_db()
        return Response(RunSerializer(run).data)


class ExportView(APIView):
    """Generate one immutable export artifact from an approved run."""

    def post(self, request, run_id: int):
        run = _run_for_user(request.user, run_id)
        serializer = ExportRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            artifact = create_export(
                run=run, user=request.user, format=serializer.validated_data["format"]
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(ArtifactSerializer(artifact).data, status=status.HTTP_201_CREATED)


class DownloadView(APIView):
    """Serve an artifact's exact stored bytes to organization members."""

    def get(self, request, artifact_id: int):
        artifact = _artifact_for_user(request.user, artifact_id)
        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        try:
            content = adapter.read_bytes(artifact.storage_key)
        except FileNotFoundError:
            return Response(
                {"detail": "Stored artifact is missing."},
                status=status.HTTP_410_GONE,
            )
        import io

        filename = f"offerflow-run-{artifact.run_id}.{artifact.format}"
        return FileResponse(
            io.BytesIO(content),
            content_type=DOWNLOAD_TYPES.get(artifact.format, "application/octet-stream"),
            filename=filename,
            as_attachment=True,
        )


class CancelView(APIView):
    """Cancel a run that has not finished."""

    def post(self, request, run_id: int):
        run = _run_for_user(request.user, run_id)
        try:
            cancel_run(run=run, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        run.refresh_from_db()
        return Response(RunSerializer(run).data)


class FileListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        files = (
            SourceFile.objects.filter(organization__memberships__user=request.user)
            .select_related("organization", "uploaded_by", "run", "category")
            .order_by("-created_at")
        )
        organization_id = request.query_params.get("organization_id")
        if organization_id:
            try:
                organization = org_services.authorize_organization_access(
                    request.user, organization_id
                )
            except DjangoPermissionDenied as error:
                raise DRFPermissionDenied(str(error)) from error
            files = files.filter(organization=organization)
        return Response(SourceFileSerializer(files, many=True).data)


class FileDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, file_id):
        source_file = _file_for_user(request.user, file_id)
        serializer = FileRenameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            source_file = rename_upload(
                source_file=source_file,
                user=request.user,
                original_name=serializer.validated_data["original_name"],
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(SourceFileSerializer(source_file).data)

    def delete(self, request, file_id):
        source_file = _file_for_user(request.user, file_id)
        try:
            delete_upload(source_file=source_file, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class FileFxView(APIView):
    """Re-fetch a file's recorded exchange rates for a picked date."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, file_id):
        from pipeline.fx import refresh_file_rates

        source_file = _file_for_user(request.user, file_id)
        serializer = FxDateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            source_file = refresh_file_rates(
                source_file=source_file,
                user=request.user,
                currency_date=serializer.validated_data["currency_date"],
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(SourceFileSerializer(source_file).data)


class RemapView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, run_id):
        run = _run_for_user(request.user, run_id)
        serializer = MappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = remap_run(
                run=run,
                user=request.user,
                mapping=serializer.validated_data["mapping"],
                constants=serializer.validated_data.get("constants"),
                ignored=serializer.validated_data.get("ignored"),
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(RunSerializer(run).data)


class PreviewCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = UploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            preview = create_preview(
                organization=organization,
                user=request.user,
                uploaded_file=serializer.validated_data["file"],
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(preview, status=status.HTTP_201_CREATED)


class PreviewRenderView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, key):
        serializer = MappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = render_preview(
                key=key,
                user=request.user,
                mapping=serializer.validated_data["mapping"],
                constants=serializer.validated_data.get("constants"),
                ignored=serializer.validated_data.get("ignored"),
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(result)


class PreviewConfirmView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, key):
        serializer = MappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = confirm_preview(
                key=key,
                user=request.user,
                mapping=serializer.validated_data["mapping"],
                constants=serializer.validated_data.get("constants"),
                ignored=serializer.validated_data.get("ignored"),
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(RunSerializer(run).data, status=status.HTTP_201_CREATED)


class PreviewCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, key):
        try:
            cancel_preview(key=key, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class RuleListView(APIView):
    """List rules for the member's orgs, or create one (operating roles)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rules = PricingRule.objects.filter(
            organization__memberships__user=request.user
        ).order_by("priority", "name")
        organization_id = request.query_params.get("organization_id")
        if organization_id:
            try:
                organization = org_services.authorize_organization_access(
                    request.user, organization_id
                )
            except DjangoPermissionDenied as error:
                raise DRFPermissionDenied(str(error)) from error
            rules = rules.filter(organization=organization)
        return Response(PricingRuleSerializer(rules, many=True).data)

    def post(self, request):
        serializer = PricingRuleInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("organization_id") is None:
            return Response(
                {"detail": "organization_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            rule = create_rule(
                organization=organization, user=request.user, data=data
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(
            PricingRuleSerializer(rule).data, status=status.HTTP_201_CREATED
        )


class RuleDetailView(APIView):
    """Read, replace, or delete one rule (member read, operating writes)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, rule_id):
        rule = _rule_for_user(request.user, rule_id)
        return Response(PricingRuleSerializer(rule).data)

    def put(self, request, rule_id):
        rule = _rule_for_user(request.user, rule_id)
        serializer = PricingRuleInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            rule = update_rule(
                rule=rule, user=request.user, data=serializer.validated_data
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(PricingRuleSerializer(rule).data)

    def delete(self, request, rule_id):
        rule = _rule_for_user(request.user, rule_id)
        try:
            delete_rule(rule=rule, user=request.user)
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class CategoryListView(APIView):
    """List a company's categories, or create one (operating roles)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        categories = Category.objects.filter(
            organization__memberships__user=request.user
        ).order_by("name")
        organization_id = request.query_params.get("organization_id")
        if organization_id:
            try:
                organization = org_services.authorize_organization_access(
                    request.user, organization_id
                )
            except DjangoPermissionDenied as error:
                raise DRFPermissionDenied(str(error)) from error
            categories = categories.filter(organization=organization)
        return Response(CategorySerializer(categories, many=True).data)

    def post(self, request):
        from pipeline.customer_quotes import create_category

        serializer = CategoryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("organization_id") is None:
            return Response(
                {"detail": "organization_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            category = create_category(
                organization=organization, user=request.user, data=data
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(
            CategorySerializer(category).data, status=status.HTTP_201_CREATED
        )


class CategoryDetailView(APIView):
    """Read, rename, or delete one category (member read, operating writes)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, category_id):
        category = _category_for_user(request.user, category_id)
        return Response(CategorySerializer(category).data)

    def put(self, request, category_id):
        from pipeline.customer_quotes import update_category

        category = _category_for_user(request.user, category_id)
        serializer = CategoryInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            category = update_category(
                category=category,
                user=request.user,
                data=serializer.validated_data,
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(CategorySerializer(category).data)

    def delete(self, request, category_id):
        from pipeline.customer_quotes import delete_category

        category = _category_for_user(request.user, category_id)
        try:
            delete_category(category=category, user=request.user)
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class FileCategoryView(APIView):
    """Map a whole file to one of its company's categories (or unmap)."""

    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, file_id):
        from pipeline.customer_quotes import set_file_category

        source_file = _file_for_user(request.user, file_id)
        serializer = FileCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            source_file = set_file_category(
                source_file=source_file,
                user=request.user,
                category_id=serializer.validated_data.get("category_id"),
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(SourceFileSerializer(source_file).data)


def _ordered_suppliers(user):
    from django.db.models import F

    return (
        Supplier.objects.filter(organization__memberships__user=user)
        .prefetch_related("notes")
        .order_by(F("favorite_rank").asc(nulls_last=True), "name")
    )


class SupplierListView(APIView):
    """List a company's suppliers, favorites first, or add one."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        suppliers = _ordered_suppliers(request.user)
        organization_id = request.query_params.get("organization_id")
        if organization_id:
            try:
                organization = org_services.authorize_organization_access(
                    request.user, organization_id
                )
            except DjangoPermissionDenied as error:
                raise DRFPermissionDenied(str(error)) from error
            suppliers = suppliers.filter(organization=organization)
        return Response(SupplierSerializer(suppliers, many=True).data)

    def post(self, request):
        from pipeline.suppliers import create_supplier

        serializer = SupplierInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("organization_id") is None:
            return Response(
                {"detail": "organization_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            supplier = create_supplier(
                organization=organization, user=request.user, data=data
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(
            SupplierSerializer(supplier).data, status=status.HTTP_201_CREATED
        )


class SupplierDetailView(APIView):
    """Read, edit, or delete one supplier (member read, operating writes)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, supplier_id):
        supplier = _supplier_for_user(request.user, supplier_id)
        return Response(SupplierSerializer(supplier).data)

    def put(self, request, supplier_id):
        from pipeline.suppliers import update_supplier

        supplier = _supplier_for_user(request.user, supplier_id)
        serializer = SupplierInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            supplier = update_supplier(
                supplier=supplier,
                user=request.user,
                data=serializer.validated_data,
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(SupplierSerializer(supplier).data)

    def delete(self, request, supplier_id):
        from pipeline.suppliers import delete_supplier

        supplier = _supplier_for_user(request.user, supplier_id)
        try:
            delete_supplier(supplier=supplier, user=request.user)
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class SupplierOrderView(APIView):
    """Rank the favorites shelf: ordered ids become ranks 1..N."""

    permission_classes = [permissions.IsAuthenticated]

    def put(self, request):
        from pipeline.suppliers import set_favorite_order

        serializer = SupplierOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization_id = request.query_params.get("organization_id")
        if not organization_id:
            return Response(
                {"detail": "organization_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            organization = org_services.authorize_organization_access(
                request.user, organization_id
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            suppliers = set_favorite_order(
                organization=organization,
                user=request.user,
                ordered_ids=serializer.validated_data["ordered_ids"],
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(SupplierSerializer(suppliers, many=True).data)


class SupplierNoteListView(APIView):
    """Read a supplier's note history, or append to it."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, supplier_id):
        supplier = _supplier_for_user(request.user, supplier_id)
        notes = supplier.notes.select_related("author").order_by("-created_at")
        return Response(SupplierNoteSerializer(notes, many=True).data)

    def post(self, request, supplier_id):
        from pipeline.suppliers import add_supplier_note

        supplier = _supplier_for_user(request.user, supplier_id)
        serializer = SupplierNoteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            note = add_supplier_note(
                supplier=supplier,
                user=request.user,
                text=serializer.validated_data.get("text", ""),
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(
            SupplierNoteSerializer(note).data, status=status.HTTP_201_CREATED
        )


class SupplierNoteDetailView(APIView):
    """Delete one supplier note (operating roles)."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, supplier_id, note_id):
        from pipeline.suppliers import delete_supplier_note

        note = _supplier_note_for_user(request.user, supplier_id, note_id)
        try:
            delete_supplier_note(note=note, user=request.user)
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class AssistantStatusView(APIView):
    """Whether the paid assistant is reachable (no spend to ask)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from common.provider import ProviderNotConfigured, get_provider

        organization_id = request.query_params.get("organization_id")
        if not organization_id:
            return Response(
                {"detail": "organization_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            organization = org_services.authorize_organization_access(
                request.user, organization_id
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            provider = get_provider(organization_id=organization.id)
        except ProviderNotConfigured:
            return Response({"available": False})
        return Response({"available": not provider.is_simulated})


class AssistantChatView(APIView):
    """One help-assistant turn, spent from company credits (any member)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from common.meta import ASSISTANT_REFUSAL, ProposalUnusable, ProviderError
        from common.provider import ProviderNotConfigured, get_provider

        serializer = AssistantChatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        conversation = []
        for turn in data["messages"]:
            if not isinstance(turn, dict):
                return self._refuse("Each message needs a role and content.")
            role, content = turn.get("role"), turn.get("content")
            if role not in ("user", "assistant"):
                return self._refuse("Message roles are user or assistant.")
            if (
                not isinstance(content, str)
                or not content.strip()
                or len(content) > 2000
            ):
                return self._refuse(
                    "Message text is required (max 2000 characters)."
                )
            conversation.append({"role": role, "content": content.strip()})
        if conversation[-1]["role"] != "user":
            return self._refuse("The conversation must end with your message.")
        from organizations.models import UserPreference

        from pipeline.assistant_guard import inspect_question

        if UserPreference.objects.filter(
            user=request.user, assistant_banned=True
        ).exists():
            return Response(
                {"detail": "Assistant access is disabled for your account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        reason = inspect_question(conversation[-1]["content"])
        if reason is not None:
            AssistantFlag.objects.create(
                organization=organization,
                user=request.user,
                username=request.user.username,
                question=conversation[-1]["content"][:2000],
                reason=reason,
            )
            return Response(
                {
                    "reply": ASSISTANT_REFUSAL,
                    "out_of_scope": True,
                    "tokens": 0,
                    "flagged": True,
                }
            )
        try:
            provider = get_provider(organization_id=organization.id)
        except ProviderNotConfigured as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if provider.is_simulated:
            return Response(
                {"detail": "The assistant needs the paid model."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        question = conversation[-1]["content"][:120]
        provider_model = getattr(getattr(provider, "_config", None), "model", "")
        if not isinstance(provider_model, str):
            provider_model = ""

        def _note_chunk(position, total, prompt_tokens, completion_tokens):
            from pipeline.jobs import record_usage

            record_usage(
                organization.id,
                None,
                "provider_call",
                prompt_tokens + completion_tokens,
                {
                    "provider": provider.name,
                    "model": provider_model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "task": "assistant",
                    "question": question,
                },
            )

        try:
            result = provider.chat(
                {"conversation": conversation}, on_chunk=_note_chunk
            )
        except ProposalUnusable as error:
            return Response(
                {"detail": f"Assistant answer unusable ({error}); not retried."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ProviderError as error:
            from pipeline.spending import BudgetRefused

            if isinstance(error.__cause__, BudgetRefused):
                return Response(
                    {"detail": str(error)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"detail": str(error)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        out_of_scope = bool(result.get("out_of_scope"))
        return Response(
            {
                "reply": ASSISTANT_REFUSAL if out_of_scope else result["reply"],
                "out_of_scope": out_of_scope,
                "tokens": (result.get("usage") or {}).get("total_tokens", 0),
            }
        )

    def _refuse(self, detail: str) -> Response:
        return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)


class QuoteView(APIView):
    """Preview the customer quote: one repriced line per product group."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        run = _run_for_user(request.user, run_id)
        return Response(build_quote(run))


class QuoteDownloadView(APIView):
    """Download the customer quote as xlsx or csv."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id, file_format="xlsx"):
        run = _run_for_user(request.user, run_id)
        if file_format not in ("xlsx", "csv"):
            return Response(
                {"detail": 'Format must be "xlsx" or "csv".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        import io

        quote = build_quote(run)
        if file_format == "xlsx":
            content = render_quote_xlsx(quote)
        else:
            content = render_quote_csv(quote)
        return FileResponse(
            io.BytesIO(content),
            content_type=DOWNLOAD_TYPES[file_format],
            filename=f"offerflow-quote-{run.id}.{file_format}",
            as_attachment=True,
        )


def _quote_for_user(user, quote_id: int) -> CustomerQuote:
    return get_object_or_404(
        CustomerQuote.objects.filter(organization__memberships__user=user),
        pk=quote_id,
    )


class CustomerQuoteListView(APIView):
    """List customer quotes, or start one from the latest supplier runs."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        quotes = CustomerQuote.objects.filter(
            organization__memberships__user=request.user
        ).order_by("-created_at")
        organization_id = request.query_params.get("organization_id")
        if organization_id:
            try:
                organization = org_services.authorize_organization_access(
                    request.user, organization_id
                )
            except DjangoPermissionDenied as error:
                raise DRFPermissionDenied(str(error)) from error
            quotes = quotes.filter(organization=organization)
        return Response(CustomerQuoteSerializer(quotes, many=True).data)

    def post(self, request):
        from common.meta import ProviderError
        from common.provider import ProviderNotConfigured

        from pipeline.customer_quotes import start_customer_quote

        serializer = QuoteStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            organization = org_services.authorize_organization_access(
                request.user, serializer.validated_data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            quote = start_customer_quote(
                organization=organization, user=request.user
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        except ProviderNotConfigured as error:
            return Response(
                {"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST
            )
        except ProviderError as error:
            return Response(
                {"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            CustomerQuoteSerializer(quote).data, status=status.HTTP_201_CREATED
        )


class CustomerQuoteDetailView(APIView):
    """Read one customer quote (member read)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, quote_id):
        quote = _quote_for_user(request.user, quote_id)
        return Response(CustomerQuoteSerializer(quote).data)


class CustomerQuoteConfirmView(APIView):
    """Confirm reconcile groups: learn them, then price the quote."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, quote_id):
        from pipeline.customer_quotes import confirm_customer_quote

        quote = _quote_for_user(request.user, quote_id)
        serializer = QuoteConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            quote = confirm_customer_quote(
                quote=quote,
                user=request.user,
                decisions=serializer.validated_data["groups"],
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(CustomerQuoteSerializer(quote).data)


class CustomerQuoteDownloadView(APIView):
    """Download a ready quote's lines as xlsx or csv."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, quote_id, file_format="xlsx"):
        import io

        quote = _quote_for_user(request.user, quote_id)
        if file_format not in ("xlsx", "csv"):
            return Response(
                {"detail": 'Format must be "xlsx" or "csv".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if quote.status != "ready":
            return Response(
                {"detail": "Confirm the product groups before downloading."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from pipeline.customer_quotes import quote_lines_for_category

        wanted = request.query_params.get("category_id")
        try:
            lines, suffix = quote_lines_for_category(
                lines=quote.lines, category_id=wanted
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        payload = {"lines": lines, "notes": quote.notes}
        # The full quote keeps its Category column whenever shelves
        # exist; a per-category file names the shelf in the filename.
        include_category = wanted is None and any(
            isinstance(line, dict) and line.get("category_name")
            for line in lines
        )
        if file_format == "xlsx":
            content = render_quote_xlsx(payload, include_category=include_category)
        else:
            content = render_quote_csv(payload, include_category=include_category)
        return FileResponse(
            io.BytesIO(content),
            content_type=DOWNLOAD_TYPES[file_format],
            filename=f"offerflow-customer-quote-{quote.id}{suffix}.{file_format}",
            as_attachment=True,
        )


class CustomerQuoteMessageView(APIView):
    """Read the Telegram message whole or for one category (member read)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, quote_id):
        from pipeline.customer_quotes import (
            quote_lines_for_category,
            render_message,
        )

        quote = _quote_for_user(request.user, quote_id)
        if quote.status != "ready":
            return Response(
                {"detail": "Confirm the product groups before messaging."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        wanted = request.query_params.get("category_id")
        if wanted is None:
            return Response({"message": quote.message})
        try:
            lines, _ = quote_lines_for_category(
                lines=quote.lines, category_id=wanted
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(
            {
                "message": render_message(
                    organization_name=quote.organization.name,
                    day=quote.created_at.date(),
                    lines=lines,
                )
            }
        )
