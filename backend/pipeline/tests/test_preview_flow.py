"""Preview-first import, mapping memory, remap/delete, and preferences."""

import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.models import (
    AuditEvent,
    ColumnMappingMemory,
    ImportRun,
    ReviewRequest,
    ReviewStatus,
    RunStatus,
    SourceFile,
)
from pipeline.tests.test_parsing import (
    HEADERS,
    force_fixture_provider,
    make_workbook,
    two_sheet_workbook,
)

User = get_user_model()

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ODD_HEADERS = ["Fornitore", "Product", "Prezzo", "Qty", "Currency"]


def complete_row():
    return ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", ""]


def odd_row():
    return ["Acme", "P1", 10, 2, "EUR"]


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PreviewFlowTests(TestCase):
    def setUp(self):
        force_fixture_provider(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.alice = User.objects.create_user(
            username="alice", email="alice@example.test", password="x"
        )
        self.bob = User.objects.create_user(
            username="bob", email="bob@example.test", password="x"
        )
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(
            user=self.alice, organization=self.org_a, role="owner"
        )
        Membership.objects.create(user=self.bob, organization=self.org_b, role="viewer")
        self.client = APIClient()

    def _preview(self, user, org, rows, name="sup.xlsx"):
        self.client.force_authenticate(user)
        return self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": org.id,
                "file": SimpleUploadedFile(name, make_workbook(rows), CONTENT_TYPE),
            },
            format="multipart",
        )

    def _preview_bytes(self, user, org, content, name="sup.xlsx"):
        self.client.force_authenticate(user)
        return self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": org.id,
                "file": SimpleUploadedFile(name, content, CONTENT_TYPE),
            },
            format="multipart",
        )

    def _revision(self, key):
        draft = self.client.get(f"/api/pipeline/previews/{key}/")
        return draft.data["draft"]["revision"]

    def _confirm(self, key, mapping):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/pipeline/previews/{key}/confirm/",
                {"mapping": mapping, "revision": self._revision(key)},
                format="json",
            )

    def _upload(self, user, org, rows, name="sup.xlsx"):
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/api/pipeline/uploads/",
                {
                    "organization_id": org.id,
                    "file": SimpleUploadedFile(name, make_workbook(rows), CONTENT_TYPE),
                },
                format="multipart",
            )

    def test_resume_restores_file_mapping_and_samples(self):
        key = self._preview(
            self.alice, self.org_a, [HEADERS, complete_row()]
        ).data["key"]
        rendered = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {"supplier": 0, "price": 8}},
            format="json",
        )
        self.assertEqual(rendered.status_code, 200, rendered.content)
        pending = self.client.get(
            f"/api/pipeline/previews/pending/?organization_id={self.org_a.id}"
        )
        self.assertEqual([item["key"] for item in pending.json()["previews"]], [key])
        resumed = self.client.get(f"/api/pipeline/previews/{key}/resume/")
        self.assertEqual(resumed.status_code, 200, resumed.content)
        body = resumed.json()
        # The last rendered effective mapping is restored, detection
        # included — the editor selection comes back intact.
        self.assertEqual(body["proposed"], rendered.json()["mapping"])
        self.assertEqual(body["proposed"]["supplier"], 0)
        self.assertEqual(body["proposed"]["price"], 8)
        self.assertTrue(body["samples"]["rows"])
        self.assertEqual(body["draft"]["revision"], 1)
        self.assertEqual(body["total_rows"], 1)

    def test_preview_proposes_detected_mapping_without_importing(self):
        response = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        self.assertEqual(response.status_code, 201, response.content)
        body = response.data
        self.assertEqual(body["proposed"]["supplier"], 0)
        self.assertEqual(body["proposed"]["model"], 2)
        self.assertEqual(body["proposed"]["price"], 8)
        self.assertEqual(body["missing"], [])
        self.assertIsNone(body["memory"])
        self.assertTrue(body["samples"]["complete"])
        sample = body["samples"]["rows"][0]
        self.assertEqual(sample["normalized"]["model_name"], "P1")
        self.assertEqual(sample["status"], "ok")
        self.assertEqual(sample["issues"], [])
        self.assertEqual(SourceFile.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)

    def test_preview_reuses_remembered_mapping(self):
        first = self._preview(self.alice, self.org_a, [ODD_HEADERS, odd_row()])
        self.assertEqual(first.data["missing"], ["supplier", "price"])
        self.assertIsNone(first.data["memory"])
        confirm = self._confirm(first.data["key"], {"supplier": 0, "price": 2})
        self.assertEqual(confirm.status_code, 201, confirm.content)
        self.assertEqual(ColumnMappingMemory.objects.count(), 1)

        second_row = ["Beta", "P2", 20, 3, "EUR"]
        second = self._preview(self.alice, self.org_a, [ODD_HEADERS, second_row])
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.data["proposed"]["supplier"], 0)
        self.assertEqual(second.data["proposed"]["price"], 2)
        self.assertEqual(second.data["memory"]["use_count"], 1)
        self.assertEqual(second.data["memory"]["supplier_hint"], "Acme")
        self.assertTrue(second.data["samples"]["complete"])
        confirm2 = self._confirm(second.data["key"], second.data["proposed"])
        self.assertEqual(confirm2.status_code, 201, confirm2.content)
        # Each supplier keeps its own profile: the second confirm seeds
        # Beta's row instead of overwriting Acme's.
        hints = set(
            ColumnMappingMemory.objects.values_list("supplier_hint", flat=True)
        )
        self.assertEqual(hints, {"Acme", "Beta"})

    def test_render_adjusts_samples(self):
        preview = self._preview(self.alice, self.org_a, [ODD_HEADERS, odd_row()])
        response = self.client.post(
            f"/api/pipeline/previews/{preview.data['key']}/render/",
            {"mapping": {"supplier": 0}},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["mapping"]["supplier"], 0)
        samples = response.data["samples"]
        self.assertFalse(samples["complete"])
        self.assertEqual(samples["missing"], ["price"])
        self.assertEqual(samples["rows"][0]["cells"]["supplier"], "Acme")

    def test_confirm_requires_complete_mapping(self):
        preview = self._preview(self.alice, self.org_a, [ODD_HEADERS, odd_row()])
        response = self._confirm(preview.data["key"], {"supplier": 0})
        self.assertEqual(response.status_code, 400)
        self.assertIn("price", response.data["detail"])

    def _paste(self, user, org, text):
        self.client.force_authenticate(user)
        return self.client.post(
            "/api/pipeline/previews/from-text/",
            {"organization_id": org.id, "text": text},
            format="json",
        )

    def test_paste_tabular_text_uses_normal_mapping(self):
        text = "Supplier;Model;Price;Qty\nAcme;P1;10;2\n"
        response = self._paste(self.alice, self.org_a, text)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(response.data["message_lines"])
        self.assertEqual(response.data["total_rows"], 1)
        self.assertTrue(response.data["filename"].endswith(".txt"))

    def test_paste_chat_lines_become_message_rows(self):
        text = "iPhone 15 128GB Black 450\nSamsung S24 128GB 390\n"
        response = self._paste(self.alice, self.org_a, text)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.data["message_lines"])
        self.assertEqual(response.data["total_rows"], 2)
        self.assertEqual(response.data["missing"], [])
        rows = response.data["samples"]["rows"]
        self.assertEqual(
            rows[0]["cells"]["message"], "iPhone 15 128GB Black 450"
        )

    def test_paste_blank_text_refused(self):
        response = self._paste(self.alice, self.org_a, "   \n  ")
        self.assertEqual(response.status_code, 400)

    def test_confirm_message_paste_starts_free_text_run(self):
        text = "iPhone 15 128GB Black 450\n"
        key = self._paste(self.alice, self.org_a, text).data["key"]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/pipeline/previews/{key}/confirm/",
                {
                    "mapping": {},
                    "constants": {"supplier": "Acme"},
                    "revision": self._revision(key),
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.content)
        run = ImportRun.objects.get(pk=response.data["id"])
        self.assertTrue(run.mapping.get("free_text"))
        row = run.source_rows.get()
        self.assertEqual(row.data["message"], "iPhone 15 128GB Black 450")
        self.assertEqual(row.data["supplier"], "Acme")

    def test_confirm_message_paste_rejects_column_mapping(self):
        text = "iPhone 15 128GB Black 450\n"
        key = self._paste(self.alice, self.org_a, text).data["key"]
        response = self.client.post(
            f"/api/pipeline/previews/{key}/confirm/",
            {"mapping": {"model": 0}, "revision": self._revision(key)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", response.data["detail"])

    def test_render_message_paste_with_set_values(self):
        text = "iPhone 15 128GB Black x5 450\n"
        key = self._paste(self.alice, self.org_a, text).data["key"]
        response = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {}, "constants": {"currency": "USD"}, "ignored": []},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["mapping"], {})
        rows = response.data["samples"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["cells"]["message"], "iPhone 15 128GB Black x5 450"
        )
        self.assertEqual(rows[0]["cells"]["currency"], "USD")

    def test_second_message_paste_reuses_supplier_memory(self):
        first = self._paste(self.alice, self.org_a, "iPhone 15 128GB 450\n")
        key = first.data["key"]
        with self.captureOnCommitCallbacks(execute=True):
            confirm = self.client.post(
                f"/api/pipeline/previews/{key}/confirm/",
                {
                    "mapping": {},
                    "constants": {"supplier": "Acme"},
                    "revision": self._revision(key),
                },
                format="json",
            )
        self.assertEqual(confirm.status_code, 201, confirm.content)
        # An unrelated paste names no supplier, so no profile applies:
        # the remembered supplier must not leak across suppliers.
        second = self._paste(self.alice, self.org_a, "Galaxy S24 256GB 520\n")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertTrue(second.data["message_lines"])
        self.assertEqual(second.data["proposed"], {})
        self.assertIsNone(second.data["memory"])
        self.assertNotIn("supplier", second.data["proposed_constants"])
        # Once the same supplier is known, its profile returns visibly.
        render = self.client.post(
            f"/api/pipeline/previews/{second.data['key']}/render/",
            {"mapping": {}, "constants": {"supplier": "Acme"}},
            format="json",
        )
        self.assertEqual(render.status_code, 200, render.content)
        self.assertEqual(render.data["memory"]["supplier_hint"], "Acme")
        self.assertIn("Saved profile", render.data["memory"]["reason"])

    def test_duplicate_bytes_refused_on_second_preview(self):
        content = make_workbook([HEADERS, complete_row()])
        preview = self._preview_bytes(self.alice, self.org_a, content)
        confirm = self._confirm(preview.data["key"], {})
        self.assertEqual(confirm.status_code, 201, confirm.content)
        again = self._preview_bytes(self.alice, self.org_a, content)
        self.assertEqual(again.status_code, 400)
        self.assertIn("duplicate", again.data["detail"])

    def test_direct_upload_asks_mapping_questions(self):
        upload = self._upload(self.alice, self.org_a, [ODD_HEADERS, odd_row()])
        self.assertEqual(upload.status_code, 201, upload.content)
        run_id = upload.data["id"]
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.NEEDS_INPUT)
        reviews = ReviewRequest.objects.filter(
            run_id=run_id, status=ReviewStatus.OPEN
        ).order_by("field")
        self.assertEqual(
            [review.field for review in reviews],
            ["mapping:price", "mapping:supplier"],
        )
        self.assertTrue(all(review.offer_id is None for review in reviews))
        self.assertIn("Fornitore (A)", reviews[1].choices)

        with self.captureOnCommitCallbacks(execute=True):
            answer1 = self.client.post(
                f"/api/pipeline/reviews/{reviews[1].id}/answer/",
                {"choice": "Fornitore (A)"},
                format="json",
            )
        self.assertEqual(answer1.status_code, 200, answer1.content)
        with self.captureOnCommitCallbacks(execute=True):
            answer2 = self.client.post(
                f"/api/pipeline/reviews/{reviews[0].id}/answer/",
                {"choice": "Prezzo (C)"},
                format="json",
            )
        self.assertEqual(answer2.status_code, 200, answer2.content)
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.NEEDS_INPUT)
        offers = self.client.get(f"/api/pipeline/runs/{run_id}/offers/")
        self.assertEqual(len(offers.data), 1)
        self.assertEqual(offers.data[0]["supplier"], "Acme")

    def test_ignore_endpoint_drops_record(self):
        from pipeline.models import OfferStatus
        from pipeline.tests.test_flow import MIXED_ROWS

        upload = self._upload(self.alice, self.org_a, MIXED_ROWS)
        run_id = upload.data["id"]
        reviews = self.client.get(f"/api/pipeline/runs/{run_id}/reviews/")
        target = next(
            item for item in reviews.data if item["field"] == "unit_price"
        )
        self.assertIsNotNone(target["offer"])
        response = self.client.post(
            f"/api/pipeline/reviews/{target['id']}/ignore/"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["offer"], target["offer"])
        remaining = self.client.get(f"/api/pipeline/runs/{run_id}/reviews/")
        self.assertEqual(len(remaining.data), 1)
        offer = ReviewRequest.objects.get(pk=target["id"]).offer
        self.assertEqual(offer.status, OfferStatus.EXCLUDED)

    def test_ignore_endpoint_refuses_mapping_questions(self):
        upload = self._upload(self.alice, self.org_a, [ODD_HEADERS, odd_row()])
        run_id = upload.data["id"]
        review = ReviewRequest.objects.get(
            run_id=run_id, field="mapping:supplier"
        )
        response = self.client.post(f"/api/pipeline/reviews/{review.id}/ignore/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Mapping", response.data["detail"])

    def test_mapping_answer_rejects_double_booking(self):
        upload = self._upload(self.alice, self.org_a, [ODD_HEADERS, odd_row()])
        run_id = upload.data["id"]
        review = ReviewRequest.objects.get(run_id=run_id, field="mapping:supplier")
        response = self.client.post(
            f"/api/pipeline/reviews/{review.id}/answer/",
            {"choice": "Product (B)"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already mapped", response.data["detail"])

    def test_remap_reprocesses_same_run(self):
        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {})
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run_id = confirm.data["id"]
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.READY_FOR_REVIEW)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/pipeline/runs/{run_id}/remap/",
                {"mapping": {"supplier": 0, "model": 2, "price": 8}},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["id"], run_id)
        # The request returns at once; the worker reprocesses after commit.
        self.assertEqual(response.data["status"], RunStatus.UPLOADED)
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.READY_FOR_REVIEW)
        memory = ColumnMappingMemory.objects.get()
        self.assertEqual(memory.mapping["price"], 8)

    def test_remap_completed_run_discards_exports(self):
        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {})
        run_id = confirm.data["id"]
        self.assertEqual(
            self.client.post(f"/api/pipeline/runs/{run_id}/approve/").status_code, 200
        )
        export = self.client.post(
            f"/api/pipeline/runs/{run_id}/export/", {"format": "csv"}, format="json"
        )
        self.assertEqual(export.status_code, 201, export.content)
        artifact_id = export.data["id"]
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/pipeline/runs/{run_id}/remap/",
                {
                    "mapping": {},
                    "constants": {"supplier": "Fixed Co"},
                    "ignored": ["supplier"],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["status"], RunStatus.UPLOADED)
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.READY_FOR_REVIEW)
        from pipeline.models import AuditEvent, ExportArtifact

        self.assertEqual(ExportArtifact.objects.count(), 0)
        self.assertEqual(
            self.client.get(
                f"/api/pipeline/artifacts/{artifact_id}/download/"
            ).status_code,
            404,
        )
        offers = self.client.get(f"/api/pipeline/runs/{run_id}/offers/")
        self.assertEqual(offers.data[0]["supplier"], "Fixed Co")
        audit = AuditEvent.objects.get(
            organization=self.org_a, action="run_remapped"
        )
        self.assertEqual(audit.metadata["from_status"], RunStatus.COMPLETED)

    def test_remap_approved_run_restarts_review(self):
        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {})
        run_id = confirm.data["id"]
        self.client.post(f"/api/pipeline/runs/{run_id}/approve/")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/pipeline/runs/{run_id}/remap/",
                {"mapping": {"supplier": 0, "model": 2, "price": 8}},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["status"], RunStatus.UPLOADED)
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.READY_FOR_REVIEW)

    def test_remap_refused_while_processing_or_cancelled(self):
        from pipeline.models import ImportRun

        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        run_id = self._confirm(preview.data["key"], {}).data["id"]
        run = ImportRun.objects.get(pk=run_id)
        run.status = RunStatus.PARSING
        run.save(update_fields=["status"])
        refused = self.client.post(
            f"/api/pipeline/runs/{run_id}/remap/",
            {"mapping": {"supplier": 0, "model": 2, "price": 8}},
            format="json",
        )
        self.assertEqual(refused.status_code, 400)
        run.status = RunStatus.READY_FOR_REVIEW
        run.save(update_fields=["status"])
        self.client.post(f"/api/pipeline/runs/{run_id}/cancel/")
        refused = self.client.post(
            f"/api/pipeline/runs/{run_id}/remap/",
            {"mapping": {"supplier": 0, "model": 2, "price": 8}},
            format="json",
        )
        self.assertEqual(refused.status_code, 400)

    def test_remap_requires_complete_mapping(self):
        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {})
        response = self.client.post(
            f"/api/pipeline/runs/{confirm.data['id']}/remap/",
            {"mapping": {"supplier": 0, "model": 2, "price": 7}},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_remap_or_delete(self):
        Membership.objects.create(
            user=self.bob, organization=self.org_a, role="viewer"
        )
        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        confirm = self._confirm(preview.data["key"], {})
        run_id = confirm.data["id"]
        file_id = SourceFile.objects.get().id
        self.client.force_authenticate(self.bob)
        remap = self.client.post(
            f"/api/pipeline/runs/{run_id}/remap/",
            {"mapping": {"supplier": 0, "model": 2, "price": 8}},
            format="json",
        )
        self.assertEqual(remap.status_code, 403)
        delete = self.client.delete(f"/api/pipeline/files/{file_id}/")
        self.assertEqual(delete.status_code, 403)

    def test_delete_removes_file_run_and_bytes(self):
        preview = self._preview(self.alice, self.org_a, [HEADERS, complete_row()])
        self._confirm(preview.data["key"], {})
        source_file = SourceFile.objects.get()
        import os

        storage_path = f"{self.tmp.name}/{source_file.storage_key}"
        self.assertTrue(os.path.exists(storage_path))
        response = self.client.delete(f"/api/pipeline/files/{source_file.id}/")
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(SourceFile.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)
        self.assertFalse(os.path.exists(f"{self.tmp.name}/{source_file.storage_key}"))
        self.assertTrue(
            AuditEvent.objects.filter(action="upload_deleted").exists()
        )

    def test_files_list_reports_source_currency(self):
        self._confirm(
            self._preview(self.alice, self.org_a, [HEADERS, complete_row()]).data["key"],
            {},
        )
        self.client.force_authenticate(self.alice)
        rows = self.client.get("/api/pipeline/files/")
        self.assertEqual(rows.status_code, 200, rows.content)
        row = rows.data[0]
        self.assertEqual(row["source_currency"], "USD")
        self.assertNotEqual(row["fx_source"], "USD")

    def test_files_list_scoped_to_member_orgs(self):
        self._confirm(
            self._preview(self.alice, self.org_a, [HEADERS, complete_row()]).data["key"],
            {},
        )
        self.client.force_authenticate(self.alice)
        rows = self.client.get("/api/pipeline/files/")
        self.assertEqual(len(rows.data), 1)
        row = rows.data[0]
        self.assertEqual(row["original_name"], "sup.xlsx")
        self.assertEqual(row["run_status"], RunStatus.READY_FOR_REVIEW)
        self.assertEqual(row["uploaded_by_name"], "alice")
        self.assertEqual(row["organization_name"], "Org A")
        filtered = self.client.get(
            f"/api/pipeline/files/?organization_id={self.org_b.id}"
        )
        self.assertEqual(filtered.status_code, 403)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get("/api/pipeline/files/").data, [])

    def test_preferences_default_and_update(self):
        self.client.force_authenticate(self.alice)
        default = self.client.get("/api/orgs/preferences/me/")
        self.assertEqual(
            default.data,
            {
                "datetime_format": "us",
                "theme_mode": "light",
                "reading_font": "system",
                "reading_size": "md",
                "contrast_mode": "standard",
                "motion_mode": "system",
            },
        )
        full = {
            "datetime_format": "eu",
            "theme_mode": "dark",
            "reading_font": "readable",
            "reading_size": "lg",
            "contrast_mode": "high",
            "motion_mode": "reduce",
        }
        update = self.client.put(
            "/api/orgs/preferences/me/", full, format="json"
        )
        self.assertEqual(update.status_code, 200, update.content)
        again = self.client.get("/api/orgs/preferences/me/")
        self.assertEqual(again.data, full)
        partial = self.client.patch(
            "/api/orgs/preferences/me/",
            {"theme_mode": "light"},
            format="json",
        )
        self.assertEqual(partial.status_code, 200, partial.content)
        self.assertEqual(partial.data["theme_mode"], "light")
        self.assertEqual(partial.data["reading_size"], "lg")
        bad = self.client.put(
            "/api/orgs/preferences/me/",
            {**full, "theme_mode": "xx"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PreviewSheetTests(TestCase):
    """Multi-sheet discovery/selection and the message-candidate offer."""

    def setUp(self):
        force_fixture_provider(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.alice = User.objects.create_user(
            username="alice", email="alice@example.test", password="x"
        )
        self.org_a = Organization.objects.create(name="Org A")
        Membership.objects.create(
            user=self.alice, organization=self.org_a, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def _preview_bytes(self, content, name="sup.xlsx"):
        return self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org_a.id,
                "file": SimpleUploadedFile(name, content, CONTENT_TYPE),
            },
            format="multipart",
        )

    def _revision(self, key):
        draft = self.client.get(f"/api/pipeline/previews/{key}/")
        return draft.data["draft"]["revision"]

    def test_preview_reports_selection_and_omission(self):
        response = self._preview_bytes(two_sheet_workbook())
        self.assertEqual(response.status_code, 201, response.content)
        sheets = response.data["sheets"]
        self.assertEqual(sheets["selected"], "Phones")
        self.assertEqual(sheets["omitted"], ["Tablets"])
        self.assertEqual(
            [entry["name"] for entry in sheets["available"]],
            ["Phones", "Tablets"],
        )
        self.assertIn("Tablets", sheets["notice"])
        rows = response.data["samples"]["rows"]
        self.assertEqual(rows[0]["cells"]["supplier"], "PhoneCo")

    def test_render_switches_sheet_and_resets_mapping(self):
        key = self._preview_bytes(two_sheet_workbook()).data["key"]
        response = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {}, "sheet": "Tablets"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("switched", response.data)
        self.assertEqual(response.data["sheets"]["selected"], "Tablets")
        self.assertEqual(response.data["sheets"]["omitted"], ["Phones"])
        rows = response.data["samples"]["rows"]
        self.assertEqual(rows[0]["cells"]["supplier"], "TabCo")

    def test_render_unknown_sheet_refused(self):
        key = self._preview_bytes(two_sheet_workbook()).data["key"]
        response = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {}, "sheet": "Nope"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Phones, Tablets", response.data["detail"])

    def test_confirm_switched_sheet_imports_tablets(self):
        key = self._preview_bytes(two_sheet_workbook()).data["key"]
        switched = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {}, "sheet": "Tablets"},
            format="json",
        )
        self.assertEqual(switched.status_code, 200, switched.content)
        with self.captureOnCommitCallbacks(execute=True):
            confirm = self.client.post(
                f"/api/pipeline/previews/{key}/confirm/",
                {"mapping": {}, "revision": self._revision(key)},
                format="json",
            )
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        self.assertEqual(run.mapping["sheet"], "Tablets")
        self.assertEqual(run.mapping["sheets_omitted"], ["Phones"])
        offers = self.client.get(f"/api/pipeline/runs/{run.id}/offers/")
        self.assertEqual(offers.data[0]["supplier"], "TabCo")
        self.assertEqual(offers.data[0]["model_name"], "T1")

    def test_direct_upload_records_sheet_and_omission(self):
        with self.captureOnCommitCallbacks(execute=True):
            upload = self.client.post(
                "/api/pipeline/uploads/",
                {
                    "organization_id": self.org_a.id,
                    "file": SimpleUploadedFile(
                        "sup.xlsx", two_sheet_workbook(), CONTENT_TYPE
                    ),
                },
                format="multipart",
            )
        self.assertEqual(upload.status_code, 201, upload.content)
        run = ImportRun.objects.get(pk=upload.data["id"])
        self.assertEqual(run.mapping["sheet"], "Phones")
        self.assertEqual(run.mapping["sheets_omitted"], ["Tablets"])
        offers = self.client.get(f"/api/pipeline/runs/{run.id}/offers/")
        self.assertEqual(offers.data[0]["supplier"], "PhoneCo")

    def test_headerless_txt_upload_offers_message_import(self):
        text = "iPhone 15 128GB Black 450\nGalaxy S24 256GB 520\n"
        response = self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org_a.id,
                "file": SimpleUploadedFile("chat.txt", text.encode(), "text/plain"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.data["message_candidate"])
        self.assertEqual(response.data["text"], text)
        self.assertFalse(response.data["truncated"])
        self.assertIn("message", response.data["explanation"])
        paste = self.client.post(
            "/api/pipeline/previews/from-text/",
            {"organization_id": self.org_a.id, "text": response.data["text"]},
            format="json",
        )
        self.assertEqual(paste.status_code, 201, paste.content)
        self.assertTrue(paste.data["message_lines"])

    def test_tabular_csv_without_headers_stays_refusal(self):
        response = self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org_a.id,
                "file": SimpleUploadedFile(
                    "grid.csv", b"a,b,c\n1,2,3\n", "text/csv"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("No header row", response.data["detail"])
