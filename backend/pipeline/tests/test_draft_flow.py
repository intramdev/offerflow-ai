"""Versioned conversational import draft: propose, apply, import, export.

Covers the slice the correction contract requires: the user's
explanation changes the data the application imports — typed ops
persist server-side, the preview renders the confirmed draft, the
worker imports exactly that revision, and stale or unknown requests
fail explicitly.
"""

import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.jobs import _constrain_proposals
from pipeline.models import ImportRun, ReviewStatus, RunStatus
from pipeline.previews import lookup_memory, remember_mapping
from pipeline.review import answer_request, approve_run
from pipeline.tests.test_parsing import force_fixture_provider

User = get_user_model()

TRAP_TEXT = "> Phone 18 Pro 256GB - 417\n"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class DraftFlowTests(TestCase):
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
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.alice, organization=self.org, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def _paste(self, text):
        return self.client.post(
            "/api/pipeline/previews/from-text/",
            {"organization_id": self.org.id, "text": text},
            format="json",
        )

    def _apply(self, key, operations, revision=None):
        body = {"operations": operations}
        if revision is not None:
            body["revision"] = revision
        return self.client.post(
            f"/api/pipeline/previews/{key}/assist/apply/",
            body,
            format="json",
        )

    def _confirm(self, key, revision=None):
        body = {"mapping": {}}
        if revision is not None:
            body["revision"] = revision
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/pipeline/previews/{key}/confirm/",
                body,
                format="json",
            )

    def _draft(self, key):
        return self.client.get(f"/api/pipeline/previews/{key}/")

    def test_terminal_trap_slice(self):
        """Confirmed terminal price binds; capacity never becomes stock."""
        key = self._paste(TRAP_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Northstar"},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        body = applied.json()
        self.assertEqual(body["preview"]["draft"]["revision"], 2)
        sample = body["preview"]["samples"]["rows"][0]
        self.assertEqual(sample["cells"]["price"], "417")
        self.assertEqual(sample["cells"]["currency"], "USD")
        self.assertEqual(sample["cells"]["supplier"], "Northstar")

        confirm = self._confirm(key)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        offer = run.offers.get()
        # The terminal token won: the offer already carries 417 while
        # still awaiting input, and no quantity was invented.
        self.assertEqual(offer.unit_price, Decimal("417"))
        row = offer.source_row
        self.assertEqual(row.data["price"], "417")
        self.assertTrue(row.provenance.get("terminal_scope"))
        fields = set(
            run.reviews.filter(status=ReviewStatus.OPEN).values_list(
                "field", flat=True
            )
        )
        self.assertEqual(
            fields, {"model_name", "capacity_gb", "grade", "quantity"}
        )

        answers = {"model_name": "Phone 18 Pro", "capacity_gb": "256",
                   "grade": "A", "quantity": "3"}
        for review in run.reviews.filter(status=ReviewStatus.OPEN):
            answer_request(
                review=review, user=self.alice, value=answers[review.field]
            )
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertTrue(all(item.passed for item in run.validations.all()))
        offer.refresh_from_db()
        self.assertEqual(str(offer.unit_price), "417.0000")
        self.assertEqual(offer.quantity, 3)
        self.assertEqual(offer.capacity_gb, 256)

    def test_price_only_slice_omits_stock(self):
        """Price-only lists import without fabricating stock."""
        key = self._paste(TRAP_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Northstar"},
                {"op": "terminal_price", "currency": "USD"},
                {"op": "policy", "policy": "price_only"},
                {"op": "absent", "field": "quantity"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(
            applied.json()["preview"]["draft"]["document_policy"], "price_only"
        )
        confirm = self._confirm(key)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        fields = set(
            run.reviews.filter(status=ReviewStatus.OPEN).values_list(
                "field", flat=True
            )
        )
        self.assertNotIn("quantity", fields)
        for review in run.reviews.filter(status=ReviewStatus.OPEN):
            value = {"model_name": "Phone 18 Pro", "capacity_gb": "256",
                     "grade": "A"}[review.field]
            answer_request(review=review, user=self.alice, value=value)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertTrue(all(item.passed for item in run.validations.all()))
        offer = run.offers.get()
        self.assertIsNone(offer.quantity)

        approve_run(run=run, user=self.alice)
        from pipeline.quotes import build_quote, render_quote_csv

        run.refresh_from_db()
        text = render_quote_csv(
            build_quote(run), document_policy="price_only"
        ).decode("utf-8")
        self.assertNotIn("Quantity", text.splitlines()[0])

    def test_stale_revision_conflicts(self):
        key = self._paste(TRAP_TEXT).data["key"]
        first = self._apply(
            key,
            [{"op": "global", "field": "supplier", "value": "Northstar"}],
            revision=1,
        )
        self.assertEqual(first.status_code, 200, first.content)
        stale = self._apply(
            key,
            [{"op": "global", "field": "supplier", "value": "Other"}],
            revision=1,
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["revision_actual"], 2)
        draft = self._draft(key).json()["draft"]
        self.assertEqual(draft["revision"], 2)
        confirm = self._confirm(key, revision=1)
        self.assertEqual(confirm.status_code, 409)

    def test_unknown_fields_fail_explicitly(self):
        key = self._paste(TRAP_TEXT).data["key"]
        response = self.client.post(
            f"/api/pipeline/previews/{key}/assist/apply/",
            {"operations": [], "interpretation_instructions": "be nice"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        bad_op = self._apply(
            key, [{"op": "teleport", "lines": [1]}]
        )
        self.assertEqual(bad_op.status_code, 400)

    def test_draft_survives_reload(self):
        key = self._paste(TRAP_TEXT).data["key"]
        self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Northstar"},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        draft = self._draft(key)
        self.assertEqual(draft.status_code, 200, draft.content)
        body = draft.json()["draft"]
        self.assertEqual(body["revision"], 2)
        self.assertEqual(len(body["rules"]), 2)
        self.assertEqual(len(body["records"]), 1)
        self.assertEqual(body["records"][0]["id"], "L1")


class ConstrainProposalsTests(TestCase):
    def test_unmarked_quantity_dropped_with_reason(self):
        from types import SimpleNamespace

        row = SimpleNamespace(
            index=5,
            data={"message": "> Phone 18 Pro 256GB - 417"},
            provenance={"terminal_scope": True},
        )
        interpreted = {
            5: {"quantity": "256", "model": "Phone 18 Pro", "zzz": "1"}
        }
        _constrain_proposals(
            interpreted, [row],
            allowed={"quantity", "model", "price", "capacity"},
            has_marking=lambda text: False,
        )
        proposal = interpreted[5]
        self.assertNotIn("quantity", proposal)
        self.assertNotIn("zzz", proposal)
        self.assertEqual(proposal["_unsupported"], ["zzz"])
        self.assertEqual(proposal["_dropped"]["quantity"]["value"], "256")
        self.assertIn("reason", proposal["_dropped"]["quantity"])

    def test_marked_quantity_survives(self):
        from types import SimpleNamespace

        row = SimpleNamespace(
            index=5,
            data={"message": "> Phone 18 Pro x3 - 417"},
            provenance={"terminal_scope": True},
        )
        interpreted = {5: {"quantity": "3"}}
        _constrain_proposals(
            interpreted, [row],
            allowed={"quantity"},
            has_marking=lambda text: True,
        )
        self.assertEqual(interpreted[5]["quantity"], "3")
        self.assertNotIn("_dropped", interpreted[5])

    def test_unscoped_rows_untouched(self):
        from types import SimpleNamespace

        row = SimpleNamespace(
            index=5,
            data={"message": "> Phone 18 Pro 256GB - 417"},
            provenance={},
        )
        interpreted = {5: {"quantity": "256"}}
        _constrain_proposals(
            interpreted, [row],
            allowed={"quantity"},
            has_marking=lambda text: False,
        )
        self.assertEqual(interpreted[5]["quantity"], "256")


class MemoryScopingTests(TestCase):
    def test_supplier_profiles_do_not_leak(self):
        org = Organization.objects.create(name="Org")
        headers = ["message"]
        remember_mapping(
            organization=org, headers=headers, mapping={},
            constants={"supplier": "Alpha", "currency": "USD"}, ignored=[],
            supplier_hint="alpha",
        )
        remember_mapping(
            organization=org, headers=headers, mapping={},
            constants={"supplier": "Beta", "currency": "EUR"}, ignored=[],
            supplier_hint="beta",
        )
        match = lookup_memory(
            organization=org, headers=headers, supplier="beta"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.constants["supplier"], "Beta")
        self.assertEqual(match.constants["currency"], "EUR")
        self.assertIsNone(
            lookup_memory(organization=org, headers=headers, supplier="")
        )
        self.assertIsNone(
            lookup_memory(organization=org, headers=headers, supplier="gamma")
        )
