"""Admin registrations for local provisioning (synthetic data only)."""

from django import forms
from django.contrib import admin

from organizations.models import (
    AIProviderConfig,
    Invitation,
    Membership,
    Organization,
    UserPreference,
)


class AIProviderConfigForm(forms.ModelForm):
    """Key entry that never renders the stored secret back."""

    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the stored key. Must start with LLM_.",
    )

    class Meta:
        model = AIProviderConfig
        fields = ("enabled", "api_key", "model", "default_monthly_credits")

    def clean_api_key(self):
        key = (self.cleaned_data.get("api_key") or "").strip()
        if key and not key.startswith("LLM_"):
            raise forms.ValidationError("Meta API keys start with LLM_.")
        return key

    def save(self, commit=True):
        from common.crypto import encrypt_key

        instance = super().save(commit=False)
        key = (self.cleaned_data.get("api_key") or "").strip()
        if key:
            instance.api_key_encrypted = encrypt_key(key)
        if commit:
            instance.save()
        return instance


@admin.register(AIProviderConfig)
class AIProviderConfigAdmin(admin.ModelAdmin):
    form = AIProviderConfigForm
    list_display = ("__str__", "enabled", "has_key", "model", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return AIProviderConfig.objects.count() == 0

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "default_currency",
        "destination_currency",
        "created_at",
    )
    search_fields = ("name",)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "joined_at")
    list_filter = ("role",)
    search_fields = ("user__username", "organization__name")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "role", "accepted_at", "expires_at")
    list_filter = ("role",)
    search_fields = ("email", "organization__name")


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "datetime_format", "assistant_banned")
    list_filter = ("datetime_format", "assistant_banned")
    search_fields = ("user__username",)
