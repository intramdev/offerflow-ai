"""Tests for pricing rules and customer quotes."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from organizations.models import Membership, Organization
from pipeline.models import (
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    PricingRule,
    RuleSetVersion,
    RunStatus,
    SourceFile,
    SourceRow,
)
from pipeline.quotes import (
    build_quote,
    create_rule,
    render_quote_csv,
    render_quote_xlsx,
)

User = get_user_model()

RULE_DATA = {
    "name": "Standard",
    "matcher": {},
    "supplier_strategy": "lowest",
    "preferred_supplier": "",
    "ops": [],
    "priority": 100,
    "active": True,
}


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RuleServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
        self.rule_version = RuleSetVersion.objects.get(version="v1")

    def _run(self):
        source_file = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.user,
            original_name="s.xlsx",
            size_bytes=10,
            sha256="ab" * 32,
            storage_key="org/1",
        )
        return ImportRun.objects.create(
            organization=self.org,
            source_file=source_file,
            rule_version=self.rule_version,
            status=RunStatus.READY_FOR_REVIEW,
        )

    def _offer(self, run, index, **fields):
        row = SourceRow.objects.create(
            organization=self.org,
            run=run,
            index=index,
            sheet="Sheet1",
            data={},
            provenance={"sheet": "Sheet1", "row": index},
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
        data = dict(RULE_DATA)
        data.update(overrides)
        return create_rule(organization=self.org, user=self.user, data=data)

    def test_create_rule_defaults_lowest(self):
        rule = self._rule()
        self.assertEqual(rule.supplier_strategy, "lowest")
        self.assertEqual(rule.priority, 100)
        self.assertTrue(rule.active)

    def test_create_rule_duplicate_name_rejected(self):
        self._rule()
        with self.assertRaises(ValidationError):
            self._rule()

    def test_create_rule_unknown_matcher_key_rejected(self):
        with self.assertRaises(ValidationError):
            self._rule(matcher={"color": "red"})

    def test_create_rule_accepts_category_matcher(self):
        rule = self._rule(name="Phones", matcher={"category": "Phones"})
        self.assertEqual(rule.matcher, {"category": "Phones"})

    def test_category_rule_beats_general_despite_priority(self):
        from pipeline.quotes import _winning_rule

        general = self._rule(name="General", matcher={}, priority=0)
        scoped = self._rule(
            name="Phones", matcher={"category": "phones"}, priority=100
        )
        winner = _winning_rule(
            [general, scoped],
            {"brand": "Apple", "model": "iPhone 15", "category": "Phones"},
        )
        self.assertEqual(winner, scoped)
        fallback = _winning_rule(
            [general, scoped],
            {"brand": "Apple", "model": "iPhone 15", "category": ""},
        )
        self.assertEqual(fallback, general)

    def test_per_run_quote_keeps_colors_on_separate_lines(self):
        # R7: the run-level grouping also splits variants; each
        # line carries its own color and conditions.
        self._rule()
        run = self._run()
        self._offer(run, 2, color="Black", conditions="Boxed")
        self._offer(run, 3, color="White", conditions="Loose")
        quote = build_quote(run)
        self.assertEqual(len(quote["lines"]), 2)
        by_color = {line["color"]: line for line in quote["lines"]}
        self.assertEqual(set(by_color), {"Black", "White"})
        self.assertEqual(by_color["Black"]["conditions"], "Boxed")
        self.assertEqual(by_color["White"]["conditions"], "Loose")

    def test_per_run_quote_ignores_category_rules(self):
        self._rule(
            name="Phones",
            matcher={"category": "Phones"},
            ops=[{"op": "percent", "value": "50"}],
        )
        self._rule(name="General", matcher={}, ops=[])
        run = self._run()
        self._offer(run, 2)
        quote = build_quote(run)
        self.assertEqual(quote["lines"][0]["rule"], "General")
        self.assertEqual(quote["lines"][0]["unit_price"], "100.0000")

    def test_rename_retargets_rule_matchers(self):
        from pipeline.customer_quotes import update_category
        from pipeline.models import Category

        category = Category.objects.create(organization=self.org, name="Phones")
        rule = self._rule(name="Phones", matcher={"category": "phones"})
        update_category(
            category=category, user=self.user, data={"name": "Smartphones"}
        )
        rule.refresh_from_db()
        self.assertEqual(rule.matcher, {"category": "Smartphones"})

    def test_delete_refused_while_rule_names_the_shelf(self):
        from pipeline.customer_quotes import delete_category
        from pipeline.models import Category

        category = Category.objects.create(organization=self.org, name="Phones")
        self._rule(name="Phones deal", matcher={"category": "Phones"})
        with self.assertRaises(ValidationError) as ctx:
            delete_category(category=category, user=self.user)
        self.assertIn("Phones deal", str(ctx.exception))
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())

    def test_create_rule_unknown_op_rejected(self):
        with self.assertRaises(ValidationError):
            self._rule(ops=[{"op": "discount"}])

    def test_create_rule_bad_currency_rejected(self):
        with self.assertRaises(ValidationError):
            self._rule(ops=[{"op": "convert", "rate": "1.1", "to": "EU"}])

    def test_create_rule_viewer_forbidden(self):
        viewer = User.objects.create_user(username="v", password="x")
        Membership.objects.create(
            user=viewer, organization=self.org, role="viewer"
        )
        with self.assertRaises(DjangoPermissionDenied):
            create_rule(organization=self.org, user=viewer, data=dict(RULE_DATA))

    def test_lowest_picks_cheapest_supplier(self):
        run = self._run()
        self._offer(run, 2, supplier="Expensive", unit_price=Decimal("120.0000"))
        self._offer(run, 3, supplier="Cheap", unit_price=Decimal("90.0000"))
        self._rule()
        quote = build_quote(run)
        self.assertEqual(len(quote["lines"]), 1)
        self.assertEqual(quote["lines"][0]["supplier"], "Cheap")
        self.assertEqual(quote["lines"][0]["unit_price"], "90.0000")

    def test_highest_strategy_picks_priciest(self):
        run = self._run()
        self._offer(run, 2, supplier="Expensive", unit_price=Decimal("120.0000"))
        self._offer(run, 3, supplier="Cheap", unit_price=Decimal("90.0000"))
        self._rule(supplier_strategy="highest")
        quote = build_quote(run)
        self.assertEqual(quote["lines"][0]["supplier"], "Expensive")

    def test_preferred_missing_supplier_skips_with_note(self):
        run = self._run()
        self._offer(run, 2, supplier="Someone Else")
        self._rule(supplier_strategy="preferred", preferred_supplier="Acme")
        quote = build_quote(run)
        self.assertEqual(quote["lines"], [])
        self.assertEqual(len(quote["notes"]), 1)
        self.assertIn("Acme", quote["notes"][0])

    def test_convert_then_percent_chain(self):
        run = self._run()
        self._offer(run, 2, unit_price=Decimal("100.0000"), currency="USD")
        self._rule(
            ops=[
                {"op": "convert", "rate": "0.9", "to": "EUR"},
                {"op": "percent", "value": "10"},
            ]
        )
        quote = build_quote(run)
        line = quote["lines"][0]
        self.assertEqual(line["currency"], "EUR")
        self.assertEqual(line["unit_price"], "99.000000")

    def test_round_step_applies_half_up(self):
        run = self._run()
        self._offer(run, 2, unit_price=Decimal("104.5000"))
        self._rule(ops=[{"op": "round", "step": "1"}])
        quote = build_quote(run)
        self.assertEqual(quote["lines"][0]["unit_price"], "105")

    def test_specific_matcher_beats_global_rule(self):
        run = self._run()
        self._offer(run, 2, brand="Apple")
        self._rule(name="Global", priority=0)
        self._rule(
            name="Apple only",
            matcher={"brand": "Apple"},
            priority=100,
            ops=[{"op": "percent", "value": "50"}],
        )
        quote = build_quote(run)
        self.assertEqual(quote["lines"][0]["rule"], "Apple only")
        self.assertEqual(quote["lines"][0]["unit_price"], "150.00000")

    def test_non_matching_rule_ignored(self):
        run = self._run()
        self._offer(run, 2, brand="Apple")
        self._rule(matcher={"brand": "Samsung"})
        quote = build_quote(run)
        self.assertIsNone(quote["lines"][0]["rule"])

    def test_inactive_rule_ignored(self):
        run = self._run()
        self._offer(run, 2)
        self._rule(active=False, ops=[{"op": "percent", "value": "50"}])
        quote = build_quote(run)
        self.assertIsNone(quote["lines"][0]["rule"])
        self.assertEqual(quote["lines"][0]["unit_price"], "100.0000")

    def test_no_rule_means_lowest_unchanged(self):
        run = self._run()
        self._offer(run, 2, unit_price=Decimal("100.0000"))
        quote = build_quote(run)
        self.assertIsNone(quote["lines"][0]["rule"])
        self.assertEqual(quote["lines"][0]["currency"], "USD")

    def test_different_currencies_are_separate_groups(self):
        run = self._run()
        self._offer(run, 2, supplier="Dollar", unit_price=Decimal("100.0000"),
                    currency="USD")
        self._offer(run, 3, supplier="Euro", unit_price=Decimal("1.0000"),
                    currency="EUR")
        self._rule(supplier_strategy="lowest")
        quote = build_quote(run)
        self.assertEqual(len(quote["lines"]), 2)

    def test_different_grades_are_separate_lines(self):
        run = self._run()
        self._offer(run, 2, grade="A")
        self._offer(run, 3, grade="B")
        quote = build_quote(run)
        self.assertEqual(len(quote["lines"]), 2)

    def test_excluded_offers_are_not_quotable(self):
        run = self._run()
        self._offer(run, 2, status=OfferStatus.EXCLUDED)
        quote = build_quote(run)
        self.assertEqual(quote["lines"], [])

    def test_quote_csv_has_header_and_line(self):
        run = self._run()
        self._offer(run, 2)
        quote = build_quote(run)
        text = render_quote_csv(quote).decode("utf-8")
        rows = text.strip().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("Brand,Model"))
        self.assertIn("iPhone 15", rows[1])

    def test_quote_xlsx_price_is_numeric(self):
        import io

        from openpyxl import load_workbook

        run = self._run()
        self._offer(run, 2)
        quote = build_quote(run)
        workbook = load_workbook(filename=io.BytesIO(render_quote_xlsx(quote)))
        sheet = workbook.active
        self.assertEqual(sheet["A1"].value, "Brand")
        self.assertEqual(sheet["G2"].value, 100)
        self.assertIsInstance(sheet["G2"].value, (int, float))

    def test_quote_csv_sanitizes_formula_supplier(self):
        run = self._run()
        self._offer(run, 2, supplier="=1+1")
        quote = build_quote(run)
        text = render_quote_csv(quote).decode("utf-8")
        self.assertIn("'=1+1", text)

    def test_category_column_is_opt_in(self):
        run = self._run()
        self._offer(run, 2)
        quote = build_quote(run)
        plain = render_quote_csv(quote).decode("utf-8").splitlines()[0]
        self.assertNotIn("Category", plain)
        quote["lines"][0]["category_name"] = "Phones"
        text = render_quote_csv(quote, include_category=True).decode("utf-8")
        rows = text.strip().splitlines()
        self.assertTrue(rows[0].endswith(",Category"))
        self.assertTrue(rows[1].endswith(",Phones"))

    def test_quote_xlsx_appends_category_when_asked(self):
        import io

        from openpyxl import load_workbook

        run = self._run()
        self._offer(run, 2)
        quote = build_quote(run)
        quote["lines"][0]["category_name"] = "Phones"
        workbook = load_workbook(
            filename=io.BytesIO(render_quote_xlsx(quote, include_category=True))
        )
        sheet = workbook.active
        self.assertEqual(sheet["J1"].value, "Category")
        self.assertEqual(sheet["J2"].value, "Phones")
        self.assertEqual(sheet["G2"].value, 100)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RuleApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")
        self.rule_version = RuleSetVersion.objects.get(version="v1")
        self.client.force_login(self.user)

    def _run(self):
        source_file = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.user,
            original_name="s.xlsx",
            size_bytes=10,
            sha256="cd" * 32,
            storage_key="org/1",
        )
        return ImportRun.objects.create(
            organization=self.org,
            source_file=source_file,
            rule_version=self.rule_version,
            status=RunStatus.READY_FOR_REVIEW,
        )

    def _approved_run(self):
        from pipeline.quotes import build_quote

        run = self._run()
        row = SourceRow.objects.create(
            organization=self.org,
            run=run,
            index=2,
            sheet="Sheet1",
            data={
                "supplier": "Supplier A",
                "brand": "Apple",
                "model": "iPhone 15",
                "capacity": "128GB",
                "grade": "A",
                "quantity": 5,
                "price": "100",
                "currency": "USD",
            },
            provenance={"sheet": "Sheet1", "row": 2},
        )
        NormalizedOffer.objects.create(
            organization=self.org,
            run=run,
            source_row=row,
            supplier="Supplier A",
            brand="Apple",
            model_name="iPhone 15",
            capacity_gb=128,
            grade="A",
            quantity=5,
            unit_price=Decimal("100.0000"),
            currency="USD",
            status=OfferStatus.OK,
            provenance={"sheet": "Sheet1", "row": 2, "source_row_id": row.id},
        )
        build_quote(run)
        approved = self.client.post(f"/api/pipeline/runs/{run.id}/approve/")
        self.assertEqual(approved.status_code, 200)
        run.refresh_from_db()
        return run

    def test_create_and_list_rules(self):
        payload = dict(RULE_DATA)
        payload["organization_id"] = self.org.id
        created = self.client.post(
            "/api/pipeline/rules/", payload, content_type="application/json"
        )
        self.assertEqual(created.status_code, 201)
        listed = self.client.get("/api/pipeline/rules/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)

    def test_create_rule_requires_organization(self):
        response = self.client.post(
            "/api/pipeline/rules/", dict(RULE_DATA), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rule_bad_op_returns_400(self):
        payload = dict(RULE_DATA)
        payload["organization_id"] = self.org.id
        payload["ops"] = [{"op": "nope"}]
        response = self.client.post(
            "/api/pipeline/rules/", payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rule_viewer_forbidden(self):
        viewer = User.objects.create_user(username="v", password="x")
        Membership.objects.create(
            user=viewer, organization=self.org, role="viewer"
        )
        self.client.force_login(viewer)
        payload = dict(RULE_DATA)
        payload["organization_id"] = self.org.id
        response = self.client.post(
            "/api/pipeline/rules/", payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)

    def test_update_rule_replaces_definition(self):
        rule = PricingRule.objects.create(organization=self.org, **RULE_DATA)
        response = self.client.put(
            f"/api/pipeline/rules/{rule.id}/",
            {"name": "Renamed", "priority": 5},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Renamed")
        self.assertEqual(response.json()["priority"], 5)

    def test_delete_rule(self):
        rule = PricingRule.objects.create(organization=self.org, **RULE_DATA)
        response = self.client.delete(f"/api/pipeline/rules/{rule.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(PricingRule.objects.filter(id=rule.id).exists())

    def test_quote_endpoint_returns_lines(self):
        run = self._run()
        row = SourceRow.objects.create(
            organization=self.org,
            run=run,
            index=2,
            sheet="Sheet1",
            data={},
            provenance={"sheet": "Sheet1", "row": 2},
        )
        NormalizedOffer.objects.create(
            organization=self.org,
            run=run,
            source_row=row,
            supplier="Supplier A",
            brand="Apple",
            model_name="iPhone 15",
            capacity_gb=128,
            grade="A",
            quantity=5,
            unit_price=Decimal("100.0000"),
            currency="USD",
            status=OfferStatus.OK,
        )
        response = self.client.get(f"/api/pipeline/runs/{run.id}/quote/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["run_status"], RunStatus.READY_FOR_REVIEW)
        self.assertEqual(len(body["lines"]), 1)

    def test_quote_download_csv(self):
        run = self._approved_run()
        response = self.client.get(
            f"/api/pipeline/runs/{run.id}/quote/download/csv/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = b"".join(response.streaming_content).decode("utf-8")
        self.assertTrue(content.startswith("Brand,Model"))

    def test_quote_download_defaults_xlsx(self):
        run = self._approved_run()
        response = self.client.get(f"/api/pipeline/runs/{run.id}/quote/download/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_quote_download_rejects_format(self):
        run = self._run()
        response = self.client.get(
            f"/api/pipeline/runs/{run.id}/quote/download/pdf/"
        )
        self.assertEqual(response.status_code, 400)

    def test_quote_download_refuses_unapproved_run(self):
        run = self._run()
        response = self.client.get(f"/api/pipeline/runs/{run.id}/quote/download/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("approved", response.json()["detail"].lower())

    def test_quote_download_refuses_after_rule_change(self):
        run = self._approved_run()
        PricingRule.objects.create(organization=self.org, **RULE_DATA)
        response = self.client.get(f"/api/pipeline/runs/{run.id}/quote/download/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Pricing rules changed", response.json()["detail"])

    def test_quote_preview_stays_open_and_labeled(self):
        run = self._run()
        response = self.client.get(f"/api/pipeline/runs/{run.id}/quote/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["preview"])
