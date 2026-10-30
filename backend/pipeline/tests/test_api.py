"""Pipeline HTTP tests incl. eager end-to-end upload and cross-tenant 404s."""

import hashlib
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.models import RunStatus
from pipeline.tests.test_parsing import HEADERS, make_workbook

User = get_user_model()

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def complete_row():
    return ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", ""]


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PipelineApiTests(TestCase):
    def setUp(self):
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

    def upload(self, user, org, rows, name="sup.xlsx"):
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

    def test_eager_upload_completes_ready_file(self):
        response = self.upload(self.alice, self.org_a, [HEADERS, complete_row()])
        self.assertEqual(response.status_code, 201, response.content)
        run_id = response.data["id"]
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.READY_FOR_REVIEW)
        self.assertTrue(all(item["passed"] for item in detail.data["validations"]))
        offers = self.client.get(f"/api/pipeline/runs/{run_id}/offers/")
        self.assertEqual(len(offers.data), 1)
        self.assertEqual(offers.data[0]["model_name"], "P1")

    def test_unauthenticated_refused(self):
        response = self.client.get("/api/pipeline/runs/1/")
        self.assertEqual(response.status_code, 403)

    def test_wrong_extension_refused(self):
        response = self.upload(self.alice, self.org_a, [HEADERS, complete_row()], name="sup.pdf")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Accepted formats", response.data["detail"])

    def test_csv_upload_accepted(self):
        response = self.upload(self.alice, self.org_a, [HEADERS, complete_row()], name="sup.csv")
        self.assertEqual(response.status_code, 201, response.content)

    def test_cross_tenant_ids_answer_404(self):
        upload = self.upload(self.alice, self.org_a, [HEADERS, complete_row()])
        run_id = upload.data["id"]
        self.client.force_authenticate(self.alice)
        self.client.post(f"/api/pipeline/runs/{run_id}/approve/")
        export = self.client.post(
            f"/api/pipeline/runs/{run_id}/export/", {"format": "csv"}, format="json"
        )
        artifact_id = export.data["id"]
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get(f"/api/pipeline/runs/{run_id}/").status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/pipeline/runs/{run_id}/offers/").status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/api/pipeline/runs/{run_id}/approve/").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/pipeline/artifacts/{artifact_id}/download/").status_code,
            404,
        )

    def test_full_http_flow_with_reviews(self):
        rows = [
            HEADERS,
            ["Acme", "Pear", "P1", "128GB", "Black", "A/B", "Unlocked", 5, 299.99, "USD", ""],
            ["Acme", "Pear", "P2", "256GB", "White", "A", "Unlocked", 2, "", "USD", ""],
        ]
        upload = self.upload(self.alice, self.org_a, rows)
        run_id = upload.data["id"]
        self.client.force_authenticate(self.alice)
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(detail.data["status"], RunStatus.NEEDS_INPUT)
        reviews = self.client.get(f"/api/pipeline/runs/{run_id}/reviews/")
        self.assertEqual(len(reviews.data), 2)
        by_field = {item["field"]: item for item in reviews.data}
        answer = self.client.post(
            f"/api/pipeline/reviews/{by_field['grade']['id']}/answer/",
            {"choice": "A"},
            format="json",
        )
        self.assertEqual(answer.status_code, 200)
        answer = self.client.post(
            f"/api/pipeline/reviews/{by_field['unit_price']['id']}/answer/",
            {"value": "49.99"},
            format="json",
        )
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.data["run_status"], RunStatus.READY_FOR_REVIEW)
        approve = self.client.post(f"/api/pipeline/runs/{run_id}/approve/")
        self.assertEqual(approve.status_code, 200)
        export = self.client.post(
            f"/api/pipeline/runs/{run_id}/export/", {"format": "csv"}, format="json"
        )
        self.assertEqual(export.status_code, 201)
        download = self.client.get(
            f"/api/pipeline/artifacts/{export.data['id']}/download/"
        )
        self.assertEqual(download.status_code, 200)
        body = b"".join(download.streaming_content)
        self.assertEqual(hashlib.sha256(body).hexdigest(), export.data["sha256"])
        detail = self.client.get(f"/api/pipeline/runs/{run_id}/")
        self.assertEqual(len(detail.data["artifacts"]), 1)
        self.assertEqual(detail.data["artifacts"][0]["format"], "csv")
        second = self.client.post(
            f"/api/pipeline/runs/{run_id}/export/", {"format": "xlsx"}, format="json"
        )
        self.assertEqual(second.status_code, 201)

    def test_approve_and_export_guards(self):
        rows = [HEADERS, ["Acme", "Pear", "P1", "128GB", "Black", "", "Unlocked", 5, 9.99, "USD", ""]]
        upload = self.upload(self.alice, self.org_a, rows)
        run_id = upload.data["id"]
        self.client.force_authenticate(self.alice)
        self.assertEqual(
            self.client.post(f"/api/pipeline/runs/{run_id}/approve/").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                f"/api/pipeline/runs/{run_id}/export/", {"format": "csv"}, format="json"
            ).status_code,
            400,
        )

    def test_cancel_flow(self):
        upload = self.upload(self.alice, self.org_a, [HEADERS, complete_row()])
        run_id = upload.data["id"]
        self.client.force_authenticate(self.alice)
        # Eager processing already finished the run; start a held run instead.
        from pipeline.models import ImportRun

        run = ImportRun.objects.get(pk=run_id)
        run.status = RunStatus.UPLOADED
        run.save(update_fields=["status"])
        cancel = self.client.post(f"/api/pipeline/runs/{run_id}/cancel/")
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.data["status"], RunStatus.CANCELLED)
        again = self.client.post(f"/api/pipeline/runs/{run_id}/cancel/")
        self.assertEqual(again.status_code, 400)
