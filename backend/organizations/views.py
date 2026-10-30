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
