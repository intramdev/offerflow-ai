"""End-to-end pipeline flow on synthetic workbooks with fixed expectations."""

import hashlib
import io
import tempfile
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from openpyxl import load_workbook

from common.storage import LocalFileAdapter
from organizations.models import Membership, Organization
from pipeline.export import create_export, normalized_digest
from pipeline.jobs import cancel_run, process_import_run
from pipeline.models import OfferStatus, ReviewStatus, RunStatus
from pipeline.review import answer_request, approve_run
from pipeline.tests.test_parsing import HEADERS, make_workbook
from pipeline.uploads import create_upload

User = get_user_model()


def row(
    supplier="Acme",
    brand="Pear",
    model="P1",
    capacity="128GB",
    color="Black",
    grade="A",
    lock="Unlocked",
    qty=5,
    price=299.99,
    currency="USD",
    notes="",
):
    return [
        supplier, brand, model, capacity, color, grade, lock, qty, price, currency, notes,
    ]


MIXED_ROWS = [
    HEADERS,
    row(),
    row(grade="A/B"),
    row(grade="B", lock="iCloud locked"),
    row(price=""),
]


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PipelineFlowTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.user = User.objects.create_user(
            username="u", email="u@example.test", password="x"
        )
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")

    def upload_and_process(self, rows, name="sup.xlsx"):
        uploaded = SimpleUploadedFile(
            name,
            make_workbook(rows),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        # on_commit does not fire inside TestCase; drive the task directly.
        self.assertEqual(process_import_run(run.id), "ok")
        run.refresh_from_db()
        return run

    def answer_all(self, run, answers):
        for review in run.reviews.filter(status=ReviewStatus.OPEN).order_by("id"):
            answer = answers[review.field]
            if isinstance(answer, tuple):
                answer_request(review=review, user=self.user, choice=answer[1])
            else:
                answer_request(review=review, user=self.user, value=answer)

    def test_mixed_file_needs_input_then_completes(self):
        run = self.upload_and_process(MIXED_ROWS)
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        self.assertTrue(run.simulated)
        self.assertEqual(run.provider_name, "fixture")
        self.assertEqual(run.source_rows.count(), 4)
        self.assertEqual(run.offers.filter(status=OfferStatus.OK).count(), 1)
        self.assertEqual(run.offers.filter(status=OfferStatus.EXCLUDED).count(), 1)
        open_reviews = list(run.reviews.filter(status=ReviewStatus.OPEN).order_by("id"))
        self.assertEqual(len(open_reviews), 2)
        by_field = {review.field: review for review in open_reviews}
        self.assertEqual(set(by_field), {"grade", "unit_price"})
        self.assertEqual(by_field["grade"].kind, "choice")
        self.assertEqual(by_field["grade"].choices, ["A", "B", "A/B"])

        answer_request(review=by_field["grade"], user=self.user, choice="A")
        answer_request(review=by_field["unit_price"], user=self.user, value="99.99")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertTrue(all(item.passed for item in run.validations.all()))

        approve_run(run=run, user=self.user)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.APPROVED)
        self.assertEqual(run.approved_normalized_digest, normalized_digest(run))

        xlsx = create_export(run=run, user=self.user, format="xlsx")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)
        csv_artifact = create_export(run=run, user=self.user, format="csv")

        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        xlsx_bytes = adapter.read_bytes(xlsx.storage_key)
        self.assertEqual(hashlib.sha256(xlsx_bytes).hexdigest(), xlsx.sha256)
        workbook = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
        data_rows = list(workbook["offers"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(data_rows), 3)
        self.assertEqual(data_rows[0][2], "P1")
        self.assertEqual(data_rows[0][11], "Sheet1")
        self.assertEqual(data_rows[0][12], 2)
        excluded_rows = list(workbook["excluded"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(excluded_rows), 1)
        meta = {
            cells[0]: cells[1]
            for cells in workbook["meta"].iter_rows(min_row=2, values_only=True)
        }
        self.assertEqual(meta["normalized_digest"], xlsx.normalized_digest)

        csv_bytes = adapter.read_bytes(csv_artifact.storage_key)
        lines = csv_bytes.decode("utf-8-sig").strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("supplier,brand,model"))

    def test_stale_approval_refuses_export(self):
        run = self.upload_and_process([HEADERS, row()])
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        approve_run(run=run, user=self.user)
        offer = run.offers.get()
        offer.unit_price = Decimal("1.00")
        offer.save(update_fields=["unit_price"])
        with self.assertRaises(ValidationError):
            create_export(run=run, user=self.user, format="csv")

    def test_formula_injection_sanitized_numbers_numeric(self):
        run = self.upload_and_process([HEADERS, row(notes="=1+1")])
        approve_run(run=run, user=self.user)
        xlsx = create_export(run=run, user=self.user, format="xlsx")
        csv_artifact = create_export(run=run, user=self.user, format="csv")
        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        workbook = load_workbook(
            io.BytesIO(adapter.read_bytes(xlsx.storage_key)),
            read_only=True,
            data_only=True,
        )
        data_row = next(workbook["offers"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(data_row[10], "'=1+1")
        self.assertIsInstance(data_row[7], int)
        self.assertIsInstance(data_row[8], float)
        csv_text = adapter.read_bytes(csv_artifact.storage_key).decode("utf-8-sig")
        self.assertIn("'=1+1", csv_text)

    def test_locked_only_file_is_explicit_empty(self):
        run = self.upload_and_process([HEADERS, row(lock="Locked")])
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        approve_run(run=run, user=self.user)
        xlsx = create_export(run=run, user=self.user, format="xlsx")
        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        workbook = load_workbook(
            io.BytesIO(adapter.read_bytes(xlsx.storage_key)),
            read_only=True,
            data_only=True,
        )
        self.assertEqual(len(list(workbook["offers"].iter_rows(min_row=2))), 0)
        self.assertEqual(len(list(workbook["excluded"].iter_rows(min_row=2))), 1)

    def test_cancel_before_process(self):
        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row()]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        cancel_run(run=run, user=self.user)
        self.assertEqual(process_import_run(run.id), "noop:cancelled")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)

    def test_missing_stored_bytes_fails_run(self):
        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row()]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        import os

        os.remove(f"{self.tmp.name}/{run.source_file.storage_key}")
        self.assertEqual(process_import_run(run.id), "failed")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertIn("missing", run.error)
