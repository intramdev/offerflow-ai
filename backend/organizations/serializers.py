"""API shapes for memberships, active organization, invitations, and preferences."""

from rest_framework import serializers

from organizations.models import Membership, OrganizationRole, UserPreference


class MembershipSerializer(serializers.ModelSerializer):
    """A membership as seen by its own user (read-only)."""

    organization_name = serializers.CharField(source="organization.name", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "organization", "organization_name", "role", "joined_at"]
        read_only_fields = fields


class MemberSerializer(serializers.ModelSerializer):
    """One row of an organization's member list (fellow members only)."""

    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "username", "email", "role", "joined_at"]
        read_only_fields = fields


class ActiveOrganizationSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)


class InvitationCreateSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(min_value=1)
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=OrganizationRole.values)


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=64)


class PreferenceSerializer(serializers.ModelSerializer):
    """The caller's display preferences (read and write own row)."""

    class Meta:
        model = UserPreference
        fields = ["datetime_format"]
