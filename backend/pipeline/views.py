"""Pipeline HTTP surface.

Object endpoints scope every lookup to the caller's organizations, so
cross-tenant identifiers answer 404 without leaking existence. Uploads name
their organization explicitly and are membership-checked before any byte is
stored.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, OuterRef, Subquery
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from common.storage import LocalFileAdapter
from organizations import services as org_services
from organizations.context import set_tenant_context
from organizations.models import Membership
from pipeline import review as review_services
from pipeline.export import create_export
from pipeline.jobs import cancel_run, remap_run
from pipeline.models import (
    AssistantFlag,
    Category,
    CustomerQuote,
    ExportArtifact,
    ImportRun,
    NormalizedOffer,
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
from pipeline.parsing import MessageCandidateFile, UploadTooLarge
from pipeline.previews import (
    AssistInFlight,
    AssistRefused,
    DraftConflictError,
    apply_preview_assist,
    assist_preview,
    cancel_preview,
    confirm_preview,
    create_preview,
    create_preview_from_text,
    get_preview_draft,
    list_pending_previews,
    render_preview,
    resume_preview,
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
    PreviewAssistApplySerializer,
    PreviewAssistSerializer,
    QuoteConfirmSerializer,
    QuoteLinesSerializer,
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
    PreviewTextSerializer,
    UploadSerializer,
    ValidationSerializer,
)
from pipeline.uploads import create_upload, delete_upload, rename_upload

DOWNLOAD_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}

QUOTE_LIST_PAGE_SIZE = 50


def _as_drf_error(error: DjangoValidationError) -> DRFValidationError:
    messages = getattr(error, "messages", [str(error)])
    return DRFValidationError({"detail": messages[0] if len(messages) == 1 else messages})


def _scoped_fetch(user, getter, org_of):
    """Fetch one tenant object and scope row policy to its organization.

    The session's active organization usually matches, so the first fetch
    runs as-is. When the object lives in another member organization, each
    membership is retried once so multi-org members keep exactly the
    access the application filters allow. Foreign objects still 404, and
    the middleware clears the narrowed context after the request.
    """
    from django.http import Http404

    try:
        found = getter()
    except Http404:
        found = None
    if found is None:
        for org_id in (
            Membership.objects.filter(user=user)
            .values_list("organization_id", flat=True)
            .order_by("organization_id")
            .distinct()
        ):
            set_tenant_context(org_id, user.id)
            try:
                found = getter()
            except Http404:
                continue
            break
    if found is None:
        raise Http404("No object matches the given query.")
    set_tenant_context(org_of(found), user.id)
    return found


def _run_for_user(user, run_id: int) -> ImportRun:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            ImportRun.objects.filter(organization__memberships__user=user),
            pk=run_id,
        ),
        lambda run: run.organization_id,
    )


def _review_for_user(user, review_id: int) -> ReviewRequest:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            ReviewRequest.objects.filter(
                run__organization__memberships__user=user
            ).select_related("run", "offer"),
            pk=review_id,
        ),
        lambda review: review.organization_id,
    )


def _artifact_for_user(user, artifact_id: int) -> ExportArtifact:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            ExportArtifact.objects.filter(organization__memberships__user=user),
            pk=artifact_id,
        ),
        lambda artifact: artifact.organization_id,
    )


def _file_for_user(user, file_id: int) -> SourceFile:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            SourceFile.objects.filter(
                organization__memberships__user=user
            ).select_related("organization", "uploaded_by", "category"),
            pk=file_id,
        ),
        lambda source_file: source_file.organization_id,
    )


def _rule_for_user(user, rule_id: int) -> PricingRule:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            PricingRule.objects.filter(organization__memberships__user=user),
            pk=rule_id,
        ),
        lambda rule: rule.organization_id,
    )


def _category_for_user(user, category_id: int) -> Category:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            Category.objects.filter(organization__memberships__user=user),
            pk=category_id,
        ),
        lambda category: category.organization_id,
    )


def _supplier_for_user(user, supplier_id: int) -> Supplier:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            Supplier.objects.filter(organization__memberships__user=user),
            pk=supplier_id,
        ),
        lambda supplier: supplier.organization_id,
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
        except UploadTooLarge as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
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
        top_currency = (
            NormalizedOffer.objects.filter(run__source_file=OuterRef("pk"))
            .exclude(currency="")
            .values("currency")
            .annotate(n=Count("id"))
            .order_by("-n")
            .values("currency")[:1]
        )
        files = (
            SourceFile.objects.filter(organization__memberships__user=request.user)
            .select_related("organization", "uploaded_by", "run", "category")
            .annotate(annotated_currency=Subquery(top_currency))
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
        except MessageCandidateFile as candidate:
            return Response(
                {
                    "message_candidate": True,
                    "filename": serializer.validated_data["file"].name,
                    "text": candidate.text,
                    "truncated": candidate.truncated,
                    "total_chars": candidate.total_chars,
                    "explanation": str(candidate),
                }
            )
        except UploadTooLarge as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(preview, status=status.HTTP_201_CREATED)


class PreviewTextView(APIView):
    """Preview pasted text exactly like an uploaded .txt file."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = PreviewTextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            organization = org_services.authorize_organization_access(
                request.user, data["organization_id"]
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        try:
            preview = create_preview_from_text(
                organization=organization,
                user=request.user,
                text=data["text"],
            )
        except UploadTooLarge as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
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
                sheet=serializer.validated_data.get("sheet"),
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(result)


class PreviewConfirmView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, key):
        serializer = MappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data.get("revision") is None:
            return Response(
                {
                    "detail": (
                        "Confirm needs the draft revision you reviewed. "
                        "Reload the preview and confirm again."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            run = confirm_preview(
                key=key,
                user=request.user,
                mapping=serializer.validated_data["mapping"],
                constants=serializer.validated_data.get("constants"),
                ignored=serializer.validated_data.get("ignored"),
                sheet=serializer.validated_data.get("sheet"),
                revision=serializer.validated_data.get("revision"),
            )
        except DraftConflictError as error:
            return Response(
                {
                    "detail": str(error),
                    "revision_actual": error.revision_actual,
                    "revision_expected": error.revision_expected,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(RunSerializer(run).data, status=status.HTTP_201_CREATED)


class PreviewPendingListView(APIView):
    """Metadata-only list of the org's resumable pending previews."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        organization_id = request.query_params.get("organization_id")
        if organization_id is None:
            return Response(
                {"detail": "organization_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            items = list_pending_previews(
                user=request.user, organization_id=organization_id
            )
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response({"previews": items})


class PreviewResumeView(APIView):
    """Rebuild the full preview payload for a pending draft."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, key):
        try:
            preview = resume_preview(key=key, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(preview)


class PreviewCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, key):
        try:
            draft = get_preview_draft(key=key, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(draft)

    def delete(self, request, key):
        try:
            cancel_preview(key=key, user=request.user)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class PreviewAssistView(APIView):
    """One propose-then-apply assist turn against a pending preview."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, key):
        from common.meta import ASSISTANT_REFUSAL, ProposalUnusable, ProviderError
        from common.provider import ProviderNotConfigured

        serializer = PreviewAssistSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = assist_preview(
                key=key,
                user=request.user,
                conversation=serializer.validated_data["messages"],
                idempotency_key=serializer.validated_data.get(
                    "idempotency_key"
                ),
            )
        except AssistRefused:
            return Response(
                {"refusal": ASSISTANT_REFUSAL}, status=status.HTTP_200_OK
            )
        except AssistInFlight as error:
            return Response(
                {"detail": str(error), "retryable": True},
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except ProposalUnusable as error:
            return Response(
                {
                    "message": (
                        "I could not turn that into a safe preview change. "
                        "Tell me the exact value to set or the line numbers "
                        "to drop."
                    ),
                    "proposal": {
                        "set_constants": {},
                        "drop_lines": [],
                        "dropped_previews": [],
                        "operations": [],
                    },
                    "tokens": getattr(error, "tokens", 0),
                    "retriable": True,
                    "hint": str(error),
                },
                status=status.HTTP_200_OK,
            )
        except ProviderNotConfigured as error:
            return Response(
                {"detail": str(error)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
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
        return Response(result)


class PreviewAssistApplyView(APIView):
    """Apply the confirmed assist proposal and return the fresh preview."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, key):
        serializer = PreviewAssistApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data.get("revision") is None:
            return Response(
                {
                    "detail": (
                        "Apply needs the draft revision you reviewed. "
                        "Reload the preview and apply again."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = apply_preview_assist(
                key=key,
                user=request.user,
                set_constants=serializer.validated_data["set_constants"],
                drop_lines=serializer.validated_data["drop_lines"],
                operations=serializer.validated_data.get("operations"),
                revision=serializer.validated_data.get("revision"),
                base_revision=serializer.validated_data.get("base_revision"),
            )
        except DraftConflictError as error:
            return Response(
                {
                    "detail": str(error),
                    "revision_actual": error.revision_actual,
                    "revision_expected": error.revision_expected,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(result)


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
                user=request.user,
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
    """Preview the customer quote: one repriced line per product group.

    Internal preview for members: readable without release approval, but
    labeled as such and never a shareable file. Downloads go through
    QuoteDownloadView and the shared release checks.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        run = _run_for_user(request.user, run_id)
        return Response({**build_quote(run), "preview": True})


class QuoteDownloadView(APIView):
    """Download the customer quote as xlsx or csv."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id, file_format="xlsx"):
        from pipeline.release import check_run_release

        run = _run_for_user(request.user, run_id)
        if file_format not in ("xlsx", "csv"):
            return Response(
                {"detail": 'Format must be "xlsx" or "csv".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            check_run_release(run, for_quote=True)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        import io

        quote = build_quote(run)
        policy = (run.mapping or {}).get("document_policy", "stock")
        if file_format == "xlsx":
            content = render_quote_xlsx(quote, document_policy=policy)
        else:
            content = render_quote_csv(quote, document_policy=policy)
        return FileResponse(
            io.BytesIO(content),
            content_type=DOWNLOAD_TYPES[file_format],
            filename=f"offerflow-quote-{run.id}.{file_format}",
            as_attachment=True,
        )


def _quote_for_user(user, quote_id: int) -> CustomerQuote:
    return _scoped_fetch(
        user,
        lambda: get_object_or_404(
            CustomerQuote.objects.filter(organization__memberships__user=user),
            pk=quote_id,
        ),
        lambda quote: quote.organization_id,
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
        raw_page = request.query_params.get("page")
        if raw_page is None:
            return Response(CustomerQuoteSerializer(quotes, many=True).data)
        try:
            page = int(raw_page)
        except ValueError:
            return Response(
                {"detail": "page must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if page < 1:
            return Response(
                {"detail": "page must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        import math

        rows = list(quotes)
        pages = max(1, math.ceil(len(rows) / QUOTE_LIST_PAGE_SIZE))
        start = (page - 1) * QUOTE_LIST_PAGE_SIZE
        return Response(
            {
                "page": page,
                "pages": pages,
                "count": len(rows),
                "page_size": QUOTE_LIST_PAGE_SIZE,
                "results": CustomerQuoteSerializer(
                    rows[start : start + QUOTE_LIST_PAGE_SIZE], many=True
                ).data,
            }
        )

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
    """Read one customer quote (member read), or delete it (operators)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, quote_id):
        quote = _quote_for_user(request.user, quote_id)
        return Response(CustomerQuoteSerializer(quote).data)

    def delete(self, request, quote_id):
        from pipeline.customer_quotes import delete_customer_quote

        quote = _quote_for_user(request.user, quote_id)
        try:
            delete_customer_quote(quote=quote, user=request.user)
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


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


class CustomerQuoteLinesView(APIView):
    """Replace a ready quote's line overrides (labels, details, hidden)."""

    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, quote_id):
        from pipeline.customer_quotes import update_quote_lines

        quote = _quote_for_user(request.user, quote_id)
        serializer = QuoteLinesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            quote = update_quote_lines(
                quote=quote,
                user=request.user,
                edits=serializer.validated_data["edits"],
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        except DjangoPermissionDenied as error:
            raise DRFPermissionDenied(str(error)) from error
        return Response(CustomerQuoteSerializer(quote).data)


def _quote_export_columns(*, template: dict, include_category: bool) -> list:
    """Template file columns with the Category column conditional.

    The full quote keeps its Category column whenever shelves exist
    and the template lists it; a per-category file names the shelf in
    the filename instead.
    """
    keys = [key for key in template["file_columns"] if key != "category"]
    if include_category and "category" in template["file_columns"]:
        keys.append("category")
    return keys


class CustomerQuoteDownloadView(APIView):
    """Download a ready quote's lines as xlsx or csv."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, quote_id, file_format="xlsx"):
        import io

        from pipeline.release import check_customer_quote_release

        quote = _quote_for_user(request.user, quote_id)
        if file_format not in ("xlsx", "csv"):
            return Response(
                {"detail": 'Format must be "xlsx" or "csv".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            check_customer_quote_release(quote)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        from pipeline.customer_quotes import (
            effective_template,
            present_lines,
            quote_lines_for_category,
        )

        wanted = request.query_params.get("category_id")
        # Present the full frozen list first: edits bind to stable
        # line ids (or legacy full-list positions), so filtering
        # before presenting would misaddress them on shelves.
        presented = present_lines(lines=quote.lines, edits=quote.line_edits)
        try:
            lines, suffix = quote_lines_for_category(
                lines=presented, category_id=wanted
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        shown = [line for line in lines if not line["hidden"]]
        payload = {"lines": shown, "notes": quote.notes}
        include_category = wanted is None and any(
            isinstance(line, dict) and line.get("category_name")
            for line in shown
        )
        template = effective_template(quote.organization)
        columns = _quote_export_columns(
            template=template, include_category=include_category
        )
        if file_format == "xlsx":
            content = render_quote_xlsx(payload, columns=columns)
        else:
            content = render_quote_csv(payload, columns=columns)
        return FileResponse(
            io.BytesIO(content),
            content_type=DOWNLOAD_TYPES[file_format],
            filename=f"offerflow-customer-quote-{quote.id}{suffix}.{file_format}",
            as_attachment=True,
        )


class CustomerQuoteMessageView(APIView):
    """Read the Telegram message whole or for one category (member read).

    The message renders live from the frozen lines, the line edits,
    and the company's current export template, so overrides, hidden
    lines, and format choices always match the latest save.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, quote_id):
        from pipeline.customer_quotes import (
            effective_template,
            present_lines,
            quote_lines_for_category,
            render_message,
        )

        from pipeline.release import check_customer_quote_release

        quote = _quote_for_user(request.user, quote_id)
        try:
            manifest = check_customer_quote_release(quote)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        wanted = request.query_params.get("category_id")
        presented = present_lines(lines=quote.lines, edits=quote.line_edits)
        try:
            lines, _ = quote_lines_for_category(
                lines=presented, category_id=wanted
            )
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(
            {
                "message": render_message(
                    organization_name=quote.organization.name,
                    day=quote.created_at.date(),
                    lines=[line for line in lines if not line["hidden"]],
                    template=effective_template(quote.organization),
                ),
                "release": manifest,
            }
        )
