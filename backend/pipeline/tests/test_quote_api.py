"""Customer quote API: start, reconcile-confirm, read, download."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.models import (
    CustomerQuote,
    ImportRun,
    NormalizedOffer,
    OfferStatus,
    ProductAlias,
    RuleSetVersion,
    RunStatus,
    SourceFile,
    SourceRow,
)

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CustomerQuoteApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="x")
        self.viewer = User.objects.create_user(username="viewer", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.owner, organization=self.org, role="owner"
        )
        Membership.objects.create(
            user=self.viewer, organization=self.org, role="viewer"
        )
        self.rule_version = RuleSetVersion.objects.get(version="v1")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self._row_index = 0
        self._file_index = 0

    def _run(self, original_name="sup.xlsx", status=RunStatus.APPROVED, org=None):
        org = org or self.org
        self._file_index += 1
        source_file = SourceFile.objects.create(
            organization=org,
            uploaded_by=self.owner,
            original_name=original_name,
            size_bytes=10,
            sha256=f"{self._file_index:064d}",
            storage_key=f"org/{org.id}/{self._file_index}",
            currency_date=timezone.localdate(),
        )
        run = ImportRun.objects.create(
            organization=org,
            source_file=source_file,
            rule_version=self.rule_version,
            status=status,
        )
        if status in (RunStatus.APPROVED, RunStatus.COMPLETED):
            run.approved_at = timezone.now()
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

    def _seed_two_suppliers(self):
        run_a = self._run(original_name="a.xlsx")
        first = self._offer(run_a, supplier="Acme", model_name="iPhone 15")
        run_b = self._run(original_name="b.xlsx")
        second = self._offer(run_b, supplier="Globex", model_name="IPHONE-15")
        return run_a, run_b, first, second

    def test_start_needs_approved_offers(self):
        response = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing to quote", str(response.data))

    def test_start_proposes_groups(self):
        run_a, run_b, first, second = self._seed_two_suppliers()
        response = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        quote = response.data
        self.assertEqual(quote["status"], "needs_review")
        self.assertEqual(quote["source_run_ids"], sorted([run_a.id, run_b.id]))
        self.assertEqual(
            [s["filename"] for s in quote["sources"]], ["a.xlsx", "b.xlsx"]
        )
        self.assertEqual(len(quote["groups"]), 1)
        group = quote["groups"][0]
        self.assertEqual(group["canonical_model"], "iPhone 15")
        self.assertEqual(sorted(group["offers"]), sorted([first.id, second.id]))
        self.assertTrue(quote["simulated"])

    def test_confirm_prices_and_learns(self):
        _run_a, _run_b, first, second = self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        response = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {
                "groups": [
                    {
                        "canonical_model": "iPhone 15",
                        "offer_ids": [first.id, second.id],
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        quote = response.data
        self.assertEqual(quote["status"], "ready")
        self.assertEqual(len(quote["lines"]), 1)
        self.assertEqual(quote["lines"][0]["model"], "iPhone 15")
        self.assertIn("iPhone 15 128GB", quote["message"])
        self.assertEqual(ProductAlias.objects.count(), 2)

    def test_confirm_twice_refused(self):
        _run_a, _run_b, first, second = self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        payload = {
            "groups": [
                {
                    "canonical_model": "iPhone 15",
                    "offer_ids": [first.id, second.id],
                }
            ]
        }
        self.client.post(f"/api/pipeline/quotes/{quote_id}/confirm/", payload, format="json")
        again = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/", payload, format="json"
        )
        self.assertEqual(again.status_code, 400)
        self.assertIn("already ready", str(again.data))

    def test_confirm_validates_coverage(self):
        _run_a, _run_b, first, _second = self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        response = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {"groups": [{"canonical_model": "iPhone 15", "offer_ids": [first.id]}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("unplaced", str(response.data))

    def test_list_and_detail_scope_to_org(self):
        self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        listed = self.client.get(f"/api/pipeline/quotes/?organization_id={self.org.id}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([q["id"] for q in listed.data], [quote_id])
        detail = self.client.get(f"/api/pipeline/quotes/{quote_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["id"], quote_id)
        other = Organization.objects.create(name="Other")
        stranger = User.objects.create_user(username="stranger", password="x")
        Membership.objects.create(user=stranger, organization=other, role="owner")
        self.client.force_authenticate(stranger)
        missing = self.client.get(f"/api/pipeline/quotes/{quote_id}/")
        self.assertEqual(missing.status_code, 404)

    def test_viewer_reads_but_cannot_write(self):
        self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        self.client.force_authenticate(self.viewer)
        self.assertEqual(
            self.client.get("/api/pipeline/quotes/").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/api/pipeline/quotes/{quote_id}/").status_code, 200
        )
        denied = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        self.assertEqual(denied.status_code, 403)
        refused = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {"groups": [{"canonical_model": "X", "offer_ids": [1]}]},
            format="json",
        )
        self.assertEqual(refused.status_code, 403)

    def test_operator_deletes_quote_but_sources_survive(self):
        run_a, _run_b, _first, _second = self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        deleted = self.client.delete(f"/api/pipeline/quotes/{quote_id}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            self.client.get(f"/api/pipeline/quotes/{quote_id}/").status_code, 404
        )
        listed = self.client.get(
            f"/api/pipeline/quotes/?organization_id={self.org.id}"
        )
        self.assertNotIn(quote_id, [row["id"] for row in listed.data])
        self.assertEqual(
            self.client.get(f"/api/pipeline/runs/{run_a.id}/").status_code, 200
        )

    def test_viewer_cannot_delete_quote(self):
        self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        self.client.force_authenticate(self.viewer)
        denied = self.client.delete(f"/api/pipeline/quotes/{quote_id}/")
        self.assertEqual(denied.status_code, 403)
        self.client.force_authenticate(self.owner)
        self.assertEqual(
            self.client.get(f"/api/pipeline/quotes/{quote_id}/").status_code, 200
        )

    def test_download_needs_ready_quote(self):
        _run_a, _run_b, first, second = self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        early = self.client.get(f"/api/pipeline/quotes/{quote_id}/download/xlsx/")
        self.assertEqual(early.status_code, 400)
        self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {
                "groups": [
                    {
                        "canonical_model": "iPhone 15",
                        "offer_ids": [first.id, second.id],
                    }
                ]
            },
            format="json",
        )
        xlsx = self.client.get(f"/api/pipeline/quotes/{quote_id}/download/xlsx/")
        self.assertEqual(xlsx.status_code, 200)
        self.assertEqual(
            xlsx["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertTrue(len(b"".join(xlsx.streaming_content)) > 0)
        csv = self.client.get(f"/api/pipeline/quotes/{quote_id}/download/csv/")
        self.assertEqual(csv.status_code, 200)
        body = b"".join(csv.streaming_content).decode("utf-8-sig")
        self.assertTrue(body.startswith("Brand,Model"))
        self.assertIn("iPhone 15", body)
        bad = self.client.get(f"/api/pipeline/quotes/{quote_id}/download/pdf/")
        self.assertEqual(bad.status_code, 400)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class QuotePresentationApiTests(CustomerQuoteApiTests):
    """Quote pagination, line edits, and export templates."""

    def _seed_two_products(self):
        run_a = self._run(original_name="a.xlsx")
        iphone_a = self._offer(run_a, supplier="Acme", model_name="iPhone 15")
        galaxy_a = self._offer(
            run_a,
            supplier="Acme",
            brand="Samsung",
            model_name="Galaxy S24",
            grade="B",
            unit_price=Decimal("200.0000"),
        )
        run_b = self._run(original_name="b.xlsx")
        iphone_b = self._offer(run_b, supplier="Globex", model_name="IPHONE-15")
        galaxy_b = self._offer(
            run_b,
            supplier="Globex",
            brand="Samsung",
            model_name="GALAXY-S24",
            grade="B",
            unit_price=Decimal("210.0000"),
        )
        return iphone_a, galaxy_a, iphone_b, galaxy_b

    def _ready_quote_id(self):
        iphone_a, galaxy_a, iphone_b, galaxy_b = self._seed_two_products()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        response = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {
                "groups": [
                    {
                        "canonical_model": "iPhone 15",
                        "offer_ids": [iphone_a.id, iphone_b.id],
                    },
                    {
                        "canonical_model": "Galaxy S24",
                        "offer_ids": [galaxy_a.id, galaxy_b.id],
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return quote_id

    def _csv_body(self, quote_id):
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/"
        )
        self.assertEqual(response.status_code, 200)
        return b"".join(response.streaming_content).decode("utf-8-sig")

    def test_list_paginates_fifty_newest_first(self):
        ids = [
            CustomerQuote.objects.create(
                organization=self.org, status="needs_review"
            ).id
            for _ in range(52)
        ]
        first = self.client.get(
            f"/api/pipeline/quotes/?organization_id={self.org.id}&page=1"
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["page"], 1)
        self.assertEqual(first.data["pages"], 2)
        self.assertEqual(first.data["count"], 52)
        self.assertEqual(first.data["page_size"], 50)
        self.assertEqual(len(first.data["results"]), 50)
        self.assertEqual(
            {row["id"] for row in first.data["results"]}, set(ids[2:])
        )
        second = self.client.get(
            f"/api/pipeline/quotes/?organization_id={self.org.id}&page=2"
        )
        self.assertEqual(len(second.data["results"]), 2)
        self.assertEqual(
            {row["id"] for row in second.data["results"]}, set(ids[:2])
        )
        self.assertEqual(
            [row["id"] for row in second.data["results"]],
            sorted(ids[:2], reverse=True),
        )

    def test_list_page_validates(self):
        for bad in ("0", "-1", "soon"):
            response = self.client.get(f"/api/pipeline/quotes/?page={bad}")
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("positive integer", str(response.data))

    def test_line_edits_relabel_and_hide(self):
        quote_id = self._ready_quote_id()
        response = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {
                "edits": {
                    "0": {"label": "iPhone 15 Pro Max", "hidden": False},
                    "1": {"hidden": True},
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        # Positions normalize to stable line ids on save.
        self.assertEqual(
            response.data["line_edits"],
            {
                "L1": {"label": "iPhone 15 Pro Max", "hidden": False},
                "L2": {"label": None, "hidden": True},
            },
        )
        # Frozen prices and sources stay untouched.
        self.assertEqual(response.data["lines"][0]["model"], "iPhone 15")
        body = self._csv_body(quote_id)
        self.assertIn("iPhone 15 Pro Max", body)
        self.assertNotIn("Galaxy S24", body)
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/"
        ).data["message"]
        self.assertIn("iPhone 15 Pro Max", message)
        self.assertNotIn("Galaxy S24", message)

    def test_line_edits_override_every_detail(self):
        from pipeline.models import Category

        phones = Category.objects.create(
            organization=self.org, name="Phones"
        )
        quote_id = self._ready_quote_id()
        response = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {
                "edits": {
                    "0": {
                        "label": None,
                        "hidden": False,
                        "grade": "A+",
                        "color": "Black",
                        "category_id": phones.id,
                        "quantity": 7,
                        "supplier": "Globex",
                        "unit_price": "89.995",
                    }
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        saved = response.data["line_edits"]["L1"]
        # Manual prices round half up to the cent on save.
        self.assertEqual(saved["unit_price"], "90.00")
        self.assertEqual(saved["supplier"], "Globex")
        self.assertEqual(saved["category_id"], phones.id)
        self.assertEqual(saved["category_name"], "Phones")
        # Frozen source values stay untouched underneath.
        frozen = response.data["lines"][0]
        self.assertEqual(frozen["supplier"], "Acme")
        self.assertEqual(frozen["grade"], "A")
        self.assertEqual(frozen["quantity"], 5)
        self.assertEqual(frozen["unit_price"], "100.0000")
        self.assertIsNone(frozen["category_id"])
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/"
        ).data["message"]
        self.assertIn("(Black, A+)", message)
        self.assertIn("USD 90.00", message)
        body = self._csv_body(quote_id)
        self.assertIn("90.00", body)
        self.assertIn("A+", body)
        shelf = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/"
            f"?category_id={phones.id}"
        )
        self.assertEqual(shelf.status_code, 200)
        shelf_body = b"".join(shelf.streaming_content).decode("utf-8-sig")
        self.assertIn("iPhone 15", shelf_body)
        self.assertNotIn("Galaxy S24", shelf_body)

    def test_line_edits_reject_bad_overrides(self):
        quote_id = self._ready_quote_id()
        bad_edits = [
            {"supplier": "Unknown Co"},
            {"supplier": ""},
            {"unit_price": "nine"},
            {"unit_price": "-5"},
            {"quantity": -1},
            {"quantity": 2.5},
            {"quantity": True},
            {"category_id": 999999},
            {"category_id": "phones"},
            {"grade": "x" * 61},
            {"color": 5},
            {"motto": "cheapest in town"},
        ]
        for edits in bad_edits:
            response = self.client.put(
                f"/api/pipeline/quotes/{quote_id}/lines/",
                {"edits": {"0": edits}},
                format="json",
            )
            self.assertEqual(response.status_code, 400, edits)

    def test_line_edits_drop_noop_overrides(self):
        quote_id = self._ready_quote_id()
        response = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {
                "edits": {
                    "0": {
                        "label": None,
                        "hidden": False,
                        "grade": "A",
                        "color": "",
                        "category_id": None,
                        "quantity": 5,
                        "supplier": "Acme",
                        "unit_price": "100.0000",
                    }
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["line_edits"], {})

    def test_money_renders_two_decimals(self):
        import io

        from pipeline.quotes import format_money

        self.assertEqual(format_money("245.0000"), "245.00")
        self.assertEqual(format_money("10.005"), "10.01")
        self.assertEqual(format_money("199.99"), "199.99")
        self.assertEqual(format_money("n/a"), "n/a")
        quote_id = self._ready_quote_id()
        self.assertIn("100.00", self._csv_body(quote_id))
        xlsx = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/xlsx/"
        )
        self.assertEqual(xlsx.status_code, 200)
        from openpyxl import load_workbook

        sheet = load_workbook(
            io.BytesIO(b"".join(xlsx.streaming_content))
        ).active
        headers = [cell.value for cell in sheet[1]]
        price_column = headers.index("Unit Price") + 1
        cell = sheet.cell(row=2, column=price_column)
        self.assertEqual(cell.value, 100.0)
        self.assertEqual(cell.number_format, "0.00")

    def test_line_edits_follow_lines_into_category_files(self):
        # R8: edits bind to stable line identity, not to the
        # position inside a filtered shelf.
        from pipeline.models import Category

        phones = Category.objects.create(
            organization=self.org, name="Phones"
        )
        tablets = Category.objects.create(
            organization=self.org, name="Tablets"
        )
        run = self._run()
        iphone = self._offer(run, supplier="Acme", model_name="iPhone 15")
        pixel = self._offer(
            run,
            supplier="Acme",
            brand="Google",
            model_name="Pixel 8",
            unit_price=Decimal("150.0000"),
        )
        galaxy = self._offer(
            run,
            supplier="Acme",
            brand="Samsung",
            model_name="Galaxy S24",
            grade="B",
            unit_price=Decimal("200.0000"),
        )
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        confirmed = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {
                "groups": [
                    {
                        "canonical_model": "iPhone 15",
                        "offer_ids": [iphone.id],
                        "category_id": phones.id,
                    },
                    {
                        "canonical_model": "Pixel 8",
                        "offer_ids": [pixel.id],
                        "category_id": phones.id,
                    },
                    {
                        "canonical_model": "Galaxy S24",
                        "offer_ids": [galaxy.id],
                        "category_id": tablets.id,
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        models = [line["model"] for line in confirmed.data["lines"]]
        # Frozen order: Apple, Google, Samsung.
        self.assertEqual(models, ["iPhone 15", "Pixel 8", "Galaxy S24"])
        edited = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {
                "edits": {
                    "0": {"hidden": True},
                    "2": {"label": "Galaxy S24 Ultra", "hidden": False},
                }
            },
            format="json",
        )
        self.assertEqual(edited.status_code, 200, edited.content)
        # The tablets shelf holds only the renamed Galaxy: the
        # hidden flag on full-list line 0 must not leak onto it.
        shelf = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/?category_id={tablets.id}"
        )
        self.assertEqual(shelf.status_code, 200)
        shelf_body = b"".join(shelf.streaming_content).decode("utf-8-sig")
        self.assertIn("Galaxy S24 Ultra", shelf_body)
        self.assertNotIn("iPhone 15", shelf_body)
        # The full file hides the iPhone and shows the rename.
        full = self._csv_body(quote_id)
        self.assertNotIn("iPhone 15", full)
        self.assertIn("Galaxy S24 Ultra", full)
        self.assertIn("Pixel 8", full)
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/?category_id={tablets.id}"
        ).data["message"]
        self.assertIn("Galaxy S24 Ultra", message)

    def test_line_edits_refused_loudly(self):
        quote_id = self._ready_quote_id()
        unknown = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {"edits": {"7": {"hidden": True}}},
            format="json",
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("does not exist", str(unknown.data))
        blank = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {"edits": {"0": {"label": "  "}}},
            format="json",
        )
        self.assertEqual(blank.status_code, 400)
        self.assertIn("hide the line", str(blank.data))

    def test_line_edits_need_ready_quote_and_role(self):
        self._seed_two_suppliers()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        draft_id = started.data["id"]
        early = self.client.put(
            f"/api/pipeline/quotes/{draft_id}/lines/",
            {"edits": {}},
            format="json",
        )
        self.assertEqual(early.status_code, 400)
        self.assertIn("Confirm the product groups", str(early.data))
        quote_id = self._ready_quote_id()
        self.client.force_authenticate(self.viewer)
        denied = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {"edits": {}},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

    def test_default_template_hides_supplier(self):
        quote_id = self._ready_quote_id()
        body = self._csv_body(quote_id)
        header = body.splitlines()[0]
        self.assertNotIn("Supplier", header)
        self.assertIn("Acme", self.client.get(
            f"/api/pipeline/quotes/{quote_id}/"
        ).data["lines"][0]["supplier"])
        self.assertTrue(body.startswith("Brand,Model,Capacity GB,Grade,"))

    def test_template_customizes_file_and_message(self):
        quote_id = self._ready_quote_id()
        saved = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {
                "quote_export": {
                    "file_columns": ["brand", "model", "price", "currency"],
                    "message_show_capacity": False,
                    "message_show_grade": False,
                    "message_show_quantity": True,
                    "message_show_categories": True,
                    "message_header": "Fresh stock today",
                    "message_footer": "Reply to order",
                }
            },
            format="json",
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(
            saved.data["quote_export"]["file_columns"],
            ["brand", "model", "price", "currency"],
        )
        body = self._csv_body(quote_id)
        self.assertTrue(body.startswith("Brand,Model,Unit Price,Currency"))
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/"
        ).data["message"]
        self.assertTrue(message.startswith("Fresh stock today\n"))
        self.assertTrue(message.endswith("\nReply to order"))
        self.assertIn("Apple iPhone 15 — USD 100.00 ×5", message)
        self.assertNotIn("(A)", message)
        self.assertNotIn("128GB", message)

    def test_supplier_opts_back_into_files(self):
        quote_id = self._ready_quote_id()
        saved = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {
                "quote_export": {
                    "file_columns": [
                        "brand",
                        "model",
                        "supplier",
                        "price",
                        "currency",
                    ]
                }
            },
            format="json",
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        body = self._csv_body(quote_id)
        self.assertTrue(body.startswith("Brand,Model,Supplier,Unit Price,Currency"))
        self.assertIn("Acme", body)

    def test_template_validates_loudly(self):
        unknown = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"quote_export": {"file_columns": ["model", "price", "margin"]}},
            format="json",
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("margin", str(unknown.data))
        missing = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"quote_export": {"file_columns": ["brand", "model"]}},
            format="json",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("price", str(missing.data))
        shape = self.client.put(
            f"/api/orgs/{self.org.id}/settings/",
            {"quote_export": ["model", "price"]},
            format="json",
        )
        self.assertEqual(shape.status_code, 400)
        self.assertIn("must be an object", str(shape.data))

    def test_settings_reports_effective_template(self):
        response = self.client.get(f"/api/orgs/{self.org.id}/settings/")
        self.assertEqual(response.status_code, 200)
        template = response.data["quote_export"]
        self.assertNotIn("supplier", template["file_columns"])
        self.assertIn("price", template["file_columns"])
        self.assertTrue(template["message_show_grade"])


class QuoteReleaseTests(QuotePresentationApiTests):
    def _draft_id(self):
        self._seed_two_products()
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        return started.data["id"]

    def test_draft_release_paths_refused(self):
        draft_id = self._draft_id()
        download = self.client.get(
            f"/api/pipeline/quotes/{draft_id}/download/csv/"
        )
        self.assertEqual(download.status_code, 400)
        self.assertIn("Confirm the product groups", str(download.data))
        message = self.client.get(f"/api/pipeline/quotes/{draft_id}/message/")
        self.assertEqual(message.status_code, 400)
        self.assertIn("Confirm the product groups", str(message.data))

    def test_release_manifest_names_exact_revision(self):
        quote_id = self._ready_quote_id()
        message = self.client.get(f"/api/pipeline/quotes/{quote_id}/message/")
        self.assertEqual(message.status_code, 200)
        manifest = message.data["release"]
        self.assertEqual(manifest["quote_id"], quote_id)
        self.assertEqual(manifest["revision"], 1)
        self.assertTrue(manifest["lines_digest"])
        self.assertTrue(manifest["template_digest"])
        edited = self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {"edits": {"1": {"hidden": True}}},
            format="json",
        )
        self.assertEqual(edited.status_code, 200)
        reread = self.client.get(f"/api/pipeline/quotes/{quote_id}/message/")
        self.assertEqual(reread.status_code, 200)
        self.assertEqual(reread.data["release"]["revision"], 2)
        self.assertNotEqual(
            reread.data["release"]["lines_digest"], manifest["lines_digest"]
        )
        detail = self.client.get(f"/api/pipeline/quotes/{quote_id}/")
        self.assertEqual(detail.data["revision"], 2)

    def test_tampered_lines_refused(self):
        quote_id = self._ready_quote_id()
        quote = CustomerQuote.objects.get(pk=quote_id)
        lines = list(quote.lines)
        lines[0] = dict(lines[0], unit_price="0.01")
        CustomerQuote.objects.filter(pk=quote_id).update(lines=lines)
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("audited edit flow", str(response.data))

    def test_legacy_unbound_quote_refused(self):
        quote = CustomerQuote.objects.create(
            organization=self.org,
            created_by=self.owner,
            status="ready",
            lines=[{"model": "X", "quantity": 1, "unit_price": "5", "currency": "EUR"}],
        )
        response = self.client.get(
            f"/api/pipeline/quotes/{quote.id}/download/csv/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("audited edit flow", str(response.data))

    def test_changed_source_invalidates_quote(self):
        quote_id = self._ready_quote_id()
        quote = CustomerQuote.objects.get(pk=quote_id)
        run = ImportRun.objects.get(pk=quote.source_run_ids[0])
        self._offer(run, supplier="Latecomer", model_name="iPhone 15")
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed since confirmation", str(response.data))

    def test_unapproved_source_invalidates_quote(self):
        quote_id = self._ready_quote_id()
        quote = CustomerQuote.objects.get(pk=quote_id)
        ImportRun.objects.filter(pk=quote.source_run_ids[0]).update(
            status=RunStatus.UPLOADED
        )
        response = self.client.get(f"/api/pipeline/quotes/{quote_id}/message/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer approved", str(response.data))

    def test_deleted_source_invalidates_quote(self):
        quote_id = self._ready_quote_id()
        quote = CustomerQuote.objects.get(pk=quote_id)
        ImportRun.objects.filter(pk=quote.source_run_ids[0]).delete()
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("deleted", str(response.data))

    def test_message_file_and_detail_agree(self):
        quote_id = self._ready_quote_id()
        self.client.put(
            f"/api/pipeline/quotes/{quote_id}/lines/",
            {"edits": {"0": {"label": "IPHONE 15 128GB GRADE A"}}},
            format="json",
        )
        message = self.client.get(f"/api/pipeline/quotes/{quote_id}/message/")
        body = self._csv_body(quote_id)
        detail = self.client.get(f"/api/pipeline/quotes/{quote_id}/")
        self.assertIn("IPHONE 15 128GB GRADE A", message.data["message"])
        self.assertIn("IPHONE 15 128GB GRADE A", body)
        self.assertEqual(
            detail.data["line_edits"]["L1"]["label"], "IPHONE 15 128GB GRADE A"
        )
