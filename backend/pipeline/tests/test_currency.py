"""Three-letter currency codes and the organization default currency."""

import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.fx import SUPPORTED_CURRENCIES
from pipeline.models import ColumnMappingMemory
from pipeline.normalize import parse_currency
from pipeline.tests.test_parsing import HEADERS, make_workbook
from pipeline.tests.test_preview_flow import complete_row

User = get_user_model()

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NO_CURRENCY = ["Product", "Price", "Qty"]


class ParseCurrencyTests(TestCase):
    def test_codes_uppercase(self):
        self.assertEqual(parse_currency("usd"), "USD")
        self.assertEqual(parse_currency("  eur "), "EUR")

    def test_aliases(self):
        self.assertEqual(parse_currency("$"), "USD")
        self.assertEqual(parse_currency("US$"), "USD")
        self.assertEqual(parse_currency("€"), "EUR")
        self.assertEqual(parse_currency("£"), "GBP")
        self.assertEqual(parse_currency("Kč"), "CZK")

    def test_ambiguous_and_invalid_rejected(self):
        self.assertIsNone(parse_currency("¥"))
        self.assertIsNone(parse_currency("US Dollar"))
        self.assertIsNone(parse_currency("EU"))
        self.assertIsNone(parse_currency(""))
        self.assertIsNone(parse_currency(None))
        self.assertIsNone(parse_currency(123))


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DefaultCurrencyTests(TestCase):
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
        payload = dict(payload)
        if "revision" not in payload:
            draft = self.client.get(f"/api/pipeline/previews/{key}/")
            payload["revision"] = draft.data["draft"]["revision"]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/pipeline/previews/{key}/confirm/", payload, format="json"
            )

    def test_set_value_currency_validated_upfront(self):
        preview = self._preview([NO_CURRENCY, ["P1", 10, 2]])
        rendered = self.client.post(
            f"/api/pipeline/previews/{preview.data['key']}/render/",
            {"mapping": {}, "constants": {"currency": "xx"}},
            format="json",
        )
        self.assertEqual(rendered.status_code, 400)

    def test_default_fills_samples(self):
        self.org.default_currency = "EUR"
        self.org.save(update_fields=["default_currency"])
        preview = self._preview([NO_CURRENCY, ["P1", 10, 2]])
        samples = preview.data["samples"]
        self.assertEqual(samples["rows"][0]["cells"]["currency"], "EUR")
        self.assertEqual(samples["sources"]["currency"], "default")
        self.assertEqual(samples["applied_defaults"], {"currency": "EUR"})

    def test_default_reaches_offers(self):
        self.org.default_currency = "EUR"
        self.org.save(update_fields=["default_currency"])
        preview = self._preview([NO_CURRENCY, ["P1", 10, 2]])
        confirm = self._confirm(
            preview.data["key"],
            {"mapping": {}, "constants": {"supplier": "Acme"}},
        )
        self.assertEqual(confirm.status_code, 201, confirm.content)
        offers = self.client.get(
            f"/api/pipeline/runs/{confirm.data['id']}/offers/"
        ).data
        self.assertEqual(offers[0]["currency"], "EUR")

    def test_default_does_not_override_column(self):
        self.org.default_currency = "EUR"
        self.org.save(update_fields=["default_currency"])
        preview = self._preview([HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {"mapping": {}})
        offers = self.client.get(
            f"/api/pipeline/runs/{confirm.data['id']}/offers/"
        ).data
        self.assertEqual(offers[0]["currency"], "USD")

    def test_default_does_not_override_set_value(self):
        self.org.default_currency = "EUR"
        self.org.save(update_fields=["default_currency"])
        preview = self._preview([NO_CURRENCY, ["P1", 10, 2]])
        confirm = self._confirm(
            preview.data["key"],
            {
                "mapping": {},
                "constants": {"supplier": "Acme", "currency": "CZK"},
            },
        )
        offers = self.client.get(
            f"/api/pipeline/runs/{confirm.data['id']}/offers/"
        ).data
        self.assertEqual(offers[0]["currency"], "CZK")

    def test_default_not_stored_in_memory(self):
        self.org.default_currency = "EUR"
        self.org.save(update_fields=["default_currency"])
        preview = self._preview([NO_CURRENCY, ["P1", 10, 2]])
        self._confirm(
            preview.data["key"],
            {"mapping": {}, "constants": {"supplier": "Acme"}},
        )
        memory = ColumnMappingMemory.objects.get()
        self.assertNotIn("currency", memory.constants)

    def test_settings_round_trip(self):
        response = self.client.get(f"/api/orgs/{self.org.id}/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["default_currency"], "USD")
        self.assertEqual(response.data["destination_currency"], "USD")
        self.assertEqual(
            response.data["supported_currencies"], list(SUPPORTED_CURRENCIES)
        )
        updated = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"default_currency": "eur"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["default_currency"], "EUR")
        self.assertEqual(updated.data["destination_currency"], "USD")

    def test_new_organization_defaults_to_usd(self):
        org = Organization.objects.create(name="Fresh")
        self.assertEqual(org.default_currency, "USD")
        self.assertEqual(org.destination_currency, "USD")

    def test_settings_reject_bad_code(self):
        response = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"default_currency": "xx"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_settings_reject_unsupported_code(self):
        response = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"destination_currency": "XXX"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Supported", str(response.data))
        self.org.refresh_from_db()
        self.assertEqual(self.org.destination_currency, "USD")

    def test_settings_blank_still_clears(self):
        updated = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"default_currency": "", "destination_currency": ""},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["default_currency"], "")
        self.assertEqual(updated.data["destination_currency"], "")

    def test_settings_viewer_read_only(self):
        self.client.force_authenticate(self.bob)
        fetched = self.client.get(f"/api/orgs/{self.org.id}/settings/")
        self.assertEqual(fetched.status_code, 200)
        updated = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"default_currency": "EUR"},
            format="json",
        )
        self.assertEqual(updated.status_code, 403)

    def test_settings_other_org_forbidden(self):
        other = Organization.objects.create(name="Other")
        response = self.client.get(f"/api/orgs/{other.id}/settings/")
        self.assertEqual(response.status_code, 403)
