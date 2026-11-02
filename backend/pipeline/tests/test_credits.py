"""Monthly credit accounts, billing cycles, and the usage dashboard."""

import os
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from common.crypto import StoredKeyError, decrypt_key, encrypt_key
from common.provider import ProviderNotConfigured, get_provider
from organizations.models import AIProviderConfig, Membership, Organization
from pipeline.models import (
    ImportRun,
    RuleSetVersion,
    SourceFile,
    UsageEvent,
)
from pipeline.spending import (
    BudgetRefused,
    current_period_start,
    get_allowance,
    get_operator_spend,
    next_reset_date,
    reconcile,
    reserve,
)

User = get_user_model()


class BillingCycleTests(TestCase):
    def test_anchor_day_marks_reset(self):
        anchor = date(2026, 9, 10)
        self.assertEqual(
            current_period_start(anchor, date(2026, 10, 30)), date(2026, 10, 10)
        )
        self.assertEqual(
            next_reset_date(anchor, date(2026, 10, 30)), date(2026, 11, 10)
        )

    def test_early_month_stays_in_previous_cycle(self):
        anchor = date(2026, 9, 10)
        self.assertEqual(
            current_period_start(anchor, date(2026, 10, 5)), date(2026, 9, 10)
        )
        self.assertEqual(
            next_reset_date(anchor, date(2026, 10, 5)), date(2026, 10, 10)
        )

    def test_short_months_clamp(self):
        anchor = date(2026, 1, 31)
        self.assertEqual(
            current_period_start(anchor, date(2026, 2, 15)), date(2026, 1, 31)
        )
        self.assertEqual(
            next_reset_date(anchor, date(2026, 2, 15)), date(2026, 2, 28)
        )
        self.assertEqual(
            current_period_start(anchor, date(2026, 3, 5)), date(2026, 2, 28)
        )
        self.assertEqual(
            next_reset_date(anchor, date(2026, 3, 5)), date(2026, 3, 31)
        )

    def test_year_boundary(self):
        anchor = date(2026, 12, 10)
        self.assertEqual(
            current_period_start(anchor, date(2027, 1, 5)), date(2026, 12, 10)
        )
        self.assertEqual(
            next_reset_date(anchor, date(2027, 1, 5)), date(2027, 1, 10)
        )


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MonthlyCreditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
        operator = get_operator_spend()
        operator.monthly_ceiling = 100000
        operator.save(update_fields=["monthly_ceiling"])

    def test_new_account_activates_today(self):
        allowance = get_allowance(self.org.id)
        self.assertEqual(allowance.cycle_anchor, timezone.localdate())
        self.assertEqual(allowance.monthly_credits, 0)

    def test_new_account_starts_at_zero_pending_approval(self):
        AIProviderConfig.objects.create(default_monthly_credits=5000)
        allowance = get_allowance(self.org.id)
        self.assertEqual(allowance.monthly_credits, 0)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)
        allowance.monthly_credits = 5000
        allowance.save(update_fields=["monthly_credits"])
        reservation = reserve(self.org.id, 100, 1)
        self.assertEqual(reservation.estimate_tokens, 100)

    def test_fresh_config_ships_with_sane_defaults(self):
        config = AIProviderConfig.objects.create()
        self.assertEqual(config.model, "muse-spark-1.3-contributor")
        self.assertEqual(config.default_monthly_credits, 25_000_000)
        allowance = get_allowance(self.org.id)
        self.assertEqual(allowance.monthly_credits, 0)

    def test_lazy_reset_on_new_cycle(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 100000
        allowance.token_budget = 100000
        allowance.cycle_anchor = date(2026, 1, 10)
        allowance.period_start = date(2026, 1, 10)
        allowance.period_spent = 90000
        allowance.tokens_spent = 90000
        allowance.save()
        reserve(self.org.id, 100, 1)
        allowance.refresh_from_db()
        self.assertEqual(
            allowance.period_start, current_period_start(date(2026, 1, 10))
        )
        self.assertEqual(allowance.period_spent, 0)
        self.assertEqual(allowance.tokens_spent, 90000)

    def test_monthly_exhaustion_refuses(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 100
        allowance.token_budget = 100000
        allowance.save()
        with self.assertRaisesRegex(BudgetRefused, "Monthly credits exhausted"):
            reserve(self.org.id, 101, 1)

    def test_zero_monthly_refuses_despite_lifetime(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 0
        allowance.token_budget = 100000
        allowance.save()
        with self.assertRaisesRegex(BudgetRefused, "No monthly credits"):
            reserve(self.org.id, 100, 1)

    def test_lifetime_cap_still_enforced(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 100000
        allowance.token_budget = 50
        allowance.save()
        with self.assertRaisesRegex(BudgetRefused, "headroom"):
            reserve(self.org.id, 100, 1)

    def test_reconcile_updates_period_and_grand_total(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 100000
        allowance.token_budget = 100000
        allowance.save()
        reservation = reserve(self.org.id, 100, 1)
        updated = reconcile(reservation, 60)
        self.assertEqual(updated.period_spent, 60)
        self.assertEqual(updated.tokens_spent, 60)


class StoredKeyTests(TestCase):
    def test_round_trip(self):
        self.assertEqual(decrypt_key(encrypt_key("LLM_9_secret")), "LLM_9_secret")

    def test_empty_stays_empty(self):
        self.assertEqual(encrypt_key(""), "")
        with self.assertRaises(StoredKeyError):
            decrypt_key("")

    def test_tampered_ciphertext_fails_closed(self):
        token = encrypt_key("LLM_9_secret")
        tampered = ("A" if token[0] != "A" else "B") + token[1:]
        with self.assertRaises(StoredKeyError):
            decrypt_key(tampered)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProviderCredentialTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")

    def _env(self, **overrides):
        values = {
            "OFFERFLOW_PROVIDER": "meta",
            "OFFERFLOW_ALLOW_PAID": "true",
            "OFFERFLOW_META_API_KEY": "LLM_9_envkey",
            "OFFERFLOW_META_MODEL": "env-model",
        }
        values.update(overrides)
        return mock.patch.dict(os.environ, values, clear=False)

    def test_admin_config_wins_over_env(self):
        AIProviderConfig.objects.create(
            api_key_encrypted=encrypt_key("LLM_9_adminkey"), model="admin-model"
        )
        with self._env():
            provider = get_provider(organization_id=self.org.id)
        self.assertEqual(provider._config.model, "admin-model")
        self.assertEqual(provider._config.api_key, "LLM_9_adminkey")

    def test_disabled_admin_refuses_despite_env(self):
        AIProviderConfig.objects.create(
            enabled=False,
            api_key_encrypted=encrypt_key("LLM_9_adminkey"),
            model="admin-model",
        )
        with self._env(), self.assertRaisesRegex(
            ProviderNotConfigured, "disabled in admin"
        ):
            get_provider(organization_id=self.org.id)

    def test_incomplete_admin_falls_back_to_env(self):
        AIProviderConfig.objects.create(model="admin-model")
        with self._env():
            provider = get_provider(organization_id=self.org.id)
        self.assertEqual(provider._config.model, "env-model")

    def test_no_admin_row_uses_env(self):
        with self._env():
            provider = get_provider(organization_id=self.org.id)
        self.assertEqual(provider._config.model, "env-model")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UsageEndpointTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.alice, organization=self.org, role="owner"
        )
        Membership.objects.create(
            user=self.bob, organization=self.org, role="viewer"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def _run_with_call(self, name="sup.xlsx", tokens=952):
        source = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.alice,
            original_name=name,
            size_bytes=10,
            sha256="ab",
            storage_key="k",
        )
        ruleset = RuleSetVersion.objects.get(version="v1")
        run = ImportRun.objects.create(
            organization=self.org, source_file=source, rule_version=ruleset
        )
        UsageEvent.objects.create(
            organization=self.org,
            run=run,
            kind="provider_call",
            quantity=tokens,
            detail={
                "prompt_tokens": 291,
                "completion_tokens": 661,
                "model": "muse-spark",
            },
        )
        return run

    def test_shape_and_import_log(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 25000
        allowance.save(update_fields=["monthly_credits"])
        run = self._run_with_call()
        response = self.client.get(f"/api/orgs/{self.org.id}/usage/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data
        self.assertEqual(data["allowance"], 25000)
        self.assertEqual(data["grand_total"], 0)
        self.assertIn("reset_date", data)
        self.assertEqual(len(data["imports"]), 1)
        entry = data["imports"][0]
        self.assertEqual(entry["run_id"], run.id)
        self.assertEqual(entry["filename"], "sup.xlsx")
        self.assertEqual(entry["requests"], 1)
        self.assertEqual(entry["tokens"], 952)
        self.assertEqual(entry["calls"][0]["prompt_tokens"], 291)
        self.assertEqual(data["matching"], [])

    def test_match_spend_listed_separately(self):
        from pipeline.models import UsageEvent

        UsageEvent.objects.create(
            organization=self.org,
            run=None,
            kind="provider_call",
            quantity=40,
            detail={
                "prompt_tokens": 30,
                "completion_tokens": 10,
                "model": "muse-spark",
                "task": "match",
                "block": "apple 128GB",
            },
        )
        response = self.client.get(f"/api/orgs/{self.org.id}/usage/")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.data
        self.assertEqual(len(data["imports"]), 0)
        self.assertEqual(len(data["matching"]), 1)
        self.assertEqual(data["matching"][0]["total"], 40)
        self.assertEqual(data["matching"][0]["label"], "apple 128GB")

    def test_read_snaps_new_period(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 25000
        allowance.cycle_anchor = date(2026, 1, 10)
        allowance.period_start = date(2026, 1, 10)
        allowance.period_spent = 900
        allowance.tokens_spent = 900
        allowance.save()
        response = self.client.get(f"/api/orgs/{self.org.id}/usage/")
        self.assertEqual(response.data["used"], 0)
        self.assertEqual(response.data["grand_total"], 900)

    def test_viewer_can_read_other_org_cannot(self):
        self.client.force_authenticate(self.bob)
        response = self.client.get(f"/api/orgs/{self.org.id}/usage/")
        self.assertEqual(response.status_code, 200)
        other = Organization.objects.create(name="Other")
        response = self.client.get(f"/api/orgs/{other.id}/usage/")
        self.assertEqual(response.status_code, 403)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminCreditActionTests(TestCase):
    """Superusers adjust allowances and reset usage from the changelist."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="root", password="x", email="root@example.com"
        )
        self.org = Organization.objects.create(name="Org")
        self.allowance = get_allowance(self.org.id)
        self.client = Client()
        self.client.force_login(self.admin)
        self.changelist = "/admin/pipeline/spendallowance/"

    def _selected(self):
        return {"action": "", "_selected_action": [str(self.allowance.pk)]}

    def test_increase_monthly_credits(self):
        self.allowance.monthly_credits = 0
        self.allowance.save(update_fields=["monthly_credits"])
        payload = self._selected()
        payload["action"] = "adjust_credits"
        response = self.client.post(payload["action"] and self.changelist, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "amount")
        payload.update({"apply": "Adjust credits", "amount": "5000"})
        response = self.client.post(self.changelist, payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.allowance.refresh_from_db()
        self.assertEqual(self.allowance.monthly_credits, 5000)

    def test_decrease_floors_at_zero(self):
        self.allowance.monthly_credits = 100
        self.allowance.save(update_fields=["monthly_credits"])
        payload = self._selected()
        payload.update(
            {"action": "adjust_credits", "apply": "Adjust credits", "amount": "-5000"}
        )
        self.client.post(self.changelist, payload, follow=True)
        self.allowance.refresh_from_db()
        self.assertEqual(self.allowance.monthly_credits, 0)

    def test_reset_period_usage_keeps_grand_total(self):
        self.allowance.period_spent = 900
        self.allowance.tokens_spent = 900
        self.allowance.save(update_fields=["period_spent", "tokens_spent"])
        payload = self._selected()
        payload["action"] = "reset_period_usage"
        self.client.post(self.changelist, payload, follow=True)
        self.allowance.refresh_from_db()
        self.assertEqual(self.allowance.period_spent, 0)
        self.assertEqual(self.allowance.tokens_spent, 900)


class UsageLogPaginationTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.alice, organization=self.org, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.url = f"/api/orgs/{self.org.id}/usage/log/"
        operator = get_operator_spend()
        operator.monthly_ceiling = 100000
        operator.save(update_fields=["monthly_ceiling"])

    def _loose_call(self, label, tokens=10):
        return UsageEvent.objects.create(
            organization=self.org,
            run=None,
            kind="provider_call",
            quantity=tokens,
            detail={"task": "match", "block": label, "model": "m"},
        )

    def _run_with_call(self, name):
        source = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.alice,
            original_name=name,
            size_bytes=10,
            sha256=name,
            storage_key=name,
        )
        run = ImportRun.objects.create(
            organization=self.org,
            source_file=source,
            rule_version=RuleSetVersion.objects.get(version="v1"),
        )
        UsageEvent.objects.create(
            organization=self.org,
            run=run,
            kind="provider_call",
            quantity=5,
            detail={"model": "m"},
        )
        return run

    def test_matching_paginates_fifty_per_page_newest_first(self):
        for index in range(55):
            self._loose_call(f"block-{index:02d}")
        first = self.client.get(self.url, {"section": "matching", "page": 1})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 55)
        self.assertEqual(first.data["pages"], 2)
        self.assertEqual(first.data["page_size"], 50)
        self.assertEqual(len(first.data["results"]), 50)
        self.assertEqual(first.data["results"][0]["label"], "block-54")
        second = self.client.get(self.url, {"section": "matching", "page": 2})
        self.assertEqual(len(second.data["results"]), 5)
        self.assertEqual(second.data["results"][-1]["label"], "block-00")

    def test_imports_paginate_by_run(self):
        for index in range(51):
            self._run_with_call(f"f{index:02d}.xlsx")
        first = self.client.get(self.url, {"section": "imports", "page": 1})
        self.assertEqual(first.data["count"], 51)
        self.assertEqual(len(first.data["results"]), 50)
        second = self.client.get(self.url, {"section": "imports", "page": 2})
        self.assertEqual(len(second.data["results"]), 1)

    def test_invalid_section_and_page_rejected(self):
        bad = self.client.get(self.url, {"section": "nope"})
        self.assertEqual(bad.status_code, 400)
        bad = self.client.get(self.url, {"section": "matching", "page": 0})
        self.assertEqual(bad.status_code, 400)
        bad = self.client.get(self.url, {"section": "matching", "page": "x"})
        self.assertEqual(bad.status_code, 400)

    def test_release_stuck_sweeps_only_own_expired(self):
        from datetime import timedelta

        from pipeline.models import AuditEvent, SpendReservation

        other = Organization.objects.create(name="Other")
        for org_id in (self.org.id, other.id):
            allowance = get_allowance(org_id)
            allowance.monthly_credits = 100000
            allowance.save(update_fields=["monthly_credits"])
        live = reserve(self.org.id, 100, 2)
        dead = reserve(self.org.id, 200, 2)
        SpendReservation.objects.filter(pk=dead.id).update(
            expires_at=timezone.now() - timedelta(seconds=10)
        )
        foreign = reserve(other.id, 300, 2)
        SpendReservation.objects.filter(pk=foreign.id).update(
            expires_at=timezone.now() - timedelta(seconds=10)
        )
        response = self.client.post(
            f"/api/orgs/{self.org.id}/spending/release-stuck/"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["swept"]["uncertain"], 1)
        self.assertEqual(response.data["swept"]["expired"], 0)
        # The expired hold converts to uncertain (still counted); the live
        # hold is untouched, so no budget frees for double spend.
        live_ids = {row["id"] for row in response.data["live_reservations"]}
        self.assertEqual(live_ids, {live.id, dead.id})
        mine = get_allowance(self.org.id)
        self.assertEqual(mine.in_flight, 2)
        self.assertEqual(mine.tokens_reserved, 300)
        self.assertEqual(mine.tokens_spent, 0)
        foreign_row = SpendReservation.objects.get(pk=foreign.id)
        self.assertEqual(foreign_row.state, SpendReservation.State.HELD)
        theirs = get_allowance(other.id)
        self.assertEqual(theirs.in_flight, 1)
        self.assertEqual(theirs.tokens_reserved, 300)
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org, action="stuck_slots_released"
            ).exists()
        )
        stranger = self.client.post(
            f"/api/orgs/{other.id}/spending/release-stuck/"
        )
        self.assertEqual(stranger.status_code, 403)

    def test_log_pruned_after_retention_window(self):
        from datetime import timedelta

        old = self._loose_call("ancient")
        UsageEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=121)
        )
        kept = self._loose_call("fresh")
        UsageEvent.objects.filter(pk=kept.pk).update(
            created_at=timezone.now() - timedelta(days=119)
        )
        response = self.client.get(self.url, {"section": "matching"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["label"], "fresh")
        self.assertFalse(UsageEvent.objects.filter(pk=old.pk).exists())
        summary = self.client.get(f"/api/orgs/{self.org.id}/usage/")
        self.assertEqual(summary.data["matching_count"], 1)
        self.assertEqual(summary.data["log_retention_days"], 120)
        self.assertEqual(summary.data["log_page_size"], 50)
