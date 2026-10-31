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
        self.assertEqual(
            response.data["line_edits"],
            {
                "0": {"label": "iPhone 15 Pro Max", "hidden": False},
                "1": {"label": None, "hidden": True},
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
        self.assertIn("Apple iPhone 15 — USD 100 ×5", message)
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
