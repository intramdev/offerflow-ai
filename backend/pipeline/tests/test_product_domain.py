"""Domain-aware required fields and same-file price conflicts.

Office-type documents carry no capacity/grade evidence; the reviewer
declares the file general once instead of fabricating per-row values.
Same-identity rows priced differently need an explicit human choice
before either price is authoritative.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from organizations.models import Membership, Organization
from pipeline.jobs import process_import_run
from pipeline.models import (
    NormalizedOffer,
    OfferStatus,
    ReviewDecision,
    ReviewKind,
    ReviewStatus,
    RunStatus,
)
from pipeline.normalize import (
    DOMAIN_GENERAL,
    compute_offer_status,
    normalize_cells,
)
from pipeline.review import answer_request, ignore_request
from pipeline.validate import price_label
from pipeline.tests.test_parsing import force_fixture_provider, make_workbook
from pipeline.uploads import create_upload

User = get_user_model()

OFFICE_HEADERS = ["Supplier", "Model", "Qty", "Price", "Currency"]


def office_row(
    supplier="Nordo",
    model="NDO-001",
    qty=40,
    price=54,
    currency="EUR",
):
    return [supplier, model, qty, price, currency]


def silent_provider():
    """A provider that proposes nothing, so gaps stay gaps."""
    fake = mock.Mock()
    fake.name = "meta"
    fake.is_simulated = False

    def interpret(payload, on_chunk=None):
        return {
            "simulated": False,
            "provider": "meta",
            "model": "test-model",
            "rows": [
                {"index": entry["index"], "fields": {}}
                for entry in payload["rows"]
            ],
        }

    fake.interpret_table.side_effect = interpret
    return fake


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProductDomainTests(TestCase):
    def setUp(self):
        force_fixture_provider(self)
        self.user = User.objects.create_user(
            username="u", email="u@example.test", password="x"
        )
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.user, organization=self.org, role="owner"
        )

    def upload_office(self, rows, name="office.xlsx"):
        uploaded = SimpleUploadedFile(
            name,
            make_workbook(rows),
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
        )
        run = create_upload(
            organization=self.org, user=self.user, uploaded_file=uploaded
        )
        with mock.patch(
            "pipeline.jobs.get_provider", return_value=silent_provider()
        ):
            self.assertEqual(process_import_run(run.id), "ok")
        run.refresh_from_db()
        return run

    def test_device_domain_requires_capacity_and_grade(self):
        fields = normalize_cells(
            {
                "supplier": "Acme",
                "model": "P1",
                "quantity": "5",
                "price": "299.99",
                "currency": "USD",
            }
        )
        status, gaps, _reason = compute_offer_status(fields)
        self.assertEqual(status, OfferStatus.NEEDS_INPUT)
        self.assertEqual(
            {gap["field"] for gap in gaps}, {"capacity_gb", "grade"}
        )

    def test_general_domain_marks_device_fields_not_applicable(self):
        fields = normalize_cells(
            {
                "supplier": "Nordo",
                "model": "NDO-001",
                "quantity": "40",
                "price": "54",
                "currency": "EUR",
            }
        )
        status, gaps, _reason = compute_offer_status(
            fields, domain=DOMAIN_GENERAL
        )
        self.assertEqual(gaps, [])
        self.assertEqual(status, OfferStatus.OK)

    def test_office_file_asks_one_domain_question(self):
        run = self.upload_office(
            [OFFICE_HEADERS, office_row(), office_row(model="NDO-002")]
        )
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        domain = list(
            run.reviews.filter(
                status=ReviewStatus.OPEN, offer__isnull=True, field="domain"
            )
        )
        self.assertEqual(len(domain), 1)
        self.assertEqual(domain[0].kind, ReviewKind.CHOICE)
        self.assertEqual(len(domain[0].choices), 2)

    def test_answer_general_resolves_device_gaps_as_not_applicable(self):
        run = self.upload_office([OFFICE_HEADERS, office_row()])
        domain = run.reviews.get(
            status=ReviewStatus.OPEN, offer__isnull=True, field="domain"
        )
        answer_request(
            review=domain, user=self.user, choice=domain.choices[1]
        )
        run.refresh_from_db()
        self.assertEqual(
            (run.mapping or {}).get("domain"), DOMAIN_GENERAL
        )
        self.assertFalse(
            run.reviews.filter(status=ReviewStatus.OPEN).exists()
        )
        offer = NormalizedOffer.objects.get(run=run)
        # Unknown stays unknown: nothing was invented.
        self.assertIsNone(offer.capacity_gb)
        self.assertEqual(offer.grade, "")
        self.assertEqual(offer.status, OfferStatus.OK)
        # But the record shows not-applicable, not unanswered.
        values = {
            decision.request.field: decision.value
            for decision in ReviewDecision.objects.filter(
                request__run=run, request__offer=offer
            )
        }
        self.assertEqual(
            values,
            {
                "capacity_gb": {"not_applicable": True},
                "grade": {"not_applicable": True},
            },
        )
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertTrue(
            all(
                item.passed for item in run.validations.order_by("name")
            )
        )

    def test_answer_device_keeps_row_gaps(self):
        run = self.upload_office([OFFICE_HEADERS, office_row()])
        domain = run.reviews.get(
            status=ReviewStatus.OPEN, offer__isnull=True, field="domain"
        )
        answer_request(
            review=domain, user=self.user, choice=domain.choices[0]
        )
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        self.assertEqual(
            set(
                run.reviews.filter(status=ReviewStatus.OPEN).values_list(
                    "field", flat=True
                )
            ),
            {"capacity_gb", "grade"},
        )

    def test_phone_evidence_asks_no_domain_question(self):
        run = self.upload_office(
            [
                ["Supplier", "Model", "Capacity", "Grade", "Price", "Currency"],
                ["Acme", "P1", "128GB", "A", 299.99, "USD"],
                ["Acme", "P2", "", "", 199.99, "USD"],
            ],
            name="phones.xlsx",
        )
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        self.assertFalse(
            run.reviews.filter(offer__isnull=True, field="domain").exists()
        )
        # The row without device data still asks per row.
        gap_offer = NormalizedOffer.objects.get(run=run, model_name="P2")
        self.assertEqual(
            set(
                run.reviews.filter(
                    status=ReviewStatus.OPEN, offer=gap_offer
                ).values_list("field", flat=True)
            ),
            {"capacity_gb", "grade", "quantity"},
        )

    def test_same_identity_price_conflict_asks_for_choice(self):
        run = self.upload_office(
            [
                OFFICE_HEADERS,
                office_row(model="NDO-007", qty=70, price=12),
                office_row(model="NDO-007", qty=70, price=13.5),
            ]
        )
        conflicts = list(
            run.reviews.filter(
                status=ReviewStatus.OPEN, field="price_conflict"
            ).order_by("id")
        )
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(conflicts[0].kind, ReviewKind.CHOICE)
        self.assertEqual(conflicts[0].choices, ["12", "13.5"])
        self.assertIn("NDO-007", conflicts[0].question)

    def test_conflict_answer_authorizes_one_price(self):
        run = self.upload_office(
            [
                OFFICE_HEADERS,
                office_row(model="NDO-007", qty=70, price=12),
                office_row(model="NDO-007", qty=70, price=13.5),
            ]
        )
        domain = run.reviews.get(
            status=ReviewStatus.OPEN, offer__isnull=True, field="domain"
        )
        answer_request(
            review=domain, user=self.user, choice=domain.choices[1]
        )
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        cheap = NormalizedOffer.objects.get(run=run, unit_price=Decimal("12"))
        pricey = NormalizedOffer.objects.get(
            run=run, unit_price=Decimal("13.5")
        )
        conflict = run.reviews.get(
            status=ReviewStatus.OPEN, offer=cheap, field="price_conflict"
        )
        answer_request(review=conflict, user=self.user, choice="12")
        cheap.refresh_from_db()
        pricey.refresh_from_db()
        run.refresh_from_db()
        # The chosen row is authoritative; the other stays on record,
        # excluded, with its provenance untouched.
        self.assertEqual(cheap.status, OfferStatus.OK)
        self.assertEqual(pricey.status, OfferStatus.EXCLUDED)
        self.assertIn("12", pricey.exclusion_reason)
        self.assertEqual(pricey.source_row.data["price"], 13.5)
        self.assertFalse(
            run.reviews.filter(status=ReviewStatus.OPEN).exists()
        )
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        gates = {item.name: item.passed for item in run.validations.all()}
        self.assertTrue(gates["price_conflicts_resolved"])
        self.assertTrue(all(gates.values()))

    def test_true_variants_do_not_conflict(self):
        run = self.upload_office(
            [
                ["Supplier", "Model", "Grade", "Qty", "Price", "Currency"],
                ["Acme", "P1", "A", 5, 299.99, "USD"],
                ["Acme", "P1", "B", 5, 199.99, "USD"],
            ],
            name="variants.xlsx",
        )
        self.assertFalse(
            run.reviews.filter(field="price_conflict").exists()
        )

    def test_same_price_duplicates_do_not_conflict(self):
        run = self.upload_office(
            [
                OFFICE_HEADERS,
                office_row(model="NDO-007", qty=70, price=12),
                office_row(model="NDO-007", qty=70, price=12),
            ]
        )
        self.assertFalse(
            run.reviews.filter(field="price_conflict").exists()
        )

    def test_price_labels_use_plain_notation(self):
        self.assertEqual(price_label(Decimal("20.0000")), "20")
        self.assertEqual(price_label(Decimal("100")), "100")
        self.assertEqual(price_label(Decimal("13.50")), "13.5")
        self.assertEqual(price_label(Decimal("0.50")), "0.5")

    def test_ignore_dissolves_conflict_as_moot(self):
        run = self.upload_office(
            [
                OFFICE_HEADERS,
                office_row(model="NDO-007", qty=70, price=12),
                office_row(model="NDO-007", qty=70, price=13.5),
            ]
        )
        pricey = NormalizedOffer.objects.get(
            run=run, unit_price=Decimal("13.5")
        )
        doomed = run.reviews.filter(
            status=ReviewStatus.OPEN, offer=pricey
        ).first()
        ignore_request(review=doomed, user=self.user)
        run.refresh_from_db()
        # One price left: the surviving row's conflict question is moot,
        # answered without inventing a choice.
        cheap = NormalizedOffer.objects.get(run=run, unit_price=Decimal("12"))
        moot = run.reviews.get(offer=cheap, field="price_conflict")
        self.assertEqual(moot.status, ReviewStatus.ANSWERED)
        self.assertEqual(moot.decision.value, {"moot": True})
