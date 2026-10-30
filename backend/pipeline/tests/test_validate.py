"""Tests for mandatory validation gates."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from organizations.models import Membership, Organization
from pipeline.models import (
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    ReviewRequest,
    RuleSetVersion,
    RunStatus,
    SourceFile,
    SourceRow,
)
from pipeline.validate import run_validations

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
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
            status=RunStatus.VALIDATION,
        )

    def add_offer(self, index, quantity_raw, quantity, status=OfferStatus.OK, provenance=True):
        row = SourceRow.objects.create(
            organization=self.org,
            run=self.run,
            index=index,
            sheet="Sheet1",
            data={"quantity": quantity_raw},
            provenance={"sheet": "Sheet1", "row": index},
        )
        return NormalizedOffer.objects.create(
            organization=self.org,
            run=self.run,
            source_row=row,
            supplier="Acme",
            model_name="P1",
            capacity_gb=128,
            grade="A",
            grade_raw="A",
            lock_state="unlocked",
            quantity=quantity,
            unit_price=Decimal("10.00"),
            currency="USD",
            status=status,
            provenance={"sheet": "Sheet1", "row": index} if provenance else {},
        )

    def test_all_pass(self):
        self.add_offer(2, 5, 5)
        self.add_offer(3, "7", 7)
        passed, results = run_validations(self.run)
        self.assertTrue(passed)
        self.assertEqual(len(results), 6)
        self.assertEqual(
            {item.name for item in results},
            {
                "review_coverage",
                "criticals_complete",
                "quantities_reconcile",
                "provenance_complete",
                "no_incompatible_merge",
                "releasable_or_explicit_empty",
            },
        )

    def test_open_review_blocks(self):
        offer = self.add_offer(2, 5, 5)
        ReviewRequest.objects.create(
            organization=self.org,
            run=self.run,
            offer=offer,
            kind="value",
            field="currency",
            question="q",
        )
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        coverage = next(item for item in results if item.name == "review_coverage")
        self.assertFalse(coverage.passed)

    def test_pending_offer_blocks(self):
        self.add_offer(2, 5, 5, status=OfferStatus.NEEDS_INPUT)
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        criticals = next(item for item in results if item.name == "criticals_complete")
        self.assertFalse(criticals.passed)

    def test_tampered_quantity_breaks_reconcile(self):
        offer = self.add_offer(2, 5, 5)
        offer.quantity = 99
        offer.save(update_fields=["quantity"])
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        reconcile = next(item for item in results if item.name == "quantities_reconcile")
        self.assertFalse(reconcile.passed)

    def test_missing_provenance_fails(self):
        self.add_offer(2, 5, 5, provenance=False)
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        provenance = next(item for item in results if item.name == "provenance_complete")
        self.assertFalse(provenance.passed)

    def test_all_excluded_is_explicit_empty(self):
        self.add_offer(2, 5, 5, status=OfferStatus.EXCLUDED)
        passed, _results = run_validations(self.run)
        self.assertTrue(passed)

    def test_empty_output_fails(self):
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        releasable = next(
            item for item in results if item.name == "releasable_or_explicit_empty"
        )
        self.assertFalse(releasable.passed)
