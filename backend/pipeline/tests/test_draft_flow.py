"""Versioned conversational import draft: propose, apply, import, export.

Covers the slice the correction contract requires: the user's
explanation changes the data the application imports — typed ops
persist server-side, the preview renders the confirmed draft, the
worker imports exactly that revision, and stale or unknown requests
fail explicitly.
"""

import json
import os
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import AIProviderConfig, Membership, Organization
from pipeline.jobs import _constrain_proposals
from pipeline.models import ImportRun, ReviewStatus, RunStatus
from pipeline.previews import lookup_memory, remember_mapping
from pipeline.review import answer_request, approve_run
from pipeline.spending import get_allowance, get_operator_spend
from pipeline.tests.test_parsing import force_fixture_provider
from pipeline.tests.test_spending import _FakeClient, _FakeResponse, _FakeUsage

User = get_user_model()

TRAP_TEXT = "> Phone 18 Pro 256GB - 417\n"
VEGA_TEXT = "> Vega Phone 18 Pro 256GB - 417\n"


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

    def _current_revision(self, key):
        return self._draft(key).data["draft"]["revision"]

    def _apply(self, key, operations, revision="current"):
        body = {"operations": operations}
        if revision == "current":
            revision = self._current_revision(key)
        if revision is not None:
            body["revision"] = revision
        return self.client.post(
            f"/api/pipeline/previews/{key}/assist/apply/",
            body,
            format="json",
        )

    def _confirm(self, key, revision="current"):
        body = {"mapping": {}}
        if revision == "current":
            revision = self._current_revision(key)
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

    def test_terminal_derivation_is_single_truth(self):
        key = self._paste(TRAP_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {
                    "op": "global",
                    "field": "supplier",
                    "value": "Northstar",
                },
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        preview = applied.json()["preview"]
        record = preview["draft"]["records"][0]
        price = record["fields"]["price"]
        self.assertEqual(price["value"], "417")
        self.assertEqual(price["state"], "derived")
        self.assertEqual(price["by"], "terminal L1")
        self.assertIsNotNone(price["rule"])
        self.assertEqual(price["line"], 1)
        start, end = price["span"]
        line = TRAP_TEXT.splitlines()[0]
        self.assertEqual(line[start:end], "417")
        currency = record["fields"]["currency"]
        self.assertEqual(currency["value"], "USD")
        self.assertEqual(currency["state"], "derived")
        supplier = record["fields"]["supplier"]
        self.assertEqual(supplier["value"], "Northstar")
        self.assertEqual(supplier["state"], "user_confirmed")
        unresolved = {
            (item.get("record"), item.get("field"))
            for item in preview["draft"]["unresolved"]
        }
        self.assertNotIn((record["id"], "price"), unresolved)
        sample = next(
            row
            for row in preview["samples"]["rows"]
            if row["record"] == record["id"]
        )
        self.assertEqual(sample["cells"]["price"], "417")
        self.assertEqual(sample["cells"]["currency"], "USD")
        self.assertEqual(sample["terminal"]["line"], 1)

    def test_unread_price_agrees_across_views(self):
        text = "Northstar pricelist\nNo numbers here\n"
        key = self._paste(text).data["key"]
        applied = self._apply(
            key, [{"op": "terminal_price", "currency": "USD"}]
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        preview = applied.json()["preview"]
        samples = {
            row["record"]: row for row in preview["samples"]["rows"]
        }
        unresolved = {
            (item.get("record"), item.get("field"))
            for item in preview["draft"]["unresolved"]
        }
        self.assertTrue(preview["draft"]["records"])
        for record in preview["draft"]["records"]:
            price = record["fields"]["price"]
            if price["state"] == "unknown":
                self.assertIn((record["id"], "price"), unresolved)
                self.assertNotIn("price", samples[record["id"]]["cells"])
                self.assertIsNone(samples[record["id"]]["terminal"])
            else:
                self.assertEqual(price["state"], "derived")
                self.assertNotIn((record["id"], "price"), unresolved)
                self.assertEqual(
                    samples[record["id"]]["cells"]["price"],
                    price["value"],
                )

    def test_rule_removal_removes_effect(self):
        key = self._paste(TRAP_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {
                    "op": "global",
                    "field": "supplier",
                    "value": "REMOVE_ME",
                },
                {"op": "global", "field": "grade", "value": "B"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        preview = applied.json()["preview"]
        self.assertEqual(
            preview["proposed_constants"]["supplier"], "REMOVE_ME"
        )
        self.assertEqual(
            preview["samples"]["rows"][0]["cells"]["supplier"],
            "REMOVE_ME",
        )
        rules = {rule["id"]: rule for rule in preview["draft"]["rules"]}
        supplier_rule = next(
            rule_id
            for rule_id, rule in rules.items()
            if rule["field"] == "supplier"
        )
        grade_rule = next(
            rule_id
            for rule_id, rule in rules.items()
            if rule["field"] == "grade"
        )

        replaced = self._apply(
            key,
            [
                {"op": "drop_rule", "rule": supplier_rule},
                {
                    "op": "global",
                    "field": "supplier",
                    "value": "Northstar",
                },
                {"op": "drop_rule", "rule": grade_rule},
                {"op": "terminal_price", "currency": "USD"},
            ],
            revision=2,
        )
        self.assertEqual(replaced.status_code, 200, replaced.content)
        preview = replaced.json()["preview"]
        values = [
            (rule["field"], rule["value"])
            for rule in preview["draft"]["rules"]
            if rule["kind"] == "global"
        ]
        self.assertEqual(values, [("supplier", "Northstar")])
        self.assertEqual(
            preview["proposed_constants"],
            {"supplier": "Northstar"},
        )
        sample = preview["samples"]["rows"][0]["cells"]
        self.assertEqual(sample["supplier"], "Northstar")
        self.assertNotIn("grade", sample)
        self.assertEqual(sample["price"], "417")
        record = preview["draft"]["records"][0]
        self.assertEqual(
            record["fields"]["grade"]["state"], "unknown"
        )
        self.assertIn(
            (record["id"], "grade"),
            {
                (item.get("record"), item.get("field"))
                for item in preview["draft"]["unresolved"]
            },
        )
        # Refresh agrees with the apply response.
        refreshed = self._draft(key).data["draft"]
        self.assertEqual(refreshed["rules"], preview["draft"]["rules"])
        self.assertEqual(
            refreshed["records"][0]["fields"]["supplier"]["value"],
            "Northstar",
        )

        terminal_rule = next(
            rule["id"]
            for rule in preview["draft"]["rules"]
            if rule["kind"] == "terminal_price"
        )
        dropped = self._apply(
            key, [{"op": "drop_rule", "rule": terminal_rule}], revision=3
        )
        self.assertEqual(dropped.status_code, 200, dropped.content)
        preview = dropped.json()["preview"]
        record = preview["draft"]["records"][0]
        self.assertEqual(record["fields"]["price"]["state"], "unknown")
        self.assertNotIn(
            "price", preview["samples"]["rows"][0]["cells"]
        )

        confirm = self._confirm(key)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        for review in run.reviews.filter(status=ReviewStatus.OPEN):
            value = {
                "model_name": "Phone 18 Pro",
                "capacity_gb": "256",
                "grade": "A",
                "unit_price": "400",
                "quantity": "3",
            }[review.field]
            answer_request(review=review, user=self.alice, value=value)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        offer = run.offers.get()
        self.assertEqual(offer.supplier, "Northstar")
        self.assertEqual(offer.unit_price, Decimal("400"))
        self.assertNotIn("REMOVE_ME", offer.supplier)

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

    def test_apply_and_confirm_require_revision_render_stays_open(self):
        key = self._paste(TRAP_TEXT).data["key"]
        no_revision_apply = self.client.post(
            f"/api/pipeline/previews/{key}/assist/apply/",
            {"operations": []},
            format="json",
        )
        self.assertEqual(no_revision_apply.status_code, 400)
        self.assertIn("revision", no_revision_apply.json()["detail"])
        no_revision_confirm = self.client.post(
            f"/api/pipeline/previews/{key}/confirm/",
            {"mapping": {}},
            format="json",
        )
        self.assertEqual(no_revision_confirm.status_code, 400)
        self.assertIn("revision", no_revision_confirm.json()["detail"])
        # Render is the read-only legacy path: no revision needed.
        rendered = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {}},
            format="json",
        )
        self.assertEqual(rendered.status_code, 200, rendered.content)

    def test_stock_policy_rejects_retained_absent_quantity(self):
        key = self._paste(TRAP_TEXT).data["key"]
        setup = self._apply(
            key,
            [
                {"op": "policy", "policy": "price_only"},
                {"op": "absent", "field": "quantity"},
            ],
        )
        self.assertEqual(setup.status_code, 200, setup.content)
        stranded = self._apply(key, [{"op": "policy", "policy": "stock"}])
        self.assertEqual(stranded.status_code, 400)
        self.assertIn("absent", stranded.json()["detail"])
        rules = self._draft(key).data["draft"]["rules"]
        absent_id = next(
            rule["id"] for rule in rules if rule["kind"] == "absent"
        )
        coherent = self._apply(
            key,
            [
                {"op": "drop_rule", "rule": absent_id},
                {"op": "policy", "policy": "stock"},
            ],
        )
        self.assertEqual(coherent.status_code, 200, coherent.content)
        draft = coherent.json()["preview"]["draft"]
        self.assertEqual(draft["document_policy"], "stock")
        self.assertFalse(
            [rule for rule in draft["rules"] if rule["kind"] == "absent"]
        )

    def _pending(self, org_id):
        return self.client.get(
            f"/api/pipeline/previews/pending/?organization_id={org_id}"
        )

    def test_pending_list_and_resume_restore_editor_state(self):
        key = self._paste(TRAP_TEXT).data["key"]
        self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Northstar"},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        pending = self._pending(self.org.id)
        self.assertEqual(pending.status_code, 200, pending.content)
        items = pending.json()["previews"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], key)
        self.assertEqual(items[0]["revision"], 2)
        self.assertEqual(items[0]["rules_count"], 2)
        self.assertEqual(items[0]["document_policy"], "stock")
        self.assertTrue(items[0]["message_lines"])
        # Metadata only: no source bytes, cells, or mapping positions.
        self.assertEqual(
            set(items[0].keys()),
            {
                "key",
                "filename",
                "created_at",
                "revision",
                "document_policy",
                "rules_count",
                "excluded_count",
                "message_lines",
                "sheet",
            },
        )
        resumed = self.client.get(f"/api/pipeline/previews/{key}/resume/")
        self.assertEqual(resumed.status_code, 200, resumed.content)
        body = resumed.json()
        self.assertEqual(body["key"], key)
        self.assertEqual(body["draft"]["revision"], 2)
        self.assertEqual(len(body["draft"]["rules"]), 2)
        self.assertEqual(len(body["draft"]["records"]), 1)
        sample = body["samples"]["rows"][0]
        self.assertEqual(sample["cells"]["price"], "417")
        self.assertEqual(sample["cells"]["currency"], "USD")
        self.assertEqual(sample["cells"]["supplier"], "Northstar")
        confirmed = self._confirm(key)
        self.assertEqual(confirmed.status_code, 201, confirmed.content)
        self.assertEqual(self._pending(self.org.id).json()["previews"], [])

    def test_pending_scoped_to_members_and_expiry(self):
        key = self._paste(TRAP_TEXT).data["key"]
        stranger = User.objects.create_user(username="stranger")
        self.client.force_authenticate(user=stranger)
        denied = self._pending(self.org.id)
        self.assertEqual(denied.status_code, 403)
        gone = self.client.get(f"/api/pipeline/previews/{key}/resume/")
        self.assertEqual(gone.status_code, 403)
        self.client.force_authenticate(user=self.alice)
        other = Organization.objects.create(name="Other")
        foreign = self._pending(other.id)
        self.assertEqual(foreign.status_code, 403)
        # Backdate past the TTL: the draft is collected, not listed.
        meta_path = os.path.join(
            self.tmp.name, "previews", f"{key}.json"
        )
        with open(meta_path) as handle:
            meta = json.load(handle)
        meta["created_at"] = "2020-01-01T00:00:00+00:00"
        with open(meta_path, "w") as handle:
            json.dump(meta, handle)
        self.assertEqual(self._pending(self.org.id).json()["previews"], [])
        self.assertFalse(os.path.exists(meta_path))
        expired = self.client.get(f"/api/pipeline/previews/{key}/resume/")
        self.assertEqual(expired.status_code, 400)
        self.assertIn("expired", expired.json()["detail"])


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

    def test_memory_write_collision_skips_without_failing_import(self):
        from django.db import IntegrityError

        from pipeline.models import ColumnMappingMemory

        org = Organization.objects.create(name="Org")
        headers = ["message"]
        remember_mapping(
            organization=org, headers=headers, mapping={},
            constants={"supplier": "Alpha"}, ignored=[],
            supplier_hint="alpha",
        )
        with mock.patch.object(
            ColumnMappingMemory.objects,
            "get_or_create",
            side_effect=IntegrityError("legacy unique_org_mapping_memory"),
        ):
            # A pre-0029 schema collides on the second supplier; the
            # import must proceed and the stored row must stay intact.
            self.assertIsNone(
                remember_mapping(
                    organization=org, headers=headers, mapping={},
                    constants={"supplier": "Beta"}, ignored=[],
                    supplier_hint="beta",
                )
            )
        match = lookup_memory(
            organization=org, headers=headers, supplier="alpha"
        )
        self.assertEqual(match.constants["supplier"], "Alpha")
        self.assertEqual(ColumnMappingMemory.objects.count(), 1)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class AssistPriceOnlySliceTests(DraftFlowTests):
    """Price-only as one conversational transaction through the real adapter.

    The stub sits at the SDK transport: raw provider JSON travels the
    real response-parsing, fold, validation, Apply, preview, import and
    output path. A mock above the adapter would miss the integration.
    """

    def _paid_env(self):
        return mock.patch.dict(
            os.environ,
            {
                "OFFERFLOW_PROVIDER": "meta",
                "OFFERFLOW_ALLOW_PAID": "true",
                "OFFERFLOW_META_API_KEY": "LLM_9_envkey",
                "OFFERFLOW_META_MODEL": "env-model",
            },
            clear=False,
        )

    def _allow(self, budget):
        allowance = get_allowance(self.org.id)
        allowance.token_budget = budget
        allowance.monthly_credits = budget
        allowance.save()
        operator = get_operator_spend()
        if budget > operator.monthly_ceiling:
            operator.monthly_ceiling = budget
            operator.save(update_fields=["monthly_ceiling"])

    def test_policy_transaction_through_real_adapter(self):
        AIProviderConfig.objects.create(model="admin-model")
        self._allow(200000)
        key = self._paste(VEGA_TEXT).data["key"]
        body = json.dumps(
            {
                "message": "Switching to price-only; terminal numbers "
                "are USD prices.",
                "set_constants": [
                    {"name": "supplier", "value": "Vega Supply"}
                ],
                "drop_lines": [],
                "policy": [{"policy": "price_only"}],
                "classify": [],
                "section_value": [],
                "terminal_price_document": [{"currency": "USD"}],
                "terminal_price_section": [],
                "join_lines": [],
                "correct": [],
                "absent": [{"field": "quantity"}],
            }
        )
        script = _FakeResponse(body, _FakeUsage(120, 80))
        holder = {}

        def _factory(config):
            holder["client"] = _FakeClient(script)
            return holder["client"]

        with self._paid_env(), mock.patch(
            "common.meta._default_client", side_effect=_factory
        ):
            response = self.client.post(
                f"/api/pipeline/previews/{key}/assist/",
                {"messages": [
                    {"role": "user",
                     "content": "Final numbers are USD prices."}
                ]},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        wire = holder["client"].chat.completions.calls[0]
        wire_format = wire["response_format"]["json_schema"]
        self.assertTrue(wire_format["strict"])
        self.assertIn("policy", wire_format["schema"]["properties"])
        proposal = response.json()["proposal"]
        self.assertEqual(
            proposal["operations"],
            [
                {"op": "policy", "policy": "price_only"},
                {"op": "terminal_price", "currency": "USD"},
                {"op": "absent", "field": "quantity"},
            ],
        )
        self.assertEqual(
            proposal["set_constants"], {"supplier": "Vega Supply"}
        )
        self.assertEqual(response.json()["tokens"], 200)
        # The model's guess is shown, not silently activated.
        draft = self._draft(key).data["draft"]
        self.assertEqual(draft["document_policy"], "stock")
        self.assertEqual(draft["revision"], 1)

        applied = self.client.post(
            f"/api/pipeline/previews/{key}/assist/apply/",
            {
                "operations": proposal["operations"],
                "set_constants": proposal["set_constants"],
                "drop_lines": [],
                "revision": 1,
            },
            format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        preview = applied.json()["preview"]
        self.assertEqual(
            preview["draft"]["document_policy"], "price_only"
        )
        cells = [
            row["cells"] for row in preview["samples"]["rows"]
        ]
        self.assertIn("417", [cell.get("price") for cell in cells])
        self.assertIn("USD", [cell.get("currency") for cell in cells])

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
            value = {
                "model_name": "Vega Phone 18 Pro",
                "capacity_gb": "256",
                "grade": "A",
            }[review.field]
            answer_request(review=review, user=self.alice, value=value)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertTrue(all(item.passed for item in run.validations.all()))
        offer = run.offers.get()
        self.assertEqual(offer.unit_price, Decimal("417"))
        self.assertIsNone(offer.quantity)

        approve_run(run=run, user=self.alice)
        from pipeline.quotes import build_quote, render_quote_csv

        run.refresh_from_db()
        text = render_quote_csv(
            build_quote(run), document_policy="price_only"
        ).decode("utf-8")
        self.assertNotIn("Quantity", text.splitlines()[0])
        self.assertIn("417", text)
