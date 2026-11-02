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


USAGE_LOG_PAGE_SIZE = 50
USAGE_LOG_RETENTION_DAYS = 120


def _prune_usage_log(organization_id: int) -> None:
    """Delete metered log rows older than the retention window."""
    from datetime import timedelta

    from django.utils import timezone

    from pipeline.models import UsageEvent

    cutoff = timezone.now() - timedelta(days=USAGE_LOG_RETENTION_DAYS)
    UsageEvent.objects.filter(
        organization_id=organization_id,
        kind="provider_call",
        created_at__lt=cutoff,
    ).delete()


def _import_log_entries(organization) -> list:
    from pipeline.models import UsageEvent

    calls = (
        UsageEvent.objects.filter(
            organization=organization, kind="provider_call", run__isnull=False
        )
        .select_related("run", "run__source_file")
        .order_by("-created_at")
    )
    imports: list[dict] = []
    for event in calls:
        detail = event.detail or {}
        entry = next(
            (row for row in imports if row["run_id"] == event.run_id), None
        )
        if entry is None:
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
    return imports


def _loose_call_entries(organization) -> list:
    from pipeline.models import UsageEvent

    entries = []
    for event in UsageEvent.objects.filter(
        organization=organization, kind="provider_call", run__isnull=True
    ).order_by("-created_at"):
        detail = event.detail or {}
        entries.append(
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
    return entries


class OrganizationUsageView(APIView):
    """Credit dashboard data for one company (member read).

    Credits are tokens 1:1 against the monthly allowance. Reading snaps
    the billing period first, so the numbers always describe the current
    cycle; the grand total is never reset. Log rows older than the
    retention window are pruned on read.
    """

    def get(self, request, organization_id):
        from pipeline.spending import (
            current_period_start,
            get_allowance,
            next_reset_date,
        )

        organization = services.authorize_organization_access(
            request.user, organization_id
        )
        _prune_usage_log(organization.id)
        allowance = get_allowance(organization.id)
        period = current_period_start(allowance.cycle_anchor)
        if allowance.period_start != period:
            allowance.period_start = period
            allowance.period_spent = 0
            allowance.save(
                update_fields=["period_start", "period_spent", "updated_at"]
            )
        used = allowance.period_spent
        imports = _import_log_entries(organization)
        matching = _loose_call_entries(organization)
        return Response(
            {
                "allowance": allowance.monthly_credits,
                "used": used,
                "remaining": max(0, allowance.monthly_credits - used),
                "period_start": period.isoformat(),
                "reset_date": next_reset_date(allowance.cycle_anchor).isoformat(),
                "grand_total": allowance.tokens_spent,
                "paused": allowance.paused,
                "imports": imports[:USAGE_LOG_PAGE_SIZE],
                "imports_count": len(imports),
                "matching": matching[:USAGE_LOG_PAGE_SIZE],
                "matching_count": len(matching),
                "log_page_size": USAGE_LOG_PAGE_SIZE,
                "log_retention_days": USAGE_LOG_RETENTION_DAYS,
            }
        )


class OrganizationReleaseStuckView(APIView):
    """Sweep expired reservations for the caller's own company only.

    A crashed request can leave reservations that block later dispatches
    with "No free in-flight slot". This settles only expired
    reservations for this organization alone; other companies are
    untouched, and live reservations are never cleared — freeing one
    could double-spend when the live call settles. Expired uncertain
    reservations book their held estimate as spend (the provider may
    have processed them); the response reports exactly what moved and
    what is still live.
    """

    def post(self, request, organization_id):
        from pipeline.jobs import record_audit
        from pipeline.models import SpendReservation
        from pipeline.spending import get_allowance, sweep_expired

        try:
            services.require_organization_role(request.user, organization_id)
        except DjangoPermissionDenied as error:
            from rest_framework.exceptions import (
                PermissionDenied as DRFPermissionDenied,
            )

            raise DRFPermissionDenied(str(error)) from error
        # Sweep only this organization's expired reservations. Live held
        # reservations are never touched: clearing one would free budget
        # for a call that may still settle, enabling double spend.
        swept = sweep_expired(organization_id=organization_id)
        allowance = get_allowance(organization_id)
        live = list(
            SpendReservation.objects.filter(
                organization_id=organization_id,
                state__in=[
                    SpendReservation.State.HELD,
                    SpendReservation.State.UNCERTAIN,
                ],
            )
            .order_by("id")
            .values("id", "state", "tokens", "purpose", "expires_at")
        )
        record_audit(
            organization_id,
            request.user,
            "stuck_slots_released",
            "allowance",
            allowance.pk,
            {**swept, "live_reservations": [row["id"] for row in live]},
        )
        return Response(
            {
                "swept": swept,
                "live_reservations": live,
                "in_flight": allowance.in_flight,
                "tokens_reserved": allowance.tokens_reserved,
            }
        )


class OrganizationUsageLogView(APIView):
    """One paginated log section, newest first (member read)."""

    def get(self, request, organization_id):
        import math

        organization = services.authorize_organization_access(
            request.user, organization_id
        )
        _prune_usage_log(organization.id)
        section = request.query_params.get("section", "")
        if section == "imports":
            entries = _import_log_entries(organization)
        elif section == "matching":
            entries = _loose_call_entries(organization)
        else:
            return Response(
                {"detail": 'section must be "imports" or "matching".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            page = int(request.query_params.get("page", "1"))
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
        pages = max(1, math.ceil(len(entries) / USAGE_LOG_PAGE_SIZE))
        start = (page - 1) * USAGE_LOG_PAGE_SIZE
        return Response(
            {
                "section": section,
                "page": page,
                "pages": pages,
                "count": len(entries),
                "page_size": USAGE_LOG_PAGE_SIZE,
                "results": entries[start : start + USAGE_LOG_PAGE_SIZE],
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


PREFERENCE_DEFAULTS = {
    "datetime_format": DateTimeFormat.US,
    "theme_mode": "light",
    "reading_font": "system",
    "reading_size": "md",
    "contrast_mode": "standard",
    "motion_mode": "system",
}


class PreferenceView(APIView):
    """Read or update the caller's display preferences."""

    def get(self, request):
        preference = UserPreference.objects.filter(user=request.user).first()
        if preference is None:
            return Response(dict(PREFERENCE_DEFAULTS))
        return Response(PreferenceSerializer(preference).data)

    def put(self, request):
        serializer = PreferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preference, _created = UserPreference.objects.update_or_create(
            user=request.user,
            defaults=dict(serializer.validated_data),
        )
        return Response(PreferenceSerializer(preference).data)

    def patch(self, request):
        serializer = PreferenceSerializer(
            data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        preference, _created = UserPreference.objects.update_or_create(
            user=request.user,
            defaults=dict(serializer.validated_data),
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
        if "quote_export" in request.data:
            from pipeline.customer_quotes import clean_quote_export

            try:
                template = clean_quote_export(request.data["quote_export"])
            except DjangoValidationError as error:
                raise _as_drf_error(error) from error
            organization.quote_export = template
            updated["quote_export"] = template
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
