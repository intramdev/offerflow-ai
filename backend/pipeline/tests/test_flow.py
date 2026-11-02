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

    def test_org_default_yields_to_file_currency_column(self):
        self.org.default_currency = "EUR"
        self.org.save(update_fields=["default_currency"])
        run = self.upload_and_process(MIXED_ROWS)
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        self.assertEqual(run.mapping.get("applied_defaults"), {})
        first = run.offers.filter(status=OfferStatus.OK).first()
        self.assertIsNotNone(first)
        self.assertEqual(first.currency, "USD")

    def test_ignore_record_excludes_offer_and_advances(self):
        from pipeline.review import ignore_request

        run = self.upload_and_process(MIXED_ROWS)
        by_field = {
            review.field: review
            for review in run.reviews.filter(status=ReviewStatus.OPEN)
        }
        offer = ignore_request(review=by_field["unit_price"], user=self.user)
        self.assertEqual(offer.status, OfferStatus.EXCLUDED)
        self.assertIn("u", offer.exclusion_reason)
        self.assertFalse(
            run.reviews.filter(
                offer=offer, status=ReviewStatus.OPEN
            ).exists()
        )
        decision = by_field["unit_price"].decision
        self.assertEqual(decision.value, {"ignored": True})
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        answer_request(review=by_field["grade"], user=self.user, choice="A")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        approve_run(run=run, user=self.user)
        xlsx = create_export(run=run, user=self.user, format="xlsx")
        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        workbook = load_workbook(
            io.BytesIO(adapter.read_bytes(xlsx.storage_key)),
            read_only=True,
            data_only=True,
        )
        data_rows = list(workbook["offers"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(data_rows), 2)
        excluded_rows = list(
            workbook["excluded"].iter_rows(min_row=2, values_only=True)
        )
        self.assertEqual(len(excluded_rows), 2)

    def test_last_quantity_answer_advances_run(self):
        # R5: an accepted quantity clarification participates in
        # reconciliation and the run leaves needs_input; the source
        # cell keeps its original (empty) value.
        run = self.upload_and_process([HEADERS, row(qty="")])
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        open_reviews = list(
            run.reviews.filter(status=ReviewStatus.OPEN).order_by("id")
        )
        self.assertEqual([review.field for review in open_reviews], ["quantity"])
        answer_request(review=open_reviews[0], user=self.user, value="9")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertFalse(
            run.reviews.filter(status=ReviewStatus.OPEN).exists()
        )
        offer = run.offers.get()
        self.assertEqual(offer.quantity, 9)
        offer.source_row.refresh_from_db()
        self.assertIsNone(offer.source_row.data["quantity"])

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

    def test_unresolvable_org_fails_run_visibly(self):
        from unittest import mock

        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row()]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        with mock.patch(
            "pipeline.jobs._run_org_for_worker", return_value=None
        ):
            self.assertEqual(process_import_run(run.id), "missing")
        run.refresh_from_db()
        # Failed, not stranded in uploaded: the reviewer sees the
        # error and can remap to queue the file again.
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertIn("could not resolve", run.error)

    def test_message_lines_extract_through_model_lane(self):
        from decimal import Decimal
        from unittest import mock

        from pipeline.models import NormalizedOffer

        uploaded = SimpleUploadedFile(
            "pasted-text-20261102-120000.txt",
            b"iPhone 15 128GB Black x5 450\n",
            content_type="text/plain",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        run.mapping = {
            "confirmed": {},
            "constants": {"supplier": "Acme"},
            "ignored": [],
            "free_text": True,
        }
        run.save(update_fields=["mapping", "updated_at"])
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False

        def fake_interpret(payload, on_chunk=None):
            entry = payload["rows"][0]
            assert (
                entry["data"]["message"] == "iPhone 15 128GB Black x5 450"
            )
            return {
                "simulated": False,
                "provider": "meta",
                "model": "test-model",
                "rows": [
                    {
                        "index": entry["index"],
                        "fields": {
                            # Ignored: supplier came from a set value.
                            "supplier": "Globex",
                            "brand": "Apple",
                            "model": "iPhone 15",
                            "capacity": "128",
                            "color": "Black",
                            "grade": "A",
                            "lock": "",
                            "quantity": "5",
                            "price": "450",
                            "currency": "",
                            "conditions": "",
                        },
                    }
                ],
            }

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        offer = NormalizedOffer.objects.get(run=run)
        self.assertEqual(offer.supplier, "Acme")
        self.assertEqual(offer.model_name, "iPhone 15")
        self.assertEqual(offer.unit_price, Decimal("450"))
        self.assertEqual(offer.quantity, 5)
        # The raw chat line survives as evidence on the source row.
        self.assertEqual(
            offer.source_row.data["message"], "iPhone 15 128GB Black x5 450"
        )
        self.assertEqual(offer.source_row.sheet, run.source_file.original_name)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)

    def test_ungrounded_message_quantity_asks_human(self):
        from unittest import mock

        from pipeline.models import NormalizedOffer

        uploaded = SimpleUploadedFile(
            "pasted-text-20261102-130000.txt",
            b"iPhone 15 128GB Black x5 450\n",
            content_type="text/plain",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        run.mapping = {
            "confirmed": {},
            "constants": {"supplier": "Acme"},
            "ignored": [],
            "free_text": True,
        }
        run.save(update_fields=["mapping", "updated_at"])
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False

        def fake_interpret(payload, on_chunk=None):
            entry = payload["rows"][0]
            return {
                "simulated": False,
                "provider": "meta",
                "model": "test-model",
                "rows": [
                    {
                        "index": entry["index"],
                        "fields": {
                            "model": "iPhone 15",
                            "capacity": "128",
                            "grade": "A",
                            "lock": "",
                            # Invented: no 9 appears in the source line.
                            "quantity": "9",
                            "price": "450",
                            "currency": "",
                        },
                    }
                ],
            }

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        offer = NormalizedOffer.objects.get(run=run)
        # Grounded values are adopted; the invented quantity is not.
        self.assertEqual(offer.capacity_gb, 128)
        self.assertEqual(offer.unit_price, Decimal("450"))
        self.assertIsNone(offer.quantity)
        self.assertEqual(offer.status, OfferStatus.NEEDS_INPUT)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        open_reviews = list(run.reviews.filter(status=ReviewStatus.OPEN))
        self.assertEqual([review.field for review in open_reviews], ["quantity"])

    def test_message_headers_classified_and_name_supplier(self):
        from unittest import mock

        from pipeline.models import NormalizedOffer

        uploaded = SimpleUploadedFile(
            "pasted-text-20261102-140000.txt",
            (
                "*Mercantile Pacific - Japan Spec*\n"
                "> iPhone 16 Pro 128GB x2 - 349\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        run.mapping = {
            "confirmed": {},
            "constants": {},
            "ignored": [],
            "free_text": True,
        }
        run.save(update_fields=["mapping", "updated_at"])
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False

        def fake_interpret(payload, on_chunk=None):
            # The full paste travels as context so header lines can
            # name the supplier for every product row.
            self.assertIn("Mercantile Pacific", payload["context"])
            self.assertIn("iPhone 16 Pro", payload["context"])
            by_index = {entry["index"]: entry for entry in payload["rows"]}
            self.assertEqual(len(by_index), 2)
            header = by_index[min(by_index)]
            product = by_index[max(by_index)]
            self.assertIn("Mercantile", header["data"]["message"])
            empty = {name: "" for name in ("supplier", "brand", "model", "capacity", "color", "grade", "lock", "quantity", "price", "currency", "conditions")}
            return {
                "simulated": False,
                "provider": "meta",
                "model": "test-model",
                "rows": [
                    {
                        "index": header["index"],
                        "fields": dict(empty),
                        "is_product": False,
                    },
                    {
                        "index": product["index"],
                        # is_product omitted: products stay the default.
                        "fields": {
                            **empty,
                            "supplier": "Mercantile Pacific",
                            "brand": "Apple",
                            "model": "iPhone 16 Pro",
                            "capacity": "128",
                            "color": "",
                            "grade": "A",
                            "quantity": "2",
                            "price": "349",
                        },
                    },
                ],
            }

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        offers = list(
            NormalizedOffer.objects.filter(run=run).order_by("source_row__index")
        )
        self.assertEqual(len(offers), 2)
        header_offer, product_offer = offers
        self.assertEqual(header_offer.status, OfferStatus.EXCLUDED)
        self.assertIn("header or note", header_offer.exclusion_reason)
        self.assertIsNone(header_offer.quantity)
        self.assertEqual(product_offer.status, OfferStatus.OK)
        self.assertEqual(product_offer.supplier, "Mercantile Pacific")
        self.assertEqual(product_offer.model_name, "iPhone 16 Pro")
        self.assertEqual(product_offer.quantity, 2)
        self.assertEqual(product_offer.unit_price, Decimal("349"))
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertFalse(
            run.reviews.filter(status=ReviewStatus.OPEN).exists()
        )

    def test_message_section_grades_apply_to_covered_rows(self):
        from unittest import mock

        from pipeline.models import NormalizedOffer

        uploaded = SimpleUploadedFile(
            "pasted-text-20261102-150000.txt",
            (
                "Mercantile Pacific - Japan Spec\n"
                "P1+ - Like New\n"
                "iPhone 16 Pro 128GB x2 - 349\n"
                "P2+ - A GRADE Tested\n"
                "iPhone 15 128GB x1 - 505\n"
            ).encode("utf-8"),
            content_type="text/plain",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        run.mapping = {
            "confirmed": {},
            "constants": {},
            "ignored": [],
            "free_text": True,
        }
        run.save(update_fields=["mapping", "updated_at"])
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False

        def fake_interpret(payload, on_chunk=None):
            self.assertIn("P1+ - Like New", payload["context"])
            self.assertIn("P2+ - A GRADE Tested", payload["context"])
            empty = {name: "" for name in ("supplier", "brand", "model", "capacity", "color", "grade", "lock", "quantity", "price", "currency", "conditions")}
            rows = []
            section = ""
            for entry in sorted(payload["rows"], key=lambda item: item["index"]):
                line = entry["data"]["message"]
                if "iPhone" not in line:
                    if line.startswith("P1+") or line.startswith("P2+"):
                        section = line.strip()
                    rows.append(
                        {
                            "index": entry["index"],
                            "fields": dict(empty),
                            "is_product": False,
                        }
                    )
                    continue
                rows.append(
                    {
                        "index": entry["index"],
                        "fields": {
                            **empty,
                            "supplier": "Mercantile Pacific",
                            "brand": "Apple",
                            "model": "iPhone 16 Pro" if "16 Pro" in line else "iPhone 15",
                            "capacity": "128",
                            "grade": section,
                            "quantity": "2" if "x2" in line else "1",
                            "price": "349" if "349" in line else "505",
                        },
                    }
                )
            return {
                "simulated": False,
                "provider": "meta",
                "model": "test-model",
                "rows": rows,
            }

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        offers = list(
            NormalizedOffer.objects.filter(run=run).order_by("source_row__index")
        )
        self.assertEqual(len(offers), 5)
        products = [offer for offer in offers if offer.status == OfferStatus.OK]
        self.assertEqual(
            [(offer.model_name, offer.grade) for offer in products],
            [
                ("iPhone 16 Pro", "P1+ - Like New"),
                ("iPhone 15", "P2+ - A GRADE Tested"),
            ],
        )
        self.assertTrue(
            all(offer.supplier == "Mercantile Pacific" for offer in products)
        )
        headers = [offer for offer in offers if offer.status == OfferStatus.EXCLUDED]
        self.assertEqual(len(headers), 3)

    def test_interpret_prompt_teaches_section_inheritance(self):
        from common.meta import SYSTEM_PROMPT

        # The section behavior is prompt-delivered; pin the instruction
        # so it cannot be silently dropped from the shared context call.
        self.assertIn("until the next header", SYSTEM_PROMPT)
        self.assertIn("into every covered product row", SYSTEM_PROMPT)

    def test_failed_run_keeps_attempted_provider_lane(self):
        from unittest import mock

        from common.meta import ProviderError

        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row(price="")]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        failing = mock.Mock()
        failing.name = "meta"
        failing.is_simulated = False
        failing.interpret_table.side_effect = ProviderError("boom; not retried.")
        with mock.patch("pipeline.jobs.get_provider", return_value=failing):
            self.assertEqual(process_import_run(run.id), "failed")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.provider_name, "meta")
        self.assertFalse(run.simulated)
        self.assertIn("not retried", run.error)

    def test_chunked_usage_records_one_event_per_call(self):
        from unittest import mock

        from pipeline.models import UsageEvent
        from pipeline.tests.test_spending import FIELDS

        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row(), row(price="")]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False
        seen_payloads = []

        def fake_interpret(payload, on_chunk=None):
            seen_payloads.append(payload)
            items = [
                {"index": entry["index"], "fields": dict(FIELDS)}
                for entry in payload["rows"]
            ]
            if on_chunk is not None:
                on_chunk(1, 2, 10, 4)
                on_chunk(2, 2, 20, 8)
            return {
                "simulated": False,
                "provider": "meta",
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 12,
                    "total_tokens": 42,
                },
                "chunk_usages": [
                    {"prompt_tokens": 10, "completion_tokens": 4},
                    {"prompt_tokens": 20, "completion_tokens": 8},
                ],
                "rows": items,
            }

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        calls = list(
            UsageEvent.objects.filter(
                run_id=run.id, kind="provider_call"
            ).order_by("id")
        )
        self.assertEqual([event.quantity for event in calls], [14, 28])
        self.assertEqual(calls[0].detail["chunk"], "1/2")
        self.assertEqual(calls[1].detail["chunk"], "2/2")
        # Only the gap row reached the model; the clean row stayed local.
        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(len(seen_payloads[0]["rows"]), 1)
        run.refresh_from_db()
        self.assertEqual(run.mapping["ai_progress"], {"done": 2, "total": 2})
        self.assertEqual(run.offers.count(), 2)

    def test_model_cells_cannot_overwrite_readable_values(self):
        from unittest import mock
        from decimal import Decimal

        from pipeline.models import NormalizedOffer

        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row(grade="")]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False

        def fake_interpret(payload, on_chunk=None):
            entry = payload["rows"][0]
            fields = dict(entry["data"])
            fields["grade"] = "A"
            # The model also "corrects" the price and quantity, which
            # were perfectly readable; those must be ignored.
            fields["price"] = "199.99"
            fields["quantity"] = "9"
            return {
                "simulated": False,
                "provider": "meta",
                "model": "test-model",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
                "rows": [{"index": entry["index"], "fields": fields}],
            }

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        offer = NormalizedOffer.objects.get(run_id=run.id)
        # Gap filled from the proposal; readable values untouched.
        self.assertEqual(offer.grade, "A")
        self.assertEqual(offer.unit_price, Decimal("299.99"))
        self.assertEqual(offer.quantity, 5)
        # The full proposal stays on the record for audit.
        self.assertEqual(offer.interpreted["price"], "199.99")

    def test_clean_rows_skip_provider_entirely(self):
        from unittest import mock

        from pipeline.models import UsageEvent

        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row()]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "ok")
        fake.interpret_table.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertEqual(run.provider_name, "local")
        self.assertTrue(run.simulated)
        self.assertFalse(
            UsageEvent.objects.filter(
                run_id=run.id, kind="provider_call"
            ).exists()
        )

    def test_mid_flight_cancel_aborts_at_chunk_boundary(self):
        from unittest import mock

        from pipeline.models import UsageEvent, transition_run

        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row(), row(price="")]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        fake = mock.Mock()
        fake.name = "meta"
        fake.is_simulated = False

        def fake_interpret(payload, on_chunk=None):
            transition_run(run, RunStatus.CANCELLED)
            on_chunk(1, 2, 10, 4)
            raise AssertionError("should have aborted before chunk 2")

        fake.interpret_table.side_effect = fake_interpret
        with mock.patch("pipeline.jobs.get_provider", return_value=fake):
            self.assertEqual(process_import_run(run.id), "cancelled")
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)
        self.assertEqual(run.error, "")
        calls = UsageEvent.objects.filter(
            run_id=run.id, kind="provider_call"
        )
        self.assertEqual(calls.count(), 1)
        self.assertEqual(calls[0].quantity, 14)

    def test_cancel_approved_run_refused(self):
        run = self.upload_and_process([HEADERS, row()])
        approve_run(run=run, user=self.user)
        from django.core.exceptions import ValidationError

        with self.assertRaisesRegex(ValidationError, "already approved"):
            cancel_run(run=run, user=self.user)

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
