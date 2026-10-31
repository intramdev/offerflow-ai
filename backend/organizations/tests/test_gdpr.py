"""GDPR self-service: full export archive and account erasure."""

import io
import json
import tempfile
import zipfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from common.storage import LocalFileAdapter
from organizations.models import Membership, Organization
from pipeline.models import ImportRun, RuleSetVersion, SourceFile


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class GdprApiTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.owner = User.objects.create_user(
            username="owner", password="x", email="owner@example.com"
        )
        self.staff = User.objects.create_user(
            username="staff", password="x", is_staff=True
        )
        self.org = Organization.objects.create(name="Acme")
        Membership.objects.create(
            user=self.owner, organization=self.org, role="owner"
        )
        self.other = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=self.owner, organization=self.other, role="viewer"
        )
        Membership.objects.create(
            user=self.staff, organization=self.org, role="admin"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _stored_file(self):
        from django.conf import settings

        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        key = adapter.build_key(str(self.org.id), "uploads", "price.xlsx")
        adapter.save_bytes(key, b"fake-bytes")
        rule, _ = RuleSetVersion.objects.get_or_create(
            version="v1", defaults={"is_active": True}
        )
        source_file = SourceFile.objects.create(
            organization=self.org,
            uploaded_by=self.owner,
            original_name="price.xlsx",
            size_bytes=10,
            sha256="ab" * 32,
            storage_key=key,
        )
        run = ImportRun.objects.create(
            organization=self.org,
            source_file=source_file,
            rule_version=rule,
            status="completed",
        )
        return source_file, run

    def test_export_contains_rows_and_bytes(self):
        self._stored_file()
        response = self.client.get(f"/api/orgs/{self.org.id}/gdpr/export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))
        payload = json.loads(archive.read("data.json"))
        self.assertEqual(payload["organization"]["name"], "Acme")
        self.assertEqual(len(payload["members"]), 2)
        self.assertEqual(payload["members"][0]["user"]["email"], "owner@example.com")
        self.assertNotIn("password", json.dumps(payload))
        self.assertEqual(len(payload["supplier_files"]), 1)
        self.assertEqual(len(payload["import_runs"]), 1)
        names = archive.namelist()
        self.assertIn("manifest.json", names)
        self.assertTrue(any(name.startswith("uploads/") for name in names))
        upload_name = next(
            name for name in names if name.startswith("uploads/")
        )
        self.assertEqual(archive.read(upload_name), b"fake-bytes")

    def test_export_requires_owner(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(f"/api/orgs/{self.org.id}/gdpr/export/")
        self.assertEqual(response.status_code, 403)

    def test_delete_needs_typed_name(self):
        response = self.client.post(
            f"/api/orgs/{self.org.id}/gdpr/delete/",
            {"confirm": "Wrong"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            Organization.objects.filter(id=self.org.id).exists()
        )

    def test_delete_blocked_by_active_run(self):
        _, run = self._stored_file()
        run.status = "parsing"
        run.save(update_fields=["status"])
        response = self.client.post(
            f"/api/orgs/{self.org.id}/gdpr/delete/",
            {"confirm": "Acme"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            Organization.objects.filter(id=self.org.id).exists()
        )

    def test_delete_erases_org_bytes_and_orphans(self):
        lone = User.objects.create_user(username="lone", password="x")
        Membership.objects.create(
            user=lone, organization=self.org, role="viewer"
        )
        source_file, _ = self._stored_file()
        from django.conf import settings

        adapter = LocalFileAdapter(settings.OFFERFLOW_FILE_ROOT)
        response = self.client.post(
            f"/api/orgs/{self.org.id}/gdpr/delete/",
            {"confirm": "Acme"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["organization"], "Acme")
        self.assertEqual(body["stored_files_removed"], 1)
        self.assertEqual(body["member_logins_deleted"], 1)
        self.assertFalse(
            Organization.objects.filter(id=self.org.id).exists()
        )
        self.assertFalse(
            SourceFile.objects.filter(organization_id=self.org.id).exists()
        )
        self.assertFalse(adapter.exists(source_file.storage_key))
        # The owner survives (member of Other), staff survives (staff),
        # the lone member login is gone.
        self.assertTrue(User.objects.filter(username="owner").exists())
        self.assertTrue(User.objects.filter(username="staff").exists())
        self.assertFalse(User.objects.filter(username="lone").exists())
        self.assertTrue(
            Organization.objects.filter(name="Other").exists()
        )

    def test_delete_requires_owner(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/orgs/{self.org.id}/gdpr/delete/",
            {"confirm": "Acme"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
