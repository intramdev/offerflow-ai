"""Admin registrations: run visibility plus the budget kill-switch handle."""

from django import forms
from django.contrib import admin
from django.template.response import TemplateResponse

from pipeline.models import (
    AssistantFlag,
    Category,
    CustomerQuote,
    ImportRun,
    ProductAlias,
    SpendAllowance,
    Supplier,
    SupplierNote,
)


class CreditAdjustForm(forms.Form):
    amount = forms.IntegerField(
        label="Credits to add",
        help_text="Positive adds credits, negative removes them. "
        "Allowances never go below zero.",
    )


@admin.register(AssistantFlag)
class AssistantFlagAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "organization",
        "username",
        "reason",
        "short_question",
    )
    list_filter = ("reason", "organization")
    search_fields = ("username", "question")
    readonly_fields = (
        "organization",
        "user",
        "username",
        "question",
        "reason",
        "created_at",
    )
    actions = ("ban_from_assistant",)

    @admin.display(description="Question")
    def short_question(self, flag):
        return flag.question[:80]

    @admin.action(description="Ban these users from the assistant")
    def ban_from_assistant(self, request, queryset):
        from organizations.models import UserPreference

        banned = 0
        for flag in queryset.exclude(user__isnull=True).select_related("user"):
            UserPreference.objects.update_or_create(
                user=flag.user, defaults={"assistant_banned": True}
            )
            banned += 1
        self.message_user(request, f"Banned {banned} users from the assistant.")

    def has_add_permission(self, request):
        return False


@admin.register(CustomerQuote)
class CustomerQuoteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "status",
        "simulated",
        "created_at",
    )
    list_filter = ("status", "simulated")
    readonly_fields = (
        "organization",
        "status",
        "source_run_ids",
        "groups",
        "lines",
        "notes",
        "message",
        "provider_name",
        "simulated",
        "created_by",
        "created_at",
    )


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("organization", "name", "created_at")
    list_filter = ("organization",)
    search_fields = ("name",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "name",
        "favorite_rank",
        "location",
        "customs_required",
        "created_at",
    )
    list_filter = ("organization", "customs_required")
    search_fields = ("name", "location")


@admin.register(SupplierNote)
class SupplierNoteAdmin(admin.ModelAdmin):
    list_display = ("supplier", "author", "created_at")
    list_filter = ("supplier__organization",)
    search_fields = ("text",)


@admin.register(ProductAlias)
class ProductAliasAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "supplier_norm",
        "alias_norm",
        "canonical_model",
        "use_count",
        "last_used_at",
    )
    list_filter = ("organization",)
    search_fields = ("alias_norm", "canonical_model", "supplier_norm")
    readonly_fields = ("use_count", "last_used_at", "created_at")


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
        "monthly_credits",
        "period_usage",
        "next_reset",
        "grand_total",
        "token_budget",
        "tokens_reserved",
        "in_flight",
        "paused",
        "updated_at",
    )
    list_filter = ("paused",)
    search_fields = ("organization__name",)
    list_editable = ("monthly_credits",)
    actions = ("adjust_credits", "reset_period_usage")
    readonly_fields = (
        "tokens_reserved",
        "tokens_spent",
        "period_spent",
        "in_flight",
        "updated_at",
    )

    @admin.action(description="Increase or decrease monthly credits…")
    def adjust_credits(self, request, queryset):
        if "apply" in request.POST:
            form = CreditAdjustForm(request.POST)
            if form.is_valid():
                amount = form.cleaned_data["amount"]
                for allowance in queryset:
                    allowance.monthly_credits = max(
                        0, allowance.monthly_credits + amount
                    )
                    allowance.save(update_fields=["monthly_credits", "updated_at"])
                self.message_user(
                    request,
                    f"Adjusted monthly credits by {amount:+d} "
                    f"for {queryset.count()} companies.",
                )
                return None
        else:
            form = CreditAdjustForm()
        return TemplateResponse(
            request,
            "admin/pipeline/spendallowance/adjust_credits.html",
            {
                **self.admin_site.each_context(request),
                "title": "Adjust monthly credits",
                "queryset": queryset,
                "form": form,
                "opts": self.model._meta,
            },
        )

    @admin.action(description="Reset current-period usage to zero")
    def reset_period_usage(self, request, queryset):
        from pipeline.spending import current_period_start

        for allowance in queryset:
            allowance.period_start = current_period_start(allowance.cycle_anchor)
            allowance.period_spent = 0
            allowance.save(update_fields=["period_start", "period_spent", "updated_at"])
        self.message_user(
            request,
            f"Reset period usage for {queryset.count()} companies. "
            "Grand totals unchanged.",
        )

    @admin.display(description="Used this period")
    def period_usage(self, obj):
        from pipeline.spending import current_period_start

        if obj.period_start != current_period_start(obj.cycle_anchor):
            return "0 (new period)"
        return str(obj.period_spent)

    @admin.display(description="Resets")
    def next_reset(self, obj):
        from pipeline.spending import next_reset_date

        return next_reset_date(obj.cycle_anchor)

    @admin.display(description="Grand total")
    def grand_total(self, obj):
        return obj.tokens_spent
