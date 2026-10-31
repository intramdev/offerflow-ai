"""Customer quotes: latest-per-supplier sources, canonical pricing, outputs."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations.models import Membership, Organization
from pipeline.customer_quotes import (
    build_customer_quote,
    collect_quote_offers,
    render_message,
)
from pipeline.models import (
    Category,
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    RuleSetVersion,
    RunStatus,
    SourceFile,
    SourceRow,
)
from pipeline.quotes import create_rule

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CustomerQuoteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
        self.rule_version = RuleSetVersion.objects.get(version="v1")
        self._row_index = 0
        self._file_index = 0

    def _run(self, status=RunStatus.APPROVED, days_ago=0, **fx):
        self._file_index += 1
        source_file = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.user,
            original_name=f"s{self._file_index}.xlsx",
            size_bytes=10,
            sha256=f"{self._file_index:064d}",
            storage_key=f"org/{self.org.id}/{self._file_index}",
            currency_date=timezone.localdate() - timedelta(days=days_ago),
            fx_destination=fx.get("destination", ""),
            fx_rates=fx.get("rates", {}),
            fx_source=fx.get("source", ""),
            fx_simulated=True,
        )
        run = ImportRun.objects.create(
            organization=self.org,
            source_file=source_file,
            rule_version=self.rule_version,
            status=status,
        )
        if status in (RunStatus.APPROVED, RunStatus.COMPLETED):
            run.approved_at = timezone.now() - timedelta(days=days_ago)
            run.save(update_fields=["approved_at"])
        return run

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
        return NormalizedOffer.objects.create(
            organization=self.org, run=run, source_row=row, **defaults
        )

    def _rule(self, **overrides):
        data = {
            "name": "Standard",
            "matcher": {},
            "supplier_strategy": "lowest",
            "preferred_supplier": "",
            "ops": [],
            "priority": 100,
            "active": True,
        }
        data.update(overrides)
        return create_rule(organization=self.org, user=self.user, data=data)

    def test_collect_prefers_latest_approved_per_supplier(self):
        old = self._run(days_ago=5)
        stale = self._offer(old, supplier="Acme")
        new = self._run(days_ago=0)
        fresh = self._offer(new, supplier="Acme")
        other_run = self._run(days_ago=1)
        other = self._offer(other_run, supplier="Globex")
        offers, run_ids = collect_quote_offers(self.org)
        self.assertEqual({o.id for o in offers}, {fresh.id, other.id})
        self.assertNotIn(stale.id, {o.id for o in offers})
        self.assertEqual(run_ids, sorted([new.id, other_run.id]))

    def test_collect_ignores_unapproved_runs(self):
        draft = self._run(status=RunStatus.READY_FOR_REVIEW)
        self._offer(draft, supplier="Acme")
        failed = self._run(status=RunStatus.FAILED)
        self._offer(failed, supplier="Globex")
        done = self._run(status=RunStatus.COMPLETED)
        kept = self._offer(done, supplier="Initech")
        offers, run_ids = collect_quote_offers(self.org)
        self.assertEqual([o.id for o in offers], [kept.id])
        self.assertEqual(run_ids, [done.id])

    def test_collect_empty_without_quotable_runs(self):
        self.assertEqual(collect_quote_offers(self.org), ([], []))

    def test_build_picks_lowest_and_applies_rule(self):
        run = self._run()
        cheap = self._offer(run, supplier="Cheap", unit_price=Decimal("90.0000"))
        pricey = self._offer(run, supplier="Pricey", unit_price=Decimal("120.0000"))
        self._rule(ops=[{"op": "percent", "value": "10"}])
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [cheap.id, pricey.id],
                }
            ],
        )
        self.assertEqual(len(result["lines"]), 1)
        line = result["lines"][0]
        self.assertEqual(line["supplier"], "Cheap")
        self.assertEqual(line["unit_price"], str(Decimal("90.0000") * Decimal("1.1")))
        self.assertEqual(line["currency"], "USD")
        self.assertEqual(line["rule"], "Standard")
        self.assertEqual(len(line["candidates"]), 2)

    def test_rule_matcher_uses_canonical_model(self):
        run = self._run()
        offer = self._offer(run, model_name="IPHONE-15")
        self._rule(name="Apple rule", matcher={"model": "iPhone 15"})
        self._rule(name="Global")
        result = build_customer_quote(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [offer.id]}],
        )
        self.assertEqual(result["lines"][0]["rule"], "Apple rule")

    def test_build_converts_each_file_with_its_own_rates(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run_a = self._run(destination="EUR", rates={"USD": "0.900000"})
        offer_a = self._offer(
            run_a, supplier="A", currency="USD", unit_price=Decimal("100.0000")
        )
        run_b = self._run(destination="EUR", rates={"GBP": "0.500000"})
        offer_b = self._offer(
            run_b, supplier="B", currency="GBP", unit_price=Decimal("100.0000")
        )
        self._rule(ops=[{"op": "convert_to_destination"}])
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [offer_a.id, offer_b.id],
                }
            ],
        )
        # A converts to 90 EUR via its own file, B to 50 EUR via its
        # own: a shared table would leave one of them unrated.
        line = result["lines"][0]
        self.assertEqual(line["supplier"], "B")
        self.assertEqual(line["currency"], "EUR")
        self.assertEqual(
            line["unit_price"], str(Decimal("100.0000") * Decimal("0.500000"))
        )

    def test_mixed_currencies_need_destination(self):
        run = self._run()
        usd = self._offer(run, supplier="A", currency="USD")
        eur = self._offer(run, supplier="B", currency="EUR")
        self.org.destination_currency = ""
        self.org.save(update_fields=["destination_currency"])
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {"canonical_model": "iPhone 15", "offer_ids": [usd.id, eur.id]}
            ],
        )
        self.assertEqual(result["lines"], [])
        self.assertTrue(any("destination" in note for note in result["notes"]))

    def test_candidate_without_rate_excluded_not_fatal(self):
        self.org.destination_currency = "EUR"
        self.org.save(update_fields=["destination_currency"])
        run_a = self._run(destination="EUR", rates={"USD": "0.900000"})
        rated = self._offer(
            run_a, supplier="Rated", currency="USD", unit_price=Decimal("100.0000")
        )
        run_b = self._run()
        unrated = self._offer(
            run_b, supplier="Unrated", currency="JPY", unit_price=Decimal("1.0000")
        )
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [rated.id, unrated.id],
                }
            ],
        )
        self.assertEqual(len(result["lines"]), 1)
        self.assertEqual(result["lines"][0]["supplier"], "Rated")
        self.assertTrue(
            any("Unrated" in note and "excluded" in note for note in result["notes"])
        )

    def test_preferred_strategy_and_missing_supplier(self):
        run = self._run()
        cheap = self._offer(run, supplier="Cheap", unit_price=Decimal("90.0000"))
        pricey = self._offer(run, supplier="Pricey", unit_price=Decimal("120.0000"))
        self._rule(supplier_strategy="preferred", preferred_supplier="Pricey")
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [cheap.id, pricey.id],
                }
            ],
        )
        self.assertEqual(result["lines"][0]["supplier"], "Pricey")
        self._rule(
            name="Missing",
            matcher={"model": "iPhone 15"},
            supplier_strategy="preferred",
            preferred_supplier="Ghost",
            priority=1,
        )
        missing = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [cheap.id, pricey.id],
                }
            ],
        )
        self.assertEqual(missing["lines"], [])
        self.assertTrue(any("Ghost" in note for note in missing["notes"]))

    def test_build_without_rules_picks_lowest(self):
        run = self._run()
        cheap = self._offer(run, supplier="Cheap", unit_price=Decimal("90.0000"))
        pricey = self._offer(run, supplier="Pricey", unit_price=Decimal("120.0000"))
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [cheap.id, pricey.id],
                }
            ],
        )
        self.assertEqual(result["lines"][0]["supplier"], "Cheap")
        self.assertIsNone(result["lines"][0]["rule"])

    def test_build_rejects_unknown_offers(self):
        with self.assertRaises(ValidationError):
            build_customer_quote(
                organization=self.org,
                decisions=[{"canonical_model": "iPhone 15", "offer_ids": [424242]}],
            )

    def test_message_renders_plain_lines(self):
        text = render_message(
            organization_name="ACME ltd",
            day=timezone.localdate(),
            lines=[
                {
                    "brand": "Apple",
                    "model": "iPhone 15",
                    "capacity_gb": 128,
                    "grade": "A",
                    "unit_price": "245.0000",
                    "currency": "EUR",
                },
                {
                    "brand": "Samsung",
                    "model": "Galaxy S24",
                    "capacity_gb": None,
                    "grade": "",
                    "unit_price": "199.9900",
                    "currency": "EUR",
                },
            ],
        )
        day = timezone.localdate().isoformat()
        self.assertEqual(
            text,
            f"Quote — ACME ltd — {day}\n"
            "Apple iPhone 15 128GB (A) — EUR 245\n"
            "Samsung Galaxy S24 — EUR 199.99",
        )

    def test_build_stores_message_alongside_lines(self):
        run = self._run()
        offer = self._offer(run)
        result = build_customer_quote(
            organization=self.org,
            decisions=[{"canonical_model": "iPhone 15", "offer_ids": [offer.id]}],
        )
        self.assertIn("Apple iPhone 15 128GB (A) — USD 100", result["message"])

    def test_lines_carry_categories_sorted_with_other_last(self):
        phones = Category.objects.create(organization=self.org, name="Phones")
        tablets = Category.objects.create(organization=self.org, name="Tablets")
        run = self._run()
        phone = self._offer(run, model_name="iPhone 15")
        tablet = self._offer(
            run, brand="Apple", model_name="iPad Air", capacity_gb=256
        )
        loose = self._offer(
            run, brand="Samsung", model_name="Galaxy S24", capacity_gb=128
        )
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {"canonical_model": "Galaxy S24", "offer_ids": [loose.id]},
                {
                    "canonical_model": "iPad Air",
                    "offer_ids": [tablet.id],
                    "category_id": tablets.id,
                },
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [phone.id],
                    "category_id": phones.id,
                },
            ],
        )
        self.assertEqual(
            [line["category_name"] for line in result["lines"]],
            ["Phones", "Tablets", ""],
        )
        self.assertEqual(result["lines"][0]["category_id"], phones.id)
        self.assertIsNone(result["lines"][2]["category_id"])

    def test_unknown_category_id_builds_uncategorized(self):
        run = self._run()
        offer = self._offer(run)
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [offer.id],
                    "category_id": 424242,
                }
            ],
        )
        line = result["lines"][0]
        self.assertIsNone(line["category_id"])
        self.assertEqual(line["category_name"], "")

    def test_message_groups_lines_under_category_sections(self):
        day = timezone.localdate()
        text = render_message(
            organization_name="ACME ltd",
            day=day,
            lines=[
                {
                    "brand": "Apple",
                    "model": "iPhone 15",
                    "capacity_gb": 128,
                    "grade": "A",
                    "unit_price": "245.0000",
                    "currency": "EUR",
                    "category_id": 7,
                    "category_name": "Phones",
                },
                {
                    "brand": "Apple",
                    "model": "iPad Air",
                    "capacity_gb": 256,
                    "grade": "",
                    "unit_price": "500.0000",
                    "currency": "EUR",
                    "category_id": 9,
                    "category_name": "Tablets",
                },
                {
                    "brand": "Samsung",
                    "model": "Galaxy S24",
                    "capacity_gb": 128,
                    "grade": "",
                    "unit_price": "199.9900",
                    "currency": "EUR",
                    "category_id": None,
                    "category_name": "",
                },
            ],
        )
        self.assertEqual(
            text,
            f"Quote — ACME ltd — {day.isoformat()}\n"
            "\n"
            "Phones:\n"
            "Apple iPhone 15 128GB (A) — EUR 245\n"
            "\n"
            "Tablets:\n"
            "Apple iPad Air 256GB — EUR 500\n"
            "\n"
            "Other:\n"
            "Samsung Galaxy S24 128GB — EUR 199.99",
        )

    def test_category_rule_prices_its_shelf_general_covers_the_rest(self):
        phones = Category.objects.create(organization=self.org, name="Phones")
        self._rule(name="General", matcher={}, ops=[])
        self._rule(
            name="Phones deal",
            matcher={"category": "Phones"},
            ops=[{"op": "percent", "value": "10"}],
        )
        run = self._run()
        phone = self._offer(run, model_name="iPhone 15")
        loose = self._offer(
            run, brand="Samsung", model_name="Galaxy S24", capacity_gb=128
        )
        result = build_customer_quote(
            organization=self.org,
            decisions=[
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [phone.id],
                    "category_id": phones.id,
                },
                {"canonical_model": "Galaxy S24", "offer_ids": [loose.id]},
            ],
        )
        by_model = {line["model"]: line for line in result["lines"]}
        self.assertEqual(by_model["iPhone 15"]["rule"], "Phones deal")
        self.assertEqual(by_model["iPhone 15"]["unit_price"], "110.00000")
        self.assertEqual(by_model["Galaxy S24"]["rule"], "General")
        self.assertEqual(by_model["Galaxy S24"]["unit_price"], "100.0000")

    def test_message_stays_flat_without_categories(self):
        day = timezone.localdate()
        text = render_message(
            organization_name="ACME ltd",
            day=day,
            lines=[
                {
                    "brand": "Apple",
                    "model": "iPhone 15",
                    "capacity_gb": 128,
                    "grade": "",
                    "unit_price": "100.0000",
                    "currency": "USD",
                    "category_id": None,
                    "category_name": "",
                },
            ],
        )
        self.assertEqual(
            text,
            f"Quote — ACME ltd — {day.isoformat()}\n"
            "Apple iPhone 15 128GB — USD 100",
        )
