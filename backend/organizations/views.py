"""Membership HTTP surface. Authentication is required globally; every view
rechecks live membership on the server before acting.
"""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from organizations import services
from organizations.models import DateTimeFormat, Membership, Organization, UserPreference
from organizations.serializers import (
    ActiveOrganizationSerializer,
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    MemberSerializer,
    MembershipSerializer,
    OrganizationSettingsSerializer,
    PreferenceSerializer,
)


def _as_drf_error(error: DjangoValidationError) -> DRFValidationError:
    messages = getattr(error, "messages", [str(error)])
    return DRFValidationError({"detail": messages[0] if len(messages) == 1 else messages})


class MembershipListView(APIView):
    """List the caller's own memberships across organizations."""

    def get(self, request):
        memberships = Membership.objects.filter(user=request.user).select_related(
            "organization"
        )
        return Response(MembershipSerializer(memberships, many=True).data)


class OrganizationUsageView(APIView):
    """Credit dashboard data for one company (member read).

    Credits are tokens 1:1 against the monthly allowance. Reading snaps
    the billing period first, so the numbers always describe the current
    cycle; the grand total is never reset.
    """

    def get(self, request, organization_id):
        from pipeline.models import UsageEvent
        from pipeline.spending import (
            current_period_start,
            get_allowance,
            next_reset_date,
        )

        organization = services.authorize_organization_access(
            request.user, organization_id
        )
        allowance = get_allowance(organization.id)
        period = current_period_start(allowance.cycle_anchor)
        if allowance.period_start != period:
            allowance.period_start = period
            allowance.period_spent = 0
            allowance.save(
                update_fields=["period_start", "period_spent", "updated_at"]
            )
        used = allowance.period_spent
        calls = (
            UsageEvent.objects.filter(
                organization=organization, kind="provider_call", run__isnull=False
            )
            .select_related("run", "run__source_file")
            .order_by("-created_at")[:500]
        )
        imports: list[dict] = []
        for event in calls:
            detail = event.detail or {}
            entry = next(
                (row for row in imports if row["run_id"] == event.run_id), None
            )
            if entry is None:
                if len(imports) >= 50:
                    continue
                entry = {
                    "run_id": event.run_id,
                    "filename": event.run.source_file.original_name,
                    "created_at": event.run.created_at.isoformat(),
                    "run_status": event.run.status,
                    "requests": 0,
                    "tokens": 0,
                    "calls": [],
                }
                imports.append(entry)
            entry["requests"] += 1
            entry["tokens"] += event.quantity
            entry["calls"].append(
                {
                    "at": event.created_at.isoformat(),
                    "prompt_tokens": detail.get("prompt_tokens", 0),
                    "completion_tokens": detail.get("completion_tokens", 0),
                    "total": event.quantity,
                    "model": detail.get("model", ""),
                }
            )
        match_calls = (
            UsageEvent.objects.filter(
                organization=organization, kind="provider_call", run__isnull=True
            )
            .order_by("-created_at")[:50]
        )
        matching = []
        for event in match_calls:
            detail = event.detail or {}
            matching.append(
                {
                    "at": event.created_at.isoformat(),
                    "task": detail.get("task", ""),
                    "prompt_tokens": detail.get("prompt_tokens", 0),
                    "completion_tokens": detail.get("completion_tokens", 0),
                    "total": event.quantity,
                    "model": detail.get("model", ""),
                    "label": detail.get("block", "")
                    or detail.get("question", ""),
                }
            )
        return Response(
            {
                "allowance": allowance.monthly_credits,
                "used": used,
                "remaining": max(0, allowance.monthly_credits - used),
                "period_start": period.isoformat(),
                "reset_date": next_reset_date(allowance.cycle_anchor).isoformat(),
                "grand_total": allowance.tokens_spent,
                "paused": allowance.paused,
                "imports": imports,
                "matching": matching,
            }
        )


class ActiveOrganizationView(APIView):
    """Read or select the session's active organization."""

    def get(self, request):
        membership = services.get_active_membership(request.user, request.session)
        if membership is None:
            return Response({"organization": None})
        return Response(MembershipSerializer(membership).data)

    def post(self, request):
        serializer = ActiveOrganizationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            membership = services.set_active_organization(
                request.session,
                request.user,
                serializer.validated_data["organization_id"],
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

            raise DRFPermissionDenied(str(error)) from error
        return Response(MembershipSerializer(membership).data)


class InvitationCreateView(APIView):
    """Invite an email address into an organization the caller manages."""

    def post(self, request):
        serializer = InvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        organization = get_object_or_404(Organization, pk=data["organization_id"])
        try:
            invitation = services.invite_member(
                organization=organization,
                email=data["email"],
                role=data["role"],
                invited_by=request.user,
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

            raise DRFPermissionDenied(str(error)) from error
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(
            {"id": invitation.id, "email": invitation.email, "role": invitation.role},
            status=status.HTTP_201_CREATED,
        )


class InvitationAcceptView(APIView):
    """Accept a live invitation addressed to the caller's email."""

    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            membership = services.accept_invitation(
                token=serializer.validated_data["token"], user=request.user
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

            raise DRFPermissionDenied(str(error)) from error
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(MembershipSerializer(membership).data)


class MemberListView(APIView):
    """List members of an organization the caller belongs to."""

    def get(self, request, organization_id: int):
        try:
            organization = services.authorize_organization_access(
                request.user, organization_id
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

            raise DRFPermissionDenied(str(error)) from error
        memberships = Membership.objects.filter(organization=organization).select_related(
            "user"
        )
        return Response(MemberSerializer(memberships, many=True).data)


class PreferenceView(APIView):
    """Read or update the caller's display preferences."""

    def get(self, request):
        preference = UserPreference.objects.filter(user=request.user).first()
        if preference is None:
            return Response({"datetime_format": DateTimeFormat.US})
        return Response(PreferenceSerializer(preference).data)

    def put(self, request):
        serializer = PreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preference, _created = UserPreference.objects.update_or_create(
            user=request.user,
            defaults={"datetime_format": serializer.validated_data["datetime_format"]},
        )
        return Response(PreferenceSerializer(preference).data)


class OrganizationSettingsView(APIView):
    """Read or update shared organization settings."""

    def get(self, request, organization_id):
        try:
            organization = services.authorize_organization_access(
                request.user, organization_id
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

            raise DRFPermissionDenied(str(error)) from error
        return Response(OrganizationSettingsSerializer(organization).data)

    def put(self, request, organization_id):
        try:
            organization = services.authorize_organization_access(
                request.user, organization_id
            )
            services.require_organization_role(request.user, organization.id)
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

            raise DRFPermissionDenied(str(error)) from error
        serializer = OrganizationSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = {}
        for field in ("default_currency", "destination_currency"):
            if field in request.data:
                value = serializer.validated_data[field]
                setattr(organization, field, value)
                updated[field] = value
        if updated:
            organization.save(update_fields=list(updated))
        from pipeline.jobs import record_audit

        record_audit(
            organization.id,
            request.user,
            "org_settings_updated",
            "organization",
            organization.id,
            updated,
        )
        return Response(OrganizationSettingsSerializer(organization).data)


class OrganizationGdprExportView(APIView):
    """Download every organization row plus stored bytes (Owner only)."""

    def get(self, request, organization_id):
        import io

        from django.http import FileResponse

        from organizations.gdpr import build_export_archive

        try:
            organization = services.require_organization_owner(
                request.user, organization_id
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import (
                PermissionDenied as DRFPermissionDenied,
            )

            raise DRFPermissionDenied(str(error)) from error
        content, manifest = build_export_archive(organization)
        missing = [
            entry
            for entry in manifest["uploads"] + manifest["artifacts"]
            if not entry["included"]
        ]
        if missing:
            from pipeline.jobs import record_audit

            record_audit(
                organization.id,
                request.user,
                "gdpr_export_partial",
                "organization",
                organization.id,
                {"missing": len(missing)},
            )
        safe = "".join(
            char if char.isalnum() else "_" for char in organization.name[:40]
        )
        return FileResponse(
            io.BytesIO(content),
            content_type="application/zip",
            filename=f"offerflow-export-{safe or 'org'}-{organization.id}.zip",
            as_attachment=True,
        )


class OrganizationGdprDeleteView(APIView):
    """Erase the organization after typed-name confirmation (Owner only)."""

    def post(self, request, organization_id):
        from organizations.gdpr import erase_organization

        try:
            organization = services.require_organization_owner(
                request.user, organization_id
            )
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import (
                PermissionDenied as DRFPermissionDenied,
            )

            raise DRFPermissionDenied(str(error)) from error
        confirm = request.data.get("confirm", "")
        if confirm != organization.name:
            return Response(
                {"detail": "Type the organization name to confirm deletion."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = erase_organization(organization=organization)
        except DjangoValidationError as error:
            raise _as_drf_error(error) from error
        return Response(result)
