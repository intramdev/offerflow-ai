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
            data={
                "supplier": "Acme",
                "brand": "",
                "model": "P1",
                "capacity": "128",
                "color": "",
                "grade": "A",
                "lock": "unlocked",
                "quantity": quantity_raw,
                "price": "10.00",
                "currency": "USD",
                "conditions": "",
            },
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
        self.assertEqual(len(results), 9)
        self.assertEqual(
            {item.name for item in results},
            {
                "review_coverage",
                "criticals_complete",
                "quantities_reconcile",
                "provenance_complete",
                "rows_reconcile_with_evidence",
                "roles_bound_to_evidence",
                "no_incompatible_merge",
                "price_conflicts_resolved",
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

    def test_invented_provenance_fails(self):
        # A well-formed reference that points at no real row is
        # forgery, not evidence: it must resolve to the offer's own
        # source row.
        offer = self.add_offer(2, 5, 5)
        offer.provenance = {"sheet": "Sheet1", "row": 999}
        offer.save(update_fields=["provenance"])
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        provenance = next(item for item in results if item.name == "provenance_complete")
        self.assertFalse(provenance.passed)

    def test_swapped_quantities_fail_row_gate(self):
        # Totals still agree (5 + 7), so only per-row evidence
        # catches the swap.
        self.add_offer(2, 5, 7)
        self.add_offer(3, 7, 5)
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        totals = next(item for item in results if item.name == "quantities_reconcile")
        self.assertTrue(totals.passed)
        rows = next(
            item for item in results if item.name == "rows_reconcile_with_evidence"
        )
        self.assertFalse(rows.passed)
        self.assertEqual(len(rows.details["offers"]), 2)

    def test_model_filled_quantity_gap_reconciles(self):
        # A message row has no readable quantity cell; the quantities
        # gate reconciles against the same merged proposal the per-row
        # gate authorizes, so both sides count the gap value once.
        row = SourceRow.objects.create(
            organization=self.org,
            run=self.run,
            index=2,
            sheet="paste.txt",
            data={
                "message": "iPhone 15 128GB Black x5 450",
                "supplier": "Acme",
                "currency": "USD",
            },
            provenance={"sheet": "paste.txt", "row": 2},
        )
        NormalizedOffer.objects.create(
            organization=self.org,
            run=self.run,
            source_row=row,
            supplier="Acme",
            model_name="iPhone 15",
            capacity_gb=128,
            grade="A",
            grade_raw="A",
            lock_state="unknown",
            quantity=5,
            unit_price=Decimal("450"),
            currency="USD",
            status=OfferStatus.OK,
            provenance={"sheet": "paste.txt", "row": 2},
            interpreted={
                "model": "iPhone 15",
                "capacity": "128",
                "grade": "A",
                "quantity": "5",
                "price": "450",
            },
        )
        passed, results = run_validations(self.run)
        self.assertTrue(passed)
        reconcile = next(
            item for item in results if item.name == "quantities_reconcile"
        )
        self.assertTrue(reconcile.passed)

    def test_changed_price_and_capacity_fail_row_gate(self):
        offer = self.add_offer(2, 5, 5)
        offer.unit_price = Decimal("99.99")
        offer.capacity_gb = 256
        offer.save(update_fields=["unit_price", "capacity_gb"])
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        rows = next(
            item for item in results if item.name == "rows_reconcile_with_evidence"
        )
        self.assertFalse(rows.passed)
        self.assertEqual(
            set(rows.details["offers"][0]["fields"]), {"unit_price", "capacity_gb"}
        )

    def test_locked_offer_flipped_to_ok_fails(self):
        offer = self.add_offer(2, 5, 5, status=OfferStatus.EXCLUDED)
        row = offer.source_row
        data = dict(row.data)
        data["lock"] = "locked"
        row.data = data
        row.save(update_fields=["data"])
        offer.lock_state = "locked"
        offer.status = OfferStatus.OK
        offer.save(update_fields=["lock_state", "status"])
        passed, results = run_validations(self.run)
        self.assertFalse(passed)
        rows = next(
            item for item in results if item.name == "rows_reconcile_with_evidence"
        )
        self.assertFalse(rows.passed)

    def test_human_quantity_decision_participates(self):
        # An approved clarification authorizes the new quantity in
        # both gates while the original cell stays untouched.
        from pipeline.models import ReviewDecision, ReviewKind, ReviewRequest

        offer = self.add_offer(2, 5, 9)
        request = ReviewRequest.objects.create(
            organization=self.org,
            run=self.run,
            offer=offer,
            kind=ReviewKind.VALUE,
            field="quantity",
            question="Supply a positive quantity.",
            status="answered",
        )
        ReviewDecision.objects.create(
            organization=self.org, request=request, decided_by=self.user, value="9"
        )
        passed, results = run_validations(self.run)
        self.assertTrue(passed)
        offer.source_row.refresh_from_db()
        self.assertEqual(offer.source_row.data["quantity"], 5)

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


class RolesGateTests(TestCase):
    """Terminal-scoped rows bind price and quantity to source spans."""

    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.user, organization=self.org, role="owner"
        )
        self.rule = RuleSetVersion.objects.get(version="v1")
        self.source_file = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.user,
            original_name="paste.txt",
            size_bytes=10,
            sha256="ab" * 32,
            storage_key="org/1",
        )
        self.run = ImportRun.objects.create(
            organization=self.org,
            source_file=self.source_file,
            rule_version=self.rule,
            status=RunStatus.VALIDATION,
            mapping={"free_text": True},
        )

    def add_terminal_offer(self, price, quantity=None, currency="USD",
                           scoped=True, text="> Phone 18 Pro 256GB - 417",
                           cell_quantity=None):
        from pipeline.normalize import terminal_amount

        found = terminal_amount(text)
        token, start, end = found
        data = {"message": text, "price": token, "currency": currency}
        if cell_quantity is not None:
            data["quantity"] = cell_quantity
        row = SourceRow.objects.create(
            organization=self.org,
            run=self.run,
            index=5,
            sheet="",
            data=data,
            provenance={
                "sheet": "",
                "row": 5,
                "terminal_scope": scoped,
                "terminal": {"span": [start, end], "currency": currency,
                             "line": 5},
            },
        )
        return NormalizedOffer.objects.create(
            organization=self.org,
            run=self.run,
            source_row=row,
            supplier="Northstar",
            model_name="Phone 18 Pro",
            capacity_gb=256,
            grade="A",
            grade_raw="A",
            lock_state="unlocked",
            quantity=quantity,
            unit_price=price,
            currency=currency,
            status=OfferStatus.OK,
        )

    def gate(self):
        _passed, results = run_validations(self.run)
        return next(
            item for item in results if item.name == "roles_bound_to_evidence"
        )

    def test_bound_roles_pass(self):
        self.add_terminal_offer(Decimal("417"))
        result = self.gate()
        self.assertTrue(result.passed, result.details)

    def test_model_number_as_price_fails(self):
        self.add_terminal_offer(Decimal("16"))
        result = self.gate()
        self.assertFalse(result.passed)
        self.assertIn("unit_price", result.details["offers"][0]["fields"])

    def test_capacity_as_quantity_fails(self):
        self.add_terminal_offer(Decimal("417"), quantity=256)
        result = self.gate()
        self.assertFalse(result.passed)
        self.assertIn("quantity", result.details["offers"][0]["fields"])

    def test_marked_quantity_passes(self):
        self.add_terminal_offer(
            Decimal("417"), quantity=3, text="> Phone 18 Pro x3 - 417"
        )
        result = self.gate()
        self.assertTrue(result.passed, result.details)

    def test_decided_quantity_passes(self):
        from pipeline.models import ReviewDecision

        offer = self.add_terminal_offer(Decimal("417"), quantity=3)
        request = ReviewRequest.objects.create(
            organization=self.org,
            run=self.run,
            offer=offer,
            kind="value",
            field="quantity",
            question="q",
        )
        ReviewDecision.objects.create(
            organization=self.org, request=request,
            decided_by=self.user, value="3",
        )
        result = self.gate()
        self.assertTrue(result.passed, result.details)

    def test_unscoped_rows_ignored(self):
        self.add_terminal_offer(Decimal("16"), quantity=256, scoped=False)
        result = self.gate()
        self.assertTrue(result.passed, result.details)

    def test_corrected_cell_quantity_passes(self):
        # An accepted "out of stock means zero" correction writes the
        # cell; the offer matching its reviewed cell is not invented.
        self.add_terminal_offer(
            Decimal("269"), quantity=0, currency="EUR",
            text="Samsung Galaxy S22 with 256GB in Grade B: "
                 "out of stock at 269 EUR each",
            cell_quantity="0",
        )
        result = self.gate()
        self.assertTrue(result.passed, result.details)

    def test_cell_mismatch_still_fails(self):
        self.add_terminal_offer(
            Decimal("417"), quantity=5, cell_quantity="3",
        )
        result = self.gate()
        self.assertFalse(result.passed)
        self.assertIn("quantity", result.details["offers"][0]["fields"])
