"""Date-based exchange rates recorded per file."""

import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.fx import (
    SUPPORTED_CURRENCIES,
    FixtureFxProvider,
    FxNotConfigured,
    get_fx_provider,
)
from pipeline.models import AuditEvent, ImportRun, SourceFile
from pipeline.quotes import build_quote, create_rule
from pipeline.tests.test_parsing import HEADERS, make_workbook
from pipeline.tests.test_preview_flow import complete_row

User = get_user_model()

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FixtureProviderTests(TestCase):
    def test_same_day_is_deterministic(self):
        provider = FixtureFxProvider()
        first = provider.rates_for(date(2026, 10, 29), "EUR")
        second = provider.rates_for(date(2026, 10, 29), "EUR")
        self.assertEqual(first, second)
        self.assertTrue(first["simulated"])
        self.assertIn("synthetic", first["source"])

    def test_rates_change_day_by_day(self):
        provider = FixtureFxProvider()
        monday = provider.rates_for(date(2026, 10, 26), "EUR")["rates"]
        friday = provider.rates_for(date(2026, 10, 30), "EUR")["rates"]
        self.assertNotEqual(monday, friday)

    def test_destination_converts_to_itself(self):
        rates = FixtureFxProvider().rates_for(date(2026, 10, 30), "EUR")["rates"]
        self.assertEqual(rates["EUR"], "1.000000")
        self.assertIn("USD", rates)

    def test_fixture_direction_matches_anchors(self):
        # Units-per-USD anchors say 1 USD ~ 0.92 EUR and
        # 1 CZK ~ 0.92/23.1 EUR; day jitter stays within half a percent,
        # so generous bands still prove the direction (an inversion
        # would read ~1.09 and ~25.1 instead).
        rates = FixtureFxProvider().rates_for(date(2026, 10, 30), "EUR")["rates"]
        usd = Decimal(rates["USD"])
        czk = Decimal(rates["CZK"])
        self.assertLess(usd, Decimal("1"))
        self.assertGreater(usd, Decimal("0.9"))
        self.assertLess(usd, Decimal("0.95"))
        self.assertGreater(czk, Decimal("0.039"))
        self.assertLess(czk, Decimal("0.041"))

    def test_supported_list_is_alphabetical(self):
        codes = list(SUPPORTED_CURRENCIES)
        self.assertEqual(len(codes), 34)
        self.assertEqual(codes, sorted(codes))
        for code in ("USD", "EUR", "GBP", "CZK", "JPY", "XDR"):
            self.assertIn(code, codes)

    def test_fixture_rates_every_supported_currency(self):
        rates = FixtureFxProvider().rates_for(date(2026, 10, 30), "EUR")[
            "rates"
        ]
        for code in SUPPORTED_CURRENCIES:
            self.assertIn(code, rates)

    def test_unknown_provider_refused(self):
        with mock.patch.dict(os.environ, {"OFFERFLOW_FX_PROVIDER": "bogus"}):
            with self.assertRaises(FxNotConfigured):
                get_fx_provider()

    def test_fixture_is_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OFFERFLOW_FX_PROVIDER", None)
            self.assertIsInstance(get_fx_provider(), FixtureFxProvider)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class FileFxTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
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

    def _preview(self, rows, name="sup.xlsx"):
        return self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org.id,
                "file": SimpleUploadedFile(name, make_workbook(rows), CONTENT_TYPE),
            },
            format="multipart",
        )

    def _confirm(self, key, payload):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/pipeline/previews/{key}/confirm/", payload, format="json"
            )

    def _upload(self, rows=None):
        preview = self._preview(rows or [HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {"mapping": {}})
        self.assertEqual(confirm.status_code, 201, confirm.content)
        return ImportRun.objects.get(pk=confirm.data["id"])

    def test_upload_records_upload_day_rates(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        source = SourceFile.objects.get(pk=run.source_file_id)
        self.assertEqual(source.currency_date, timezone.localdate())
        self.assertEqual(source.fx_destination, "EUR")
        self.assertIn("USD", source.fx_rates)
        self.assertIn("synthetic", source.fx_source)
        self.assertTrue(source.fx_simulated)
        self.assertIsNotNone(source.fx_fetched_at)

    def test_offers_keep_supplier_currency(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        offers = self.client.get(f"/api/pipeline/runs/{run.id}/offers/").data
        self.assertEqual(offers[0]["currency"], "USD")
        self.assertEqual(offers[0]["unit_price"], "299.9900")

    def test_quote_fx_exposes_single_pair_inputs(self):
        # The run panel shows exactly one configured pair from this
        # payload: destination, the recorded table, date, and the
        # simulated/source provenance. The recorded USD rate is
        # destination units per dollar (direction, not an example).
        from decimal import Decimal

        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        quote = self.client.get(f"/api/pipeline/runs/{run.id}/quote/").data
        fx = quote["fx"]
        self.assertEqual(fx["destination"], "EUR")
        self.assertEqual(fx["currency_date"], timezone.localdate().isoformat())
        self.assertTrue(fx["simulated"])
        self.assertIn("synthetic", fx["source"])
        rate = Decimal(fx["rates"]["USD"])
        self.assertGreater(rate, Decimal("0.9"))
        self.assertLess(rate, Decimal("0.95"))

    def test_no_destination_leaves_table_empty(self):
        self.org.destination_currency = ""
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        source = SourceFile.objects.get(pk=run.source_file_id)
        self.assertEqual(source.currency_date, timezone.localdate())
        self.assertEqual(source.fx_destination, "")
        self.assertEqual(source.fx_rates, {})
        self.assertIsNone(source.fx_fetched_at)

    def test_change_currency_date_updates_rates(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        before = SourceFile.objects.get(pk=run.source_file_id).fx_rates
        response = self.client.post(
            f"/api/pipeline/files/{run.source_file_id}/fx/",
            {"currency_date": "2026-10-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        source = SourceFile.objects.get(pk=run.source_file_id)
        self.assertEqual(source.currency_date, date(2026, 10, 1))
        self.assertEqual(response.data["currency_date"], "2026-10-01")
        self.assertNotEqual(source.fx_rates, before)
        self.assertEqual(source.fx_destination, "EUR")
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org, action="fx_rates_updated"
            ).exists()
        )

    def test_future_date_rejected(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.client.post(
            f"/api/pipeline/files/{run.source_file_id}/fx/",
            {"currency_date": future},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_bad_date_rejected(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        response = self.client.post(
            f"/api/pipeline/files/{run.source_file_id}/fx/",
            {"currency_date": "not-a-date"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_rates_need_destination_first(self):
        self.org.destination_currency = ""
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        response = self.client.post(
            f"/api/pipeline/files/{run.source_file_id}/fx/",
            {"currency_date": "2026-10-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_change_rates(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        self.client.force_authenticate(self.bob)
        response = self.client.post(
            f"/api/pipeline/files/{run.source_file_id}/fx/",
            {"currency_date": "2026-10-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_other_org_file_is_hidden(self):
        other = Organization.objects.create(name="Other")
        stranger = User.objects.create_user(username="stranger", password="x")
        Membership.objects.create(
            user=stranger, organization=other, role="owner"
        )
        self.client.force_authenticate(stranger)
        preview = self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": other.id,
                "file": SimpleUploadedFile(
                    "s.xlsx", make_workbook([HEADERS, complete_row()]), CONTENT_TYPE
                ),
            },
            format="multipart",
        )
        with self.captureOnCommitCallbacks(execute=True):
            confirm = self.client.post(
                f"/api/pipeline/previews/{preview.data['key']}/confirm/",
                {"mapping": {}},
                format="json",
            )
        file_id = ImportRun.objects.get(pk=confirm.data["id"]).source_file_id
        self.client.force_authenticate(self.alice)
        response = self.client.post(
            f"/api/pipeline/files/{file_id}/fx/",
            {"currency_date": "2026-10-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_destination_setting_round_trip(self):
        response = self.client.get(f"/api/orgs/{self.org.id}/settings/")
        self.assertEqual(response.data["default_currency"], "USD")
        self.assertEqual(response.data["destination_currency"], "USD")
        self.assertEqual(
            response.data["supported_currencies"], list(SUPPORTED_CURRENCIES)
        )
        updated = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"destination_currency": "eur"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["default_currency"], "USD")
        self.assertEqual(updated.data["destination_currency"], "EUR")

    def test_settings_updates_merge_fields(self):
        self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"destination_currency": "EUR"},
            format="json",
        )
        updated = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"default_currency": "USD"},
            format="json",
        )
        self.assertEqual(updated.data["default_currency"], "USD")
        self.assertEqual(updated.data["destination_currency"], "EUR")

    def test_settings_reject_bad_destination(self):
        response = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"destination_currency": "euro"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_quote_converts_with_recorded_rate(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        create_rule(
            organization=self.org,
            user=self.alice,
            data={
                "name": "To EUR",
                "matcher": {},
                "supplier_strategy": "lowest",
                "preferred_supplier": "",
                "ops": [{"op": "convert_to_destination"}],
                "priority": 100,
                "active": True,
            },
        )
        quote = build_quote(ImportRun.objects.get(pk=run.id))
        source = SourceFile.objects.get(pk=run.source_file_id)
        expected = Decimal("299.9900") * Decimal(source.fx_rates["USD"])
        self.assertEqual(len(quote["lines"]), 1)
        self.assertEqual(quote["lines"][0]["currency"], "EUR")
        self.assertEqual(quote["lines"][0]["unit_price"], str(expected))
        self.assertEqual(quote["fx"]["destination"], "EUR")
        self.assertEqual(
            quote["fx"]["currency_date"], timezone.localdate().isoformat()
        )
        self.assertIn("synthetic", quote["fx"]["source"])

    def test_quote_converts_along_hand_recorded_rate(self):
        # Independently directed example: with 1 USD = 0.92 EUR
        # recorded on the file, $299.99 must shrink to ~EUR 276,
        # never grow past the dollar amount.
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        source = SourceFile.objects.get(pk=run.source_file_id)
        source.fx_rates = {"USD": "0.920000"}
        source.save(update_fields=["fx_rates"])
        create_rule(
            organization=self.org,
            user=self.alice,
            data={
                "name": "To EUR",
                "matcher": {},
                "supplier_strategy": "lowest",
                "preferred_supplier": "",
                "ops": [{"op": "convert_to_destination"}],
                "priority": 100,
                "active": True,
            },
        )
        quote = build_quote(ImportRun.objects.get(pk=run.id))
        self.assertEqual(len(quote["lines"]), 1)
        converted = Decimal(quote["lines"][0]["unit_price"])
        self.assertEqual(converted, Decimal("299.9900") * Decimal("0.920000"))
        self.assertLess(converted, Decimal("299.9900"))

    def test_quote_skips_currency_without_recorded_rate(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        row = complete_row()
        row[9] = "XXX"
        run = self._upload([HEADERS, row])
        create_rule(
            organization=self.org,
            user=self.alice,
            data={
                "name": "To EUR",
                "matcher": {},
                "supplier_strategy": "lowest",
                "preferred_supplier": "",
                "ops": [{"op": "convert_to_destination"}],
                "priority": 100,
                "active": True,
            },
        )
        quote = build_quote(ImportRun.objects.get(pk=run.id))
        self.assertEqual(quote["lines"], [])
        self.assertEqual(len(quote["notes"]), 1)
        self.assertIn("XXX->EUR", quote["notes"][0])

    def test_quote_skips_without_destination(self):
        self.org.destination_currency = ""
        self.org.save(update_fields=["destination_currency"])
        run = self._upload()
        create_rule(
            organization=self.org,
            user=self.alice,
            data={
                "name": "To destination",
                "matcher": {},
                "supplier_strategy": "lowest",
                "preferred_supplier": "",
                "ops": [{"op": "convert_to_destination"}],
                "priority": 100,
                "active": True,
            },
        )
        quote = build_quote(ImportRun.objects.get(pk=run.id))
        self.assertEqual(quote["lines"], [])
        self.assertIn("destination currency", quote["notes"][0])
