"""Cross-supplier product matching: blocking, lanes, memory, learning."""

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from common.meta import (
    MatchUnusable,
    MetaConfig,
    MetaProvider,
    ProviderError,
)
from common.provider import FixtureProvider
from organizations.models import Membership, Organization
from pipeline.matching import (
    normalize_alias,
    normalize_brand,
    normalize_grade,
    normalize_supplier,
    propose_groups,
    remember_decisions,
)
from pipeline.models import (
    Category,
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    ProductAlias,
    RuleSetVersion,
    RunStatus,
    SourceFile,
    SourceRow,
    UsageEvent,
)
from pipeline.spending import get_allowance

User = get_user_model()


class NormalizeTests(SimpleTestCase):
    def test_alias_ignores_case_and_punctuation(self):
        self.assertEqual(normalize_alias("iPhone 15"), "iphone15")
        self.assertEqual(normalize_alias("IPHONE-15"), "iphone15")
        self.assertEqual(normalize_alias("  Iphone15 "), "iphone15")

    def test_alias_keeps_variants_apart(self):
        self.assertNotEqual(normalize_alias("iPhone 15"), normalize_alias("iPhone 15 Pro"))

    def test_alias_rejects_non_text(self):
        self.assertEqual(normalize_alias(None), "")
        self.assertEqual(normalize_alias(128), "")

    def test_brand_and_supplier_exact_up_to_case(self):
        self.assertEqual(normalize_brand(" Apple "), "apple")
        self.assertEqual(normalize_supplier("ACME s.r.o."), "acme s.r.o.")
        self.assertEqual(normalize_brand(None), "")

    def test_grade_exact_up_to_case(self):
        self.assertEqual(normalize_grade(" A "), "a")
        self.assertEqual(normalize_grade("Refurbished"), "refurbished")
        self.assertEqual(normalize_grade(None), "")
        self.assertEqual(normalize_grade(128), "")


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content, usage):
        message = mock.Mock()
        message.content = content
        choice = mock.Mock()
        choice.message = message
        self.choices = [choice]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.script, Exception):
            raise self.script
        return self.script


class _FakeClient:
    def __init__(self, script):
        self.chat = mock.Mock()
        self.chat.completions = _FakeCompletions(script)


class _StubProvider:
    """Scripted stand-in for the model lane (no network)."""

    name = "meta"
    is_simulated = False

    def __init__(self, script=None, error=None, decisions=None, categorize_error=None):
        self.script = script if script is not None else []
        self.error = error
        self.decisions = decisions if decisions is not None else []
        self.categorize_error = categorize_error
        self.calls = []
        self.categorize_calls = []

    def match_products(self, payload, on_chunk=None):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        if on_chunk is not None:
            on_chunk(1, 1, 10, 5)
        return {
            "simulated": False,
            "provider": "meta",
            "model": "stub",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "clusters": self.script,
        }

    def categorize(self, payload, on_chunk=None):
        self.categorize_calls.append(payload)
        if self.categorize_error is not None:
            raise self.categorize_error
        if on_chunk is not None:
            on_chunk(1, 1, 10, 5)
        return {
            "simulated": False,
            "provider": "meta",
            "model": "stub",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            "decisions": self.decisions,
        }


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MatchingCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
        self.rule_version = RuleSetVersion.objects.get(version="v1")
        self._row_index = 0

    def _run(self, org=None):
        org = org or self.org
        source_file = SourceFile.objects.create(
            organization=org,
            uploaded_by=self.user,
            original_name="s.xlsx",
            size_bytes=10,
            sha256=("ab" * 31) + f"{SourceFile.objects.count():02d}",
            storage_key=f"org/{org.id}/{SourceFile.objects.count()}",
        )
        return ImportRun.objects.create(
            organization=org,
            source_file=source_file,
            rule_version=self.rule_version,
            status=RunStatus.APPROVED,
        )

    def _category(self, name, org=None):
        return Category.objects.create(
            organization=org or self.org, name=name
        )

    def _file_category(self, run, category):
        run.source_file.category = category
        run.source_file.save(update_fields=["category"])

    def _alias(self, category=None, **fields):
        defaults = {
            "organization": self.org,
            "supplier_norm": "a",
            "alias_norm": "iphone15",
            "brand_norm": "apple",
            "capacity_gb": 128,
            "grade_norm": "a",
            "canonical_model": "iPhone 15",
            "category": category,
        }
        defaults.update(fields)
        return ProductAlias.objects.create(**defaults)

    def _offer(self, run, **fields):
        self._row_index += 1
        row = SourceRow.objects.create(
            organization=self.org,
            run=run,
            index=self._row_index,
            sheet="Sheet1",
            data={},
            provenance={"sheet": "Sheet1", "row": self._row_index},
        )
        defaults = {
            "supplier": "Supplier A",
            "brand": "Apple",
            "model_name": "iPhone 15",
            "capacity_gb": 128,
            "grade": "A",
            "quantity": 5,
            "unit_price": Decimal("100.0000"),
            "currency": "USD",
            "status": OfferStatus.OK,
        }
        defaults.update(fields)
        org = fields.pop("organization", self.org)
        return NormalizedOffer.objects.create(
            organization=org, run=run, source_row=row, **defaults
        )


class SimulatedProposeTests(MatchingCase):
    def test_normalized_spellings_auto_group(self):
        run = self._run()
        first = self._offer(run, supplier="A", model_name="iPhone 15")
        second = self._offer(run, supplier="B", model_name="IPHONE-15")
        result = propose_groups(organization=self.org, offers=[first, second])
        self.assertTrue(result["simulated"])
        self.assertFalse(result["needs_review"])
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["source"], "normalized")
        self.assertEqual(group["canonical_model"], "iPhone 15")
        self.assertEqual(sorted(group["offers"]), sorted([first.id, second.id]))
        by_id = {member["id"]: member for member in group["members"]}
        self.assertEqual(by_id[first.id]["model_name"], "iPhone 15")
        self.assertEqual(by_id[second.id]["supplier"], "B")
        self.assertEqual(by_id[second.id]["currency"], "USD")

    def test_capacity_blocks_apart(self):
        run = self._run()
        small = self._offer(run, model_name="iPhone 15", capacity_gb=128)
        big = self._offer(run, model_name="iPhone 15", capacity_gb=256)
        result = propose_groups(organization=self.org, offers=[small, big])
        self.assertEqual(len(result["groups"]), 2)
        capacities = sorted(g["capacity_gb"] for g in result["groups"])
        self.assertEqual(capacities, [128, 256])

    def test_brand_blocks_apart(self):
        run = self._run()
        apple = self._offer(run, brand="Apple", model_name="P1")
        pear = self._offer(run, brand="Pear", model_name="P1")
        result = propose_groups(organization=self.org, offers=[apple, pear])
        self.assertEqual(len(result["groups"]), 2)

    def test_unknown_spellings_stay_provisional(self):
        run = self._run()
        first = self._offer(run, supplier="A", model_name="iPhone 15")
        second = self._offer(run, supplier="B", model_name="Apple 15 phone")
        result = propose_groups(organization=self.org, offers=[first, second])
        self.assertTrue(result["needs_review"])
        self.assertEqual(len(result["groups"]), 2)
        for group in result["groups"]:
            self.assertEqual(group["source"], "provisional")
            self.assertTrue(group["needs_review"])
        self.assertTrue(any("reconcile" in note for note in result["notes"]))

    def test_missing_capacity_or_model_flagged(self):
        run = self._run()
        nocap = self._offer(run, model_name="iPhone 15", capacity_gb=None)
        nomodel = self._offer(run, model_name="", capacity_gb=128)
        result = propose_groups(organization=self.org, offers=[nocap, nomodel])
        self.assertTrue(result["needs_review"])
        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(any("no capacity" in note for note in result["notes"]))
        self.assertTrue(any("no model name" in note for note in result["notes"]))

    def test_simulated_records_no_spend(self):
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="Apple 15 phone"),
        ]
        propose_groups(organization=self.org, offers=offers)
        self.assertEqual(UsageEvent.objects.count(), 0)

    def test_memory_rematches_same_supplier_spelling(self):
        run = self._run()
        ProductAlias.objects.create(
            organization=self.org,
            supplier_norm="a",
            alias_norm="iphone15",
            brand_norm="apple",
            capacity_gb=128,
            grade_norm="a",
            canonical_model="iPhone 15",
        )
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        result = propose_groups(organization=self.org, offers=[offer])
        self.assertFalse(result["needs_review"])
        group = result["groups"][0]
        self.assertEqual(group["source"], "memory")
        self.assertEqual(group["canonical_model"], "iPhone 15")

    def test_new_spelling_joins_known_canonical_by_normalization(self):
        run = self._run()
        ProductAlias.objects.create(
            organization=self.org,
            supplier_norm="a",
            alias_norm="iphone15",
            brand_norm="apple",
            capacity_gb=128,
            canonical_model="iPhone 15",
        )
        known = self._offer(run, supplier="A", model_name="iPhone 15")
        fresh = self._offer(run, supplier="B", model_name="IPHONE 15")
        result = propose_groups(organization=self.org, offers=[known, fresh])
        self.assertFalse(result["needs_review"])
        self.assertEqual(len(result["groups"]), 1)

    def test_memory_stays_inside_its_company(self):
        other = Organization.objects.create(name="Other")
        ProductAlias.objects.create(
            organization=other,
            supplier_norm="a",
            alias_norm="iphone15",
            brand_norm="apple",
            capacity_gb=128,
            canonical_model="iPhone 15",
        )
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        result = propose_groups(organization=self.org, offers=[offer])
        self.assertEqual(result["groups"][0]["source"], "provisional")


class PaidProposeTests(MatchingCase):
    def _propose(self, offers, stub):
        with mock.patch(
            "pipeline.matching.get_provider", return_value=stub
        ):
            return propose_groups(organization=self.org, offers=offers)

    def test_confident_cluster_auto_groups_and_logs_spend(self):
        run = self._run()
        first = self._offer(run, supplier="A", model_name="iPhone 15")
        second = self._offer(run, supplier="B", model_name="Apple iPhone 15")
        stub = _StubProvider(
            script=[
                {"canonical": "iPhone 15", "members": [0, 1], "confidence": 0.95}
            ]
        )
        result = self._propose([first, second], stub)
        self.assertFalse(result["needs_review"])
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(
            [s["raw"] for s in stub.calls[0]["spellings"]],
            ["iPhone 15", "Apple iPhone 15"],
        )
        group = result["groups"][0]
        self.assertEqual(group["source"], "llm")
        self.assertEqual(group["canonical_model"], "iPhone 15")
        self.assertEqual(group["confidence"], 0.95)
        event = UsageEvent.objects.get(kind="provider_call")
        self.assertIsNone(event.run)
        self.assertEqual(event.quantity, 15)
        self.assertEqual(event.detail["task"], "match")
        self.assertEqual(event.detail["prompt_tokens"], 10)
        self.assertEqual(event.detail["completion_tokens"], 5)

    def test_low_confidence_stays_provisional_with_suggestion(self):
        run = self._run()
        first = self._offer(run, supplier="A", model_name="iPhone 15")
        second = self._offer(run, supplier="B", model_name="Apple iPhone 15")
        stub = _StubProvider(
            script=[
                {"canonical": "iPhone 15", "members": [0, 1], "confidence": 0.4}
            ]
        )
        result = self._propose([first, second], stub)
        self.assertTrue(result["needs_review"])
        self.assertEqual(len(result["groups"]), 2)
        for group in result["groups"]:
            self.assertEqual(group["source"], "provisional")
            self.assertEqual(group["suggestion"], "iPhone 15")
        self.assertTrue(any("unsure" in note for note in result["notes"]))

    def test_invented_canonical_rejected(self):
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="Galaxy S24"),
        ]
        stub = _StubProvider(
            script=[{"canonical": "Pixel 8", "members": [0, 1], "confidence": 0.99}]
        )
        result = self._propose(offers, stub)
        self.assertTrue(result["needs_review"])
        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(all(g["source"] == "provisional" for g in result["groups"]))
        self.assertTrue(any("invented" in note for note in result["notes"]))

    def test_double_booking_and_omission_flagged(self):
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="Galaxy S24"),
        ]
        stub = _StubProvider(
            script=[
                {"canonical": "iPhone 15", "members": [0], "confidence": 0.95},
                {"canonical": "iPhone 15", "members": [0, 1], "confidence": 0.95},
            ]
        )
        result = self._propose(offers, stub)
        self.assertTrue(result["needs_review"])
        self.assertTrue(any("double-booked" in note for note in result["notes"]))
        placed = sorted(offer for group in result["groups"] for offer in group["offers"])
        self.assertEqual(placed, sorted(o.id for o in offers))

    def test_unusable_proposal_keeps_everything_provisional(self):
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="Apple iPhone 15"),
        ]
        stub = _StubProvider(error=MatchUnusable("Match returned empty content."))
        result = self._propose(offers, stub)
        self.assertTrue(result["needs_review"])
        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(any("unusable" in note for note in result["notes"]))

    def test_lone_spelling_skips_the_call(self):
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        stub = _StubProvider(script=[])
        result = self._propose([offer], stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(result["groups"][0]["source"], "provisional")


class RememberTests(MatchingCase):
    def test_confirm_learns_and_rematches(self):
        run = self._run()
        first = self._offer(run, supplier="A", model_name="iPhone 15")
        second = self._offer(run, supplier="B", model_name="Apple iPhone 15")
        outcome = remember_decisions(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [first.id, second.id]}],
            expected_offer_ids=[first.id, second.id],
        )
        self.assertEqual(outcome, {"groups": 1, "remembered": 2})
        self.assertEqual(ProductAlias.objects.count(), 2)
        again = propose_groups(organization=self.org, offers=[first, second])
        self.assertFalse(again["needs_review"])
        self.assertEqual(again["groups"][0]["source"], "memory")

    def test_cross_block_merge_refused(self):
        run = self._run()
        small = self._offer(run, model_name="iPhone 15", capacity_gb=128)
        big = self._offer(run, model_name="iPhone 15", capacity_gb=256)
        with self.assertRaises(ValidationError) as ctx:
            remember_decisions(
                organization=self.org,
                decisions=[
                    {"canonical_model": "iPhone 15", "offer_ids": [small.id, big.id]}
                ],
                expected_offer_ids=[small.id, big.id],
            )
        self.assertIn("capacities", str(ctx.exception))

    def test_coverage_exactly_once(self):
        run = self._run()
        first = self._offer(run, model_name="iPhone 15")
        second = self._offer(run, model_name="Galaxy S24")
        with self.assertRaises(ValidationError) as ctx:
            remember_decisions(
                organization=self.org,
                decisions=[{"canonical_model": "iPhone 15", "offer_ids": [first.id]}],
                expected_offer_ids=[first.id, second.id],
            )
        self.assertIn("unplaced", str(ctx.exception))
        with self.assertRaises(ValidationError):
            remember_decisions(
                organization=self.org,
                decisions=[
                    {"canonical_model": "iPhone 15", "offer_ids": [first.id]},
                    {"canonical_model": "Galaxy S24", "offer_ids": [first.id, second.id]},
                ],
                expected_offer_ids=[first.id, second.id],
            )
        with self.assertRaises(ValidationError):
            remember_decisions(
                organization=self.org,
                decisions=[{"canonical_model": "  ", "offer_ids": [first.id, second.id]}],
                expected_offer_ids=[first.id, second.id],
            )

    def test_non_ok_offer_rejected(self):
        run = self._run()
        pending = self._offer(run, model_name="iPhone 15", status=OfferStatus.NEEDS_INPUT)
        with self.assertRaises(ValidationError) as ctx:
            remember_decisions(
                organization=self.org,
                decisions=[{"canonical_model": "iPhone 15", "offer_ids": [pending.id]}],
                expected_offer_ids=[pending.id],
            )
        self.assertIn("Unknown offers", str(ctx.exception))

    def test_unverifiable_members_group_but_not_remembered(self):
        run = self._run()
        nocap = self._offer(run, model_name="iPhone 15", capacity_gb=None)
        outcome = remember_decisions(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [nocap.id]}],
            expected_offer_ids=[nocap.id],
        )
        self.assertEqual(outcome["remembered"], 0)
        self.assertEqual(ProductAlias.objects.count(), 0)

    def test_reconfirm_updates_canonical_and_counts_use(self):
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="IPHONE15")
        remember_decisions(
            organization=self.org,
            decisions=[{"canonical_model": "IPHONE15", "offer_ids": [offer.id]}],
            expected_offer_ids=[offer.id],
        )
        remember_decisions(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [offer.id]}],
            expected_offer_ids=[offer.id],
        )
        alias = ProductAlias.objects.get()
        self.assertEqual(alias.canonical_model, "iPhone 15")
        self.assertEqual(alias.use_count, 2)


class FixtureMatchTests(SimpleTestCase):
    def test_fixture_echoes_zero_confidence_singletons(self):
        result = FixtureProvider().match_products(
            {"spellings": [{"supplier": ["A"], "raw": "iPhone 15"}]}
        )
        self.assertTrue(result["simulated"])
        self.assertEqual(
            result["clusters"],
            [{"canonical": "iPhone 15", "members": [0], "confidence": 0.0}],
        )
        self.assertEqual(result["usage"]["total_tokens"], 0)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MetaMatchWireTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")

    def _provider(self, script, **overrides):
        options = {
            "organization_id": self.org.id,
            "api_key": "LLM_9_testsecret",
            "model": "test-model",
        }
        options.update(overrides)

        def factory(config):
            return _FakeClient(script)

        return MetaProvider(MetaConfig(**options), client_factory=factory)

    def _allow(self, credits=100000):
        allowance = get_allowance(self.org.id)
        allowance.token_budget = credits
        allowance.monthly_credits = credits
        allowance.save()
        return allowance

    def _payload(self):
        return {
            "block": {"brand": "apple", "capacity_gb": 128},
            "known": [],
            "spellings": [
                {"supplier": ["A"], "raw": "iPhone 15"},
                {"supplier": ["B"], "raw": "Apple iPhone 15"},
            ],
        }

    def test_match_returns_clusters_and_spends(self):
        self._allow()
        body = json.dumps(
            {"clusters": [{"canonical": "iPhone 15", "members": [0, 1], "confidence": 0.9}]}
        )
        provider = self._provider(_FakeResponse(body, _FakeUsage(12, 7)))
        chunks = []
        result = provider.match_products(
            self._payload(),
            on_chunk=lambda *args: chunks.append(args),
        )
        self.assertFalse(result["simulated"])
        self.assertEqual(len(result["clusters"]), 1)
        self.assertEqual(result["usage"]["total_tokens"], 19)
        self.assertEqual(chunks, [(1, 1, 12, 7)])
        allowance = get_allowance(self.org.id)
        self.assertEqual(allowance.period_spent, 19)

    def test_match_rejects_empty_payload(self):
        self._allow()
        provider = self._provider(_FakeResponse("{}", _FakeUsage(1, 1)))
        with self.assertRaises(ProviderError):
            provider.match_products({"spellings": []})

    def test_match_bad_json_is_unusable_not_transport(self):
        self._allow()
        provider = self._provider(_FakeResponse("nope", _FakeUsage(12, 7)))
        with self.assertRaises(MatchUnusable):
            provider.match_products(self._payload())

    def test_match_budget_refused(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 0
        allowance.save(update_fields=["monthly_credits"])
        provider = self._provider(_FakeResponse("{}", _FakeUsage(1, 1)))
        with self.assertRaises(ProviderError) as ctx:
            provider.match_products(self._payload())
        self.assertIn("Budget refused", str(ctx.exception))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class GradeBlockingTests(MatchingCase):
    def test_different_grades_stay_apart(self):
        run = self._run()
        new = self._offer(run, supplier="A", model_name="iPhone 15", grade="A")
        worn = self._offer(run, supplier="B", model_name="iPhone 15", grade="B")
        result = propose_groups(organization=self.org, offers=[new, worn])
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(
            sorted(group["grade"] for group in result["groups"]), ["A", "B"]
        )

    def test_missing_grade_stays_apart_from_graded(self):
        run = self._run()
        graded = self._offer(run, supplier="A", model_name="iPhone 15", grade="A")
        plain = self._offer(run, supplier="B", model_name="iPhone 15", grade="")
        result = propose_groups(organization=self.org, offers=[graded, plain])
        self.assertEqual(len(result["groups"]), 2)

    def test_memory_requires_the_same_grade(self):
        run = self._run()
        self._alias()
        same = self._offer(run, supplier="A", model_name="iPhone 15", grade="A")
        other = self._offer(run, supplier="A", model_name="iPhone 15", grade="B")
        result = propose_groups(organization=self.org, offers=[same, other])
        by_offer = {
            group["offers"][0]: group for group in result["groups"]
        }
        self.assertEqual(by_offer[same.id]["source"], "memory")
        self.assertEqual(by_offer[other.id]["source"], "provisional")

    def test_remember_refuses_mixed_grades(self):
        run = self._run()
        new = self._offer(run, model_name="iPhone 15", grade="A")
        worn = self._offer(run, model_name="iPhone 15", grade="B")
        with self.assertRaises(ValidationError) as ctx:
            remember_decisions(
                organization=self.org,
                decisions=[
                    {"canonical_model": "iPhone 15", "offer_ids": [new.id, worn.id]}
                ],
                expected_offer_ids=[new.id, worn.id],
            )
        self.assertIn("grades", str(ctx.exception))

    def test_remember_stores_the_grade(self):
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="iPhone 15", grade="B")
        remember_decisions(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [offer.id]}],
            expected_offer_ids=[offer.id],
        )
        alias = ProductAlias.objects.get()
        self.assertEqual(alias.grade_norm, "b")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CategoryResolveTests(MatchingCase):
    def test_no_categories_stays_quiet(self):
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="IPHONE-15"),
        ]
        result = propose_groups(organization=self.org, offers=offers)
        self.assertFalse(result["needs_review"])
        group = result["groups"][0]
        self.assertIsNone(group["category_id"])
        self.assertEqual(group["category_source"], "none")
        self.assertFalse(group["category_review"])

    def test_unanimous_file_mapping_wins(self):
        phones = self._category("Phones")
        run = self._run()
        self._file_category(run, phones)
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="IPHONE-15"),
        ]
        result = propose_groups(organization=self.org, offers=offers)
        self.assertFalse(result["needs_review"])
        group = result["groups"][0]
        self.assertEqual(group["category_id"], phones.id)
        self.assertEqual(group["category_name"], "Phones")
        self.assertEqual(group["category_source"], "file")

    def test_file_beats_alias_memory(self):
        phones = self._category("Phones")
        tablets = self._category("Tablets")
        self._alias(category=tablets)
        run = self._run()
        self._file_category(run, phones)
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        result = propose_groups(organization=self.org, offers=[offer])
        group = result["groups"][0]
        self.assertEqual(group["source"], "memory")
        self.assertEqual(group["category_name"], "Phones")
        self.assertEqual(group["category_source"], "file")

    def test_alias_majority_decides(self):
        phones = self._category("Phones")
        self._alias(category=phones)
        self._alias(category=phones, supplier_norm="b")
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="iPhone 15"),
        ]
        result = propose_groups(organization=self.org, offers=offers)
        self.assertFalse(result["needs_review"])
        group = result["groups"][0]
        self.assertEqual(group["category_name"], "Phones")
        self.assertEqual(group["category_source"], "alias_memory")

    def test_singleton_alias_decides(self):
        phones = self._category("Phones")
        self._alias(category=phones)
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        result = propose_groups(organization=self.org, offers=[offer])
        group = result["groups"][0]
        self.assertEqual(group["category_name"], "Phones")
        self.assertEqual(group["category_source"], "alias_memory")
        self.assertFalse(group["category_review"])

    def test_split_aliases_need_reconcile_when_simulated(self):
        phones = self._category("Phones")
        tablets = self._category("Tablets")
        self._alias(category=phones)
        self._alias(category=tablets, supplier_norm="b")
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="iPhone 15"),
        ]
        result = propose_groups(organization=self.org, offers=offers)
        group = result["groups"][0]
        self.assertEqual(group["source"], "memory")
        self.assertIsNone(group["category_id"])
        self.assertTrue(group["category_review"])
        self.assertTrue(result["needs_review"])
        self.assertTrue(
            any("no category rule" in note for note in result["notes"])
        )

    def test_uncovered_product_needs_reconcile_when_simulated(self):
        self._category("Phones")
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        result = propose_groups(organization=self.org, offers=[offer])
        group = result["groups"][0]
        self.assertTrue(group["category_review"])
        self.assertTrue(result["needs_review"])
        self.assertTrue(
            any("no category rule" in note for note in result["notes"])
        )
        self.assertEqual(UsageEvent.objects.count(), 0)

    def test_foreign_category_never_resolves(self):
        other = Organization.objects.create(name="Other")
        foreign = self._category("Phones", org=other)
        self._category("Tablets")
        run = self._run()
        self._file_category(run, foreign)
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        result = propose_groups(organization=self.org, offers=[offer])
        group = result["groups"][0]
        self.assertIsNone(group["category_id"])
        self.assertTrue(group["category_review"])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PaidCategorizeTests(MatchingCase):
    def _propose(self, offers, stub):
        with mock.patch(
            "pipeline.matching.get_provider", return_value=stub
        ):
            return propose_groups(organization=self.org, offers=offers)

    def _memory_pair(self):
        self._alias()
        self._alias(supplier_norm="b")
        run = self._run()
        return [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="iPhone 15"),
        ]

    def test_confident_decision_auto_assigns_and_logs_spend(self):
        self._category("Phones")
        offers = self._memory_pair()
        stub = _StubProvider(
            decisions=[{"ref": 0, "category": "Phones", "confidence": 0.95}]
        )
        result = self._propose(offers, stub)
        self.assertFalse(result["needs_review"])
        self.assertEqual(len(stub.categorize_calls), 1)
        payload = stub.categorize_calls[0]
        self.assertEqual(payload["categories"], ["Phones"])
        self.assertEqual(payload["products"][0]["model"], "iPhone 15")
        self.assertEqual(payload["block"]["grade"], "a")
        group = result["groups"][0]
        self.assertEqual(group["category_name"], "Phones")
        self.assertEqual(group["category_source"], "auto")
        event = UsageEvent.objects.get(kind="provider_call")
        self.assertEqual(event.quantity, 15)
        self.assertEqual(event.detail["task"], "categorize")

    def test_low_confidence_suggests_for_reconcile(self):
        self._category("Phones")
        offers = self._memory_pair()
        stub = _StubProvider(
            decisions=[{"ref": 0, "category": "Phones", "confidence": 0.4}]
        )
        result = self._propose(offers, stub)
        self.assertTrue(result["needs_review"])
        group = result["groups"][0]
        self.assertTrue(group["category_review"])
        self.assertEqual(group["category_suggestion"], "Phones")
        self.assertIsNone(group["category_id"])
        self.assertTrue(any("unsure" in note for note in result["notes"]))

    def test_unknown_category_name_flagged(self):
        self._category("Phones")
        offers = self._memory_pair()
        stub = _StubProvider(
            decisions=[{"ref": 0, "category": "Tablets", "confidence": 0.99}]
        )
        result = self._propose(offers, stub)
        group = result["groups"][0]
        self.assertTrue(group["category_review"])
        self.assertIsNone(group["category_id"])
        self.assertTrue(any("unknown category" in note for note in result["notes"]))

    def test_double_booked_and_unplaced_flagged(self):
        self._category("Phones")
        run = self._run()
        offers = [
            self._offer(run, supplier="A", model_name="iPhone 15"),
            self._offer(run, supplier="B", model_name="iPhone 15 Pro"),
        ]
        stub = _StubProvider(
            script=[
                {"canonical": "iPhone 15", "members": [0], "confidence": 0.95},
                {"canonical": "iPhone 15 Pro", "members": [1], "confidence": 0.95},
            ],
            decisions=[
                {"ref": 0, "category": "Phones", "confidence": 0.95},
                {"ref": 0, "category": "Phones", "confidence": 0.95},
            ],
        )
        result = self._propose(offers, stub)
        self.assertTrue(result["needs_review"])
        self.assertTrue(any("double-booked" in note for note in result["notes"]))
        self.assertTrue(any("unplaced" in note for note in result["notes"]))
        placed = sorted(offer for group in result["groups"] for offer in group["offers"])
        self.assertEqual(placed, sorted(o.id for o in offers))

    def test_unusable_categorize_keeps_groups_for_reconcile(self):
        self._category("Phones")
        offers = self._memory_pair()
        stub = _StubProvider(
            categorize_error=MatchUnusable("Categorize returned empty content.")
        )
        result = self._propose(offers, stub)
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["groups"][0]["category_review"])
        self.assertTrue(any("unusable" in note for note in result["notes"]))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RememberCategoryTests(MatchingCase):
    def test_remember_learns_and_clears_category(self):
        phones = self._category("Phones")
        run = self._run()
        offer = self._offer(run, supplier="A", model_name="iPhone 15")
        remember_decisions(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [offer.id],
                    "category_id": phones.id,
                }
            ],
            expected_offer_ids=[offer.id],
        )
        self.assertEqual(ProductAlias.objects.get().category_id, phones.id)
        remember_decisions(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [offer.id]}],
            expected_offer_ids=[offer.id],
        )
        self.assertIsNone(ProductAlias.objects.get().category)

    def test_remember_refuses_unknown_category(self):
        other = Organization.objects.create(name="Other")
        foreign = self._category("Phones", org=other)
        run = self._run()
        offer = self._offer(run, model_name="iPhone 15")
        for bad in (foreign.id, 999999, "Phones", True):
            with self.assertRaises(ValidationError) as ctx:
                remember_decisions(
                    organization=self.org,
                    decisions=[
                        {
                            "canonical_model": "iPhone 15",
                            "offer_ids": [offer.id],
                            "category_id": bad,
                        }
                    ],
                    expected_offer_ids=[offer.id],
                )
            self.assertIn("unknown category", str(ctx.exception))


class FixtureCategorizeTests(SimpleTestCase):
    def test_fixture_leaves_products_unassigned(self):
        result = FixtureProvider().categorize(
            {
                "categories": ["Phones"],
                "products": [
                    {"ref": 0, "model": "iPhone 15"},
                    {"ref": 1, "model": "Galaxy S24"},
                ],
            }
        )
        self.assertTrue(result["simulated"])
        self.assertEqual(
            result["decisions"],
            [
                {"ref": 0, "category": "", "confidence": 0.0},
                {"ref": 1, "category": "", "confidence": 0.0},
            ],
        )
        self.assertEqual(result["usage"]["total_tokens"], 0)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MetaCategorizeWireTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")

    def _provider(self, script, **overrides):
        options = {
            "organization_id": self.org.id,
            "api_key": "LLM_9_testsecret",
            "model": "test-model",
        }
        options.update(overrides)

        def factory(config):
            return _FakeClient(script)

        return MetaProvider(MetaConfig(**options), client_factory=factory)

    def _allow(self, credits=100000):
        allowance = get_allowance(self.org.id)
        allowance.token_budget = credits
        allowance.monthly_credits = credits
        allowance.save()
        return allowance

    def _payload(self):
        return {
            "block": {"brand": "apple", "capacity_gb": 128, "grade": "a"},
            "categories": ["Phones", "Tablets"],
            "products": [{"ref": 0, "model": "iPhone 15"}],
        }

    def test_categorize_returns_decisions_and_spends(self):
        self._allow()
        body = json.dumps(
            {"decisions": [{"ref": 0, "category": "Phones", "confidence": 0.9}]}
        )
        provider = self._provider(_FakeResponse(body, _FakeUsage(12, 7)))
        chunks = []
        result = provider.categorize(
            self._payload(),
            on_chunk=lambda *args: chunks.append(args),
        )
        self.assertFalse(result["simulated"])
        self.assertEqual(
            result["decisions"],
            [{"ref": 0, "category": "Phones", "confidence": 0.9}],
        )
        self.assertEqual(result["usage"]["total_tokens"], 19)
        self.assertEqual(chunks, [(1, 1, 12, 7)])
        allowance = get_allowance(self.org.id)
        self.assertEqual(allowance.period_spent, 19)

    def test_categorize_bad_json_is_unusable_not_transport(self):
        self._allow()
        provider = self._provider(_FakeResponse("nope", _FakeUsage(12, 7)))
        with self.assertRaises(MatchUnusable):
            provider.categorize(self._payload())

    def test_categorize_rejects_empty_payload(self):
        self._allow()
        provider = self._provider(_FakeResponse("{}", _FakeUsage(1, 1)))
        with self.assertRaises(ProviderError):
            provider.categorize({"products": []})
