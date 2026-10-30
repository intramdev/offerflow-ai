"""Admin registrations for local provisioning (synthetic data only)."""

from django.contrib import admin

from organizations.models import Invitation, Membership, Organization, UserPreference


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
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
    list_display = ("user", "datetime_format")
    list_filter = ("datetime_format",)
    search_fields = ("user__username",)
