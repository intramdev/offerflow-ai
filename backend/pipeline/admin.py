"""Admin registrations: run visibility plus the budget kill-switch handle."""

from django.contrib import admin

from pipeline.models import ImportRun, SpendAllowance


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "status", "simulated", "created_at")
    list_filter = ("status", "simulated")
    readonly_fields = (
        "organization",
        "source_file",
        "rule_version",
        "status",
        "provider_name",
        "simulated",
        "error",
    )


@admin.register(SpendAllowance)
class SpendAllowanceAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "token_budget",
        "tokens_reserved",
        "tokens_spent",
        "in_flight",
        "paused",
        "updated_at",
    )
    list_filter = ("paused",)
    search_fields = ("organization__name",)
