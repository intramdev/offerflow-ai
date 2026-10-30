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
    ExportArtifact,
    ImportRun,
    PricingRule,
    ReviewRequest,
    ReviewStatus,
    SourceFile,
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
    ExportRequestSerializer,
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
        ).select_related("organization", "uploaded_by"),
        pk=file_id,
    )


def _rule_for_user(user, rule_id: int) -> PricingRule:
    return get_object_or_404(
        PricingRule.objects.filter(organization__memberships__user=user),
        pk=rule_id,
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
            .select_related("organization", "uploaded_by", "run")
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
