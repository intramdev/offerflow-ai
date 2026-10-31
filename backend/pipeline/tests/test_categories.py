"""Category API: company CRUD, file mapping, confirm learning."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
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
)

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CategoryApiTests(TestCase):
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

    def _run(self, original_name="sup.xlsx", org=None):
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
            status=RunStatus.APPROVED,
        )
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

    def _create(self, name="Phones", organization_id=None, expected=201):
        response = self.client.post(
            "/api/pipeline/categories/",
            {"organization_id": organization_id or self.org.id, "name": name},
            format="json",
        )
        self.assertEqual(response.status_code, expected, response.content)
        return response.data

    def test_create_and_list_scope_to_org(self):
        phones = self._create("Phones")
        self.assertEqual(phones["name"], "Phones")
        other = Organization.objects.create(name="Other")
        Category.objects.create(organization=other, name="Tablets")
        listed = self.client.get(
            f"/api/pipeline/categories/?organization_id={self.org.id}"
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([c["name"] for c in listed.data], ["Phones"])

    def test_create_needs_organization_and_name(self):
        response = self.client.post(
            "/api/pipeline/categories/", {"name": "Phones"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        blank = self.client.post(
            "/api/pipeline/categories/",
            {"organization_id": self.org.id, "name": "  "},
            format="json",
        )
        self.assertEqual(blank.status_code, 400)
        self.assertIn("needs a name", str(blank.data))

    def test_duplicate_name_refused_up_to_case(self):
        self._create("Phones")
        again = self.client.post(
            "/api/pipeline/categories/",
            {"organization_id": self.org.id, "name": "phones"},
            format="json",
        )
        self.assertEqual(again.status_code, 400)
        self.assertIn("already exists", str(again.data))

    def test_rename_and_delete(self):
        created = self._create("Phones")
        renamed = self.client.put(
            f"/api/pipeline/categories/{created['id']}/",
            {"name": "Smartphones"},
            format="json",
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.data["name"], "Smartphones")
        deleted = self.client.delete(
            f"/api/pipeline/categories/{created['id']}/"
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(Category.objects.count(), 0)

    def test_rename_conflict_refused(self):
        self._create("Phones")
        other = self._create("Tablets")
        response = self.client.put(
            f"/api/pipeline/categories/{other['id']}/",
            {"name": "PHONES"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", str(response.data))

    def test_delete_unmaps_files_and_aliases(self):
        phones = self._create("Phones")
        run = self._run()
        run.source_file.category_id = phones["id"]
        run.source_file.save(update_fields=["category"])
        ProductAlias.objects.create(
            organization=self.org,
            supplier_norm="a",
            alias_norm="iphone15",
            brand_norm="apple",
            capacity_gb=128,
            grade_norm="a",
            canonical_model="iPhone 15",
            category_id=phones["id"],
        )
        deleted = self.client.delete(f"/api/pipeline/categories/{phones['id']}/")
        self.assertEqual(deleted.status_code, 204)
        run.source_file.refresh_from_db()
        self.assertIsNone(run.source_file.category)
        self.assertIsNone(ProductAlias.objects.get().category)

    def test_viewer_reads_but_cannot_write(self):
        created = self._create("Phones")
        self.client.force_authenticate(self.viewer)
        listed = self.client.get(
            f"/api/pipeline/categories/?organization_id={self.org.id}"
        )
        self.assertEqual(listed.status_code, 200)
        refused = self.client.post(
            "/api/pipeline/categories/",
            {"organization_id": self.org.id, "name": "Tablets"},
            format="json",
        )
        self.assertEqual(refused.status_code, 403)
        renamed = self.client.put(
            f"/api/pipeline/categories/{created['id']}/",
            {"name": "Smartphones"},
            format="json",
        )
        self.assertEqual(renamed.status_code, 403)
        deleted = self.client.delete(
            f"/api/pipeline/categories/{created['id']}/"
        )
        self.assertEqual(deleted.status_code, 403)

    def test_stranger_gets_nothing(self):
        created = self._create("Phones")
        stranger = User.objects.create_user(username="stranger", password="x")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(user=stranger, organization=other, role="owner")
        self.client.force_authenticate(stranger)
        detail = self.client.get(f"/api/pipeline/categories/{created['id']}/")
        self.assertEqual(detail.status_code, 404)
        listed = self.client.get("/api/pipeline/categories/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data, [])

    def test_file_category_set_and_cleared(self):
        phones = self._create("Phones")
        run = self._run()
        mapped = self.client.put(
            f"/api/pipeline/files/{run.source_file_id}/category/",
            {"category_id": phones["id"]},
            format="json",
        )
        self.assertEqual(mapped.status_code, 200, mapped.content)
        self.assertEqual(mapped.data["category"], phones["id"])
        self.assertEqual(mapped.data["category_name"], "Phones")
        cleared = self.client.put(
            f"/api/pipeline/files/{run.source_file_id}/category/",
            {"category_id": None},
            format="json",
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["category"])
        self.assertEqual(cleared.data["category_name"], "")

    def test_file_category_rejects_foreign_company(self):
        other = Organization.objects.create(name="Other")
        foreign = Category.objects.create(organization=other, name="Phones")
        run = self._run()
        response = self.client.put(
            f"/api/pipeline/files/{run.source_file_id}/category/",
            {"category_id": foreign.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown category", str(response.data))

    def test_confirm_learns_category_and_prices_it(self):
        phones = self._create("Phones")
        run_a = self._run(original_name="a.xlsx")
        first = self._offer(run_a, supplier="Acme", model_name="iPhone 15")
        run_b = self._run(original_name="b.xlsx")
        second = self._offer(run_b, supplier="Globex", model_name="IPHONE-15")
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
                        "offer_ids": [first.id, second.id],
                        "category_id": phones["id"],
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        line = confirmed.data["lines"][0]
        self.assertEqual(line["category_id"], phones["id"])
        self.assertEqual(line["category_name"], "Phones")
        self.assertIn("Phones:", confirmed.data["message"])
        aliases = ProductAlias.objects.all()
        self.assertEqual(aliases.count(), 2)
        self.assertTrue(all(a.category_id == phones["id"] for a in aliases))

    def test_confirm_refuses_unknown_category(self):
        run = self._run()
        offer = self._offer(run)
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
                        "offer_ids": [offer.id],
                        "category_id": 424242,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown category", str(response.data))

    def test_download_adds_category_column_when_categorized(self):
        phones = self._create("Phones")
        run = self._run()
        offer = self._offer(run)
        started = self.client.post(
            "/api/pipeline/quotes/", {"organization_id": self.org.id}, format="json"
        )
        quote_id = started.data["id"]
        self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {
                "groups": [
                    {
                        "canonical_model": "iPhone 15",
                        "offer_ids": [offer.id],
                        "category_id": phones["id"],
                    }
                ]
            },
            format="json",
        )
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/"
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = body.strip().splitlines()
        self.assertTrue(rows[0].endswith(",Category"))
        self.assertTrue(rows[1].endswith(",Phones"))

    def _ready_two_shelf_quote(self):
        phones = self._create("Phones")
        tablets = self._create("Tablets")
        run = self._run()
        phone = self._offer(run)
        tablet = self._offer(
            run,
            supplier="Supplier B",
            brand="Samsung",
            model_name="Galaxy Tab",
        )
        plain = self._offer(
            run,
            supplier="Supplier C",
            brand="Nokia",
            model_name="3310",
        )
        started = self.client.post(
            "/api/pipeline/quotes/",
            {"organization_id": self.org.id},
            format="json",
        )
        quote_id = started.data["id"]
        confirmed = self.client.post(
            f"/api/pipeline/quotes/{quote_id}/confirm/",
            {
                "groups": [
                    {
                        "canonical_model": "iPhone 15",
                        "offer_ids": [phone.id],
                        "category_id": phones["id"],
                    },
                    {
                        "canonical_model": "Galaxy Tab",
                        "offer_ids": [tablet.id],
                        "category_id": tablets["id"],
                    },
                    {
                        "canonical_model": "3310",
                        "offer_ids": [plain.id],
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        return quote_id, phones, tablets

    def test_download_filters_to_one_category(self):
        quote_id, phones, _ = self._ready_two_shelf_quote()
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/",
            {"category_id": phones["id"]},
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = body.strip().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertIn("iPhone 15", rows[1])
        self.assertNotIn("Galaxy Tab", body)
        self.assertNotIn("Category", rows[0])
        self.assertIn("phones.csv", response["Content-Disposition"])

    def test_download_filters_to_uncategorized(self):
        quote_id, _, _ = self._ready_two_shelf_quote()
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/",
            {"category_id": "none"},
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("3310", body)
        self.assertNotIn("iPhone 15", body)

    def test_download_refuses_unknown_category(self):
        quote_id, _, _ = self._ready_two_shelf_quote()
        response = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/download/csv/",
            {"category_id": 424242},
        )
        self.assertEqual(response.status_code, 400)

    def test_message_returns_frozen_text_by_default(self):
        quote_id, _, _ = self._ready_two_shelf_quote()
        detail = self.client.get(f"/api/pipeline/quotes/{quote_id}/")
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/"
        )
        self.assertEqual(message.status_code, 200)
        self.assertEqual(message.data["message"], detail.data["message"])
        self.assertIn("Phones:", message.data["message"])
        self.assertIn("Tablets:", message.data["message"])

    def test_message_filters_to_one_category(self):
        quote_id, phones, _ = self._ready_two_shelf_quote()
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/",
            {"category_id": phones["id"]},
        )
        self.assertEqual(message.status_code, 200)
        self.assertIn("iPhone 15", message.data["message"])
        self.assertNotIn("Galaxy Tab", message.data["message"])

    def test_message_refuses_unknown_category(self):
        quote_id, _, _ = self._ready_two_shelf_quote()
        message = self.client.get(
            f"/api/pipeline/quotes/{quote_id}/message/",
            {"category_id": "bogus"},
        )
        self.assertEqual(message.status_code, 400)
