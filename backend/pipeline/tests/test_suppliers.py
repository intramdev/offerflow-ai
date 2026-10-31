"""Suppliers: import auto-discovery, favorites, ratings, notes."""

import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization
from pipeline.jobs import process_import_run
from pipeline.models import (
    ImportRun,
    NormalizedOffer,
    RuleSetVersion,
    SourceFile,
    SourceRow,
    Supplier,
    SupplierNote,
)
from pipeline.suppliers import ensure_suppliers
from pipeline.tests.test_parsing import HEADERS, make_workbook
from pipeline.uploads import create_upload

User = get_user_model()


def row(supplier="Acme"):
    return [
        supplier,
        "Pear",
        "P1",
        "128GB",
        "Black",
        "A",
        "Unlocked",
        5,
        299.99,
        "USD",
        "",
    ]


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class EnsureSuppliersTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")

    def test_creates_missing_and_reuses_case_insensitively(self):
        first = ensure_suppliers(self.org.id, ["Acme Ltd", "  ", ""])
        self.assertEqual([s.name for s in first], ["Acme Ltd"])
        second = ensure_suppliers(self.org.id, ["acme ltd", "ACME LTD", "Globex"])
        self.assertEqual(Supplier.objects.count(), 2)
        self.assertEqual(second[0].id, first[0].id)
        self.assertEqual(second[0].name, "Acme Ltd")
        self.assertEqual(second[1].name, "Globex")

    def test_scoped_per_company(self):
        other = Organization.objects.create(name="Other")
        ensure_suppliers(self.org.id, ["Acme"])
        ensure_suppliers(other.id, ["Acme"])
        self.assertEqual(Supplier.objects.count(), 2)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SyncSuppliersCommandTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.other = Organization.objects.create(name="Other")
        rule = RuleSetVersion.objects.get(version="v1")
        source_file = SourceFile.objects.create(
            organization=self.org,
            original_name="sup.xlsx",
            size_bytes=10,
            sha256="ab" * 32,
            storage_key="org/1/1",
        )
        run = ImportRun.objects.create(
            organization=self.org,
            source_file=source_file,
            rule_version=rule,
            status="completed",
        )
        for index, supplier in enumerate(["Acme Ltd", "acme ltd", "Globex"]):
            row = SourceRow.objects.create(
                organization=self.org,
                run=run,
                index=index,
                sheet="Sheet1",
                data={},
                provenance={},
            )
            NormalizedOffer.objects.create(
                organization=self.org,
                run=run,
                source_row=row,
                supplier=supplier,
                status="ok",
            )

    def test_backfills_missing_suppliers(self):
        out = StringIO()
        call_command("sync_suppliers", stdout=out)
        names = sorted(
            Supplier.objects.filter(organization=self.org).values_list(
                "name", flat=True
            )
        )
        self.assertEqual(names, ["Acme Ltd", "Globex"])
        self.assertIn("2 suppliers created", out.getvalue())

    def test_second_run_creates_nothing(self):
        call_command("sync_suppliers", stdout=StringIO())
        out = StringIO()
        call_command("sync_suppliers", stdout=out)
        self.assertIn("0 suppliers created", out.getvalue())

    def test_scopes_to_one_organization(self):
        call_command(
            "sync_suppliers",
            organization_id=self.other.id,
            stdout=StringIO(),
        )
        self.assertFalse(
            Supplier.objects.filter(organization=self.org).exists()
        )

    def test_unknown_organization_refuses(self):
        with self.assertRaisesRegex(CommandError, "No organization"):
            call_command(
                "sync_suppliers", organization_id=424242, stdout=StringIO()
            )


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SupplierApiTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.owner = User.objects.create_user(username="owner", password="x")
        self.viewer = User.objects.create_user(username="viewer", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.owner, organization=self.org, role="owner"
        )
        Membership.objects.create(
            user=self.viewer, organization=self.org, role="viewer"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _create(self, name="Acme", **extra):
        payload = {"organization_id": self.org.id, "name": name}
        payload.update(extra)
        response = self.client.post(
            "/api/pipeline/suppliers/", payload, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.data

    def test_import_creates_supplier_records(self):
        uploaded = SimpleUploadedFile(
            "sup.xlsx",
            make_workbook([HEADERS, row("Acme"), row("acme"), row("Globex")]),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        run = create_upload(
            organization=self.org, user=self.owner, uploaded_file=uploaded
        )
        self.assertEqual(process_import_run(run.id), "ok")
        self.assertEqual(
            sorted(Supplier.objects.values_list("name", flat=True)),
            ["Acme", "Globex"],
        )

    def test_create_and_list_scope_to_org_with_favorites_first(self):
        first = self._create("Globex")
        second = self._create("Acme")
        other = Organization.objects.create(name="Other")
        Supplier.objects.create(organization=other, name="Zeta")
        ranked = self.client.put(
            f"/api/pipeline/suppliers/order/?organization_id={self.org.id}",
            {"ordered_ids": [first["id"], second["id"]]},
            format="json",
        )
        self.assertEqual(ranked.status_code, 200, ranked.content)
        listed = self.client.get(
            f"/api/pipeline/suppliers/?organization_id={self.org.id}"
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [(row["name"], row["favorite_rank"]) for row in listed.data],
            [("Globex", 1), ("Acme", 2)],
        )

    def test_duplicate_name_refused_up_to_case(self):
        self._create("Acme")
        response = self.client.post(
            "/api/pipeline/suppliers/",
            {"organization_id": self.org.id, "name": "acme"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", str(response.data))

    def test_details_ratings_and_customs_round_trip(self):
        created = self._create("Acme")
        updated = self.client.put(
            f"/api/pipeline/suppliers/{created['id']}/",
            {
                "location": "Shenzhen, CN",
                "customs_required": True,
                "customs_fee_percent": "12.50",
                "rating_shipping": 5,
                "rating_quality": 4,
                "rating_payment": 3,
                "rating_quantity": 2,
                "rating_rma": 1,
            },
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.data["location"], "Shenzhen, CN")
        self.assertTrue(updated.data["customs_required"])
        self.assertEqual(str(updated.data["customs_fee_percent"]), "12.50")
        self.assertEqual(updated.data["rating_shipping"], 5)
        self.assertEqual(updated.data["rating_rma"], 1)

    def test_rating_bounds_refused(self):
        created = self._create("Acme")
        for bad in (0, 6, "high", True):
            response = self.client.put(
                f"/api/pipeline/suppliers/{created['id']}/",
                {"rating_quality": bad},
                format="json",
            )
            self.assertEqual(response.status_code, 400, bad)
        cleared = self.client.put(
            f"/api/pipeline/suppliers/{created['id']}/",
            {"rating_quality": None},
            format="json",
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["rating_quality"])

    def test_fee_bounds_refused(self):
        created = self._create("Acme")
        for bad in ("-1", "101", "lots"):
            response = self.client.put(
                f"/api/pipeline/suppliers/{created['id']}/",
                {"customs_fee_percent": bad},
                format="json",
            )
            self.assertEqual(response.status_code, 400, bad)

    def test_delete_supplier(self):
        created = self._create("Acme")
        deleted = self.client.delete(f"/api/pipeline/suppliers/{created['id']}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(Supplier.objects.count(), 0)

    def test_order_validates_ids(self):
        first = self._create("Acme")
        second = self._create("Globex")
        other = Organization.objects.create(name="Other")
        foreign = Supplier.objects.create(organization=other, name="Zeta")
        url = f"/api/pipeline/suppliers/order/?organization_id={self.org.id}"
        duped = self.client.put(
            url, {"ordered_ids": [first["id"], first["id"]]}, format="json"
        )
        self.assertEqual(duped.status_code, 400)
        self.assertIn("twice", str(duped.data))
        crossed = self.client.put(
            url, {"ordered_ids": [first["id"], foreign.id]}, format="json"
        )
        self.assertEqual(crossed.status_code, 400)
        self.assertIn("Unknown suppliers", str(crossed.data))
        missing = self.client.put(url, {"ordered_ids": [424242]}, format="json")
        self.assertEqual(missing.status_code, 400)
        # Unlisted suppliers lose their rank.
        self.client.put(url, {"ordered_ids": [second["id"]]}, format="json")
        ranks = dict(
            Supplier.objects.filter(organization=self.org).values_list(
                "name", "favorite_rank"
            )
        )
        self.assertEqual(ranks, {"Acme": None, "Globex": 1})
        cleared = self.client.put(url, {"ordered_ids": []}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.data, [])
        self.assertFalse(
            Supplier.objects.filter(favorite_rank__isnull=False).exists()
        )

    def test_order_needs_organization(self):
        response = self.client.put(
            "/api/pipeline/suppliers/order/", {"ordered_ids": []}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_notes_append_list_delete(self):
        created = self._create("Acme")
        url = f"/api/pipeline/suppliers/{created['id']}/notes/"
        blank = self.client.post(url, {"text": "  "}, format="json")
        self.assertEqual(blank.status_code, 400)
        first = self.client.post(url, {"text": "First call."}, format="json")
        second = self.client.post(url, {"text": "Second call."}, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.data["author_name"], "owner")
        listed = self.client.get(url)
        self.assertEqual(
            [note["text"] for note in listed.data],
            ["Second call.", "First call."],
        )
        deleted = self.client.delete(f"{url}{first.data['id']}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(SupplierNote.objects.count(), 1)

    def test_viewer_reads_but_cannot_write(self):
        created = self._create("Acme")
        self.client.force_authenticate(self.viewer)
        listed = self.client.get(
            f"/api/pipeline/suppliers/?organization_id={self.org.id}"
        )
        self.assertEqual(listed.status_code, 200)
        notes = self.client.get(
            f"/api/pipeline/suppliers/{created['id']}/notes/"
        )
        self.assertEqual(notes.status_code, 200)
        refused = self.client.post(
            "/api/pipeline/suppliers/",
            {"organization_id": self.org.id, "name": "Globex"},
            format="json",
        )
        self.assertEqual(refused.status_code, 403)
        ranked = self.client.put(
            f"/api/pipeline/suppliers/order/?organization_id={self.org.id}",
            {"ordered_ids": [created["id"]]},
            format="json",
        )
        self.assertEqual(ranked.status_code, 403)
        noted = self.client.post(
            f"/api/pipeline/suppliers/{created['id']}/notes/",
            {"text": "Hi."},
            format="json",
        )
        self.assertEqual(noted.status_code, 403)

    def test_stranger_gets_nothing(self):
        created = self._create("Acme")
        stranger = User.objects.create_user(username="stranger", password="x")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(user=stranger, organization=other, role="owner")
        self.client.force_authenticate(stranger)
        detail = self.client.get(f"/api/pipeline/suppliers/{created['id']}/")
        self.assertEqual(detail.status_code, 404)
        listed = self.client.get("/api/pipeline/suppliers/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data, [])
