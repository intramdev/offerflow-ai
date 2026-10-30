"""Re-evaluation prefers model-returned cells when the offer has them."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from organizations.models import Organization
from pipeline.models import (
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
    RuleSetVersion,
    RunStatus,
    SourceFile,
    SourceRow,
)
from pipeline.review import _offer_fields, answer_request

User = get_user_model()


def _cells(**overrides):
    cells = {
        "supplier": "Synthetic Supply Co.",
        "brand": "Apple",
        "model": "iPhone 12",
        "capacity": "128GB",
        "color": "black",
        "grade": "B",
        "lock": "unlocked",
        "quantity": "2",
        "price": "299.99",
        "currency": "EUR",
        "conditions": "",
    }
    cells.update(overrides)
    return cells


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InterpretedEvidenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        self.rule = RuleSetVersion.objects.get(version="v1")
        self.source_file = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.user,
            original_name="s.xlsx",
            size_bytes=10,
            sha256="ab" * 32,
            storage_key="org/1",
        )
        self.run = ImportRun.objects.create(
            organization=self.org,
            source_file=self.source_file,
            rule_version=self.rule,
            status=RunStatus.NEEDS_INPUT,
        )
        self.row = SourceRow.objects.create(
            organization=self.org,
            run=self.run,
            index=2,
            sheet="Sheet1",
            data=_cells(),
            provenance={"sheet": "Sheet1", "row": 2},
        )

    def add_offer(self, **kwargs):
        params = {
            "organization": self.org,
            "run": self.run,
            "source_row": self.row,
            "supplier": "Synthetic Supply Co.",
            "brand": "Apple",
            "model_name": "iPhone 12",
            "capacity_gb": 128,
            "color": "black",
            "grade": "",
            "grade_raw": "",
            "lock_state": "unlocked",
            "quantity": 2,
            "unit_price": "299.99",
            "currency": "EUR",
            "conditions": "",
            "status": OfferStatus.NEEDS_INPUT,
            "provenance": {"sheet": "Sheet1", "row": 2},
        }
        params.update(kwargs)
        return NormalizedOffer.objects.create(**params)

    def test_fields_prefer_interpreted_cells(self):
        interpreted = _cells(price="priceless")
        offer = self.add_offer(interpreted=interpreted)
        fields = _offer_fields(offer)
        self.assertEqual(fields["price_raw"], "priceless")
        self.assertEqual(fields["quantity_raw"], "2")

    def test_fields_fall_back_to_source_cells(self):
        offer = self.add_offer()
        fields = _offer_fields(offer)
        self.assertEqual(fields["price_raw"], "299.99")

    def test_followup_evidence_uses_interpreted_cells(self):
        interpreted = _cells(price="priceless")
        offer = self.add_offer(interpreted=interpreted, unit_price=None)
        review = ReviewRequest.objects.create(
            organization=self.org,
            run=self.run,
            offer=offer,
            kind=ReviewKind.VALUE,
            field="unit_price",
            question="What is the unit price?",
            evidence={},
            status=ReviewStatus.OPEN,
        )
        answer_request(review=review, user=None, value=10)
        followups = ReviewRequest.objects.filter(run=self.run, status=ReviewStatus.OPEN)
        self.assertTrue(followups.exists())
        for item in followups:
            self.assertEqual(item.evidence.get("raw"), interpreted)
