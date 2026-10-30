"""Ignored fields and set values in preview, memory, and remap."""

import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.models import ColumnMappingMemory, RunStatus
from pipeline.tests.test_parsing import HEADERS, make_workbook
from pipeline.tests.test_preview_flow import complete_row

User = get_user_model()

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NO_SUPPLIER = ["Product", "Price", "Qty"]
NO_PRICE = ["Supplier", "Product", "Qty"]


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class MappingExtrasTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.alice = User.objects.create_user(username="alice", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.alice, organization=self.org, role="owner"
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

    def _render(self, key, payload):
        return self.client.post(
            f"/api/pipeline/previews/{key}/render/", payload, format="json"
        )

    def _confirm(self, key, payload):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/pipeline/previews/{key}/confirm/", payload, format="json"
            )

    def _offers(self, run_id):
        return self.client.get(f"/api/pipeline/runs/{run_id}/offers/").data

    def test_constant_satisfies_missing_required(self):
        preview = self._preview([NO_SUPPLIER, ["P1", 10, 2]])
        self.assertIn("supplier", preview.data["missing"])
        rendered = self._render(
            preview.data["key"],
            {"mapping": {}, "constants": {"supplier": "Acme"}},
        )
        self.assertEqual(rendered.status_code, 200)
        self.assertTrue(rendered.data["samples"]["complete"])
        self.assertEqual(
            rendered.data["samples"]["rows"][0]["cells"]["supplier"], "Acme"
        )
        self.assertEqual(
            rendered.data["samples"]["sources"]["supplier"], "manual"
        )

    def test_constant_reaches_imported_offers(self):
        preview = self._preview([NO_SUPPLIER, ["P1", 10, 2]])
        confirm = self._confirm(
            preview.data["key"],
            {"mapping": {}, "constants": {"supplier": "Acme"}},
        )
        self.assertEqual(confirm.status_code, 201, confirm.content)
        offers = self._offers(confirm.data["id"])
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["supplier"], "Acme")

    def test_constant_price_must_parse(self):
        preview = self._preview([NO_PRICE, ["Acme", "P1", 2]])
        self.assertIn("price", preview.data["missing"])
        rendered = self._render(
            preview.data["key"],
            {"mapping": {}, "constants": {"price": "not-a-price"}},
        )
        self.assertEqual(rendered.status_code, 400)

    def test_column_and_value_conflict_rejected(self):
        preview = self._preview([HEADERS, complete_row()])
        rendered = self._render(
            preview.data["key"],
            {"mapping": {"supplier": 0}, "constants": {"supplier": "Acme"}},
        )
        self.assertEqual(rendered.status_code, 400)

    def test_ignored_field_dropped_from_detection(self):
        preview = self._preview([HEADERS, complete_row()])
        self.assertIn("grade", preview.data["detected"])
        rendered = self._render(
            preview.data["key"], {"mapping": {}, "ignored": ["grade"]}
        )
        self.assertEqual(rendered.status_code, 200)
        self.assertNotIn("grade", rendered.data["samples"]["detected"])
        self.assertNotIn("grade", rendered.data["samples"]["rows"][0]["cells"])

    def test_ignore_and_map_conflict_rejected(self):
        preview = self._preview([HEADERS, complete_row()])
        rendered = self._render(
            preview.data["key"],
            {"mapping": {"grade": 5}, "ignored": ["grade"]},
        )
        self.assertEqual(rendered.status_code, 400)

    def test_extras_remembered_for_same_layout(self):
        first = self._preview([NO_SUPPLIER, ["P1", 10, 2]])
        confirm = self._confirm(
            first.data["key"],
            {
                "mapping": {},
                "constants": {"supplier": "Acme"},
                "ignored": ["quantity"],
            },
        )
        self.assertEqual(confirm.status_code, 201, confirm.content)
        memory = ColumnMappingMemory.objects.get()
        self.assertEqual(memory.constants, {"supplier": "Acme"})
        self.assertEqual(memory.ignored, ["quantity"])
        second = self._preview([NO_SUPPLIER, ["P2", 20, 1]], name="sup2.xlsx")
        self.assertEqual(
            second.data["proposed_constants"], {"supplier": "Acme"}
        )
        self.assertEqual(second.data["proposed_ignored"], ["quantity"])
        self.assertIsNotNone(second.data["memory"])

    def test_confirm_still_blocks_missing_required(self):
        preview = self._preview([NO_SUPPLIER, ["P1", 10, 2]])
        confirm = self._confirm(preview.data["key"], {"mapping": {}})
        self.assertEqual(confirm.status_code, 400)

    def test_remap_can_replace_column_with_value(self):
        preview = self._preview([HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {"mapping": {}})
        run_id = confirm.data["id"]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/pipeline/runs/{run_id}/remap/",
                {
                    "mapping": {},
                    "constants": {"supplier": "Remapped Co"},
                    "ignored": ["supplier"],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["status"], RunStatus.UPLOADED)
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.READY_FOR_REVIEW)
        offers = self._offers(run_id)
        self.assertEqual(offers[0]["supplier"], "Remapped Co")
