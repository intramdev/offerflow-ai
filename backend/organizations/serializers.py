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


def _clean_currency_code(value) -> str:
    from pipeline.fx import SUPPORTED_CURRENCIES
    from pipeline.normalize import parse_currency

    text = (value or "").strip()
    if not text:
        return ""
    code = parse_currency(text)
    if code is None:
        raise serializers.ValidationError(
            "Enter a three-letter currency code such as USD, EUR, or CZK."
        )
    if code not in SUPPORTED_CURRENCIES:
        raise serializers.ValidationError(
            "Unsupported currency code. "
            f"Supported: {', '.join(SUPPORTED_CURRENCIES)}."
        )
    return code


class OrganizationSettingsSerializer(serializers.Serializer):
    """Shared organization settings (member read, operating write)."""

    default_currency = serializers.CharField(
        max_length=3, allow_blank=True, required=False, default=""
    )
    destination_currency = serializers.CharField(
        max_length=3, allow_blank=True, required=False, default=""
    )
    supported_currencies = serializers.SerializerMethodField()

    def get_supported_currencies(self, _obj) -> list:
        from pipeline.fx import SUPPORTED_CURRENCIES

        return list(SUPPORTED_CURRENCIES)

    def validate_default_currency(self, value):
        return _clean_currency_code(value)

    def validate_destination_currency(self, value):
        return _clean_currency_code(value)
