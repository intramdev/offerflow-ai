"""Upload size limit (200 MB), processing guards, and cleanup.

The byte cap and the processing caps are independent: a file that fits
in bytes can still be refused for too many rows, and an unsupported
extension fails on its type even when it also exceeds the size limit.
"""

import os
import tempfile
import tracemalloc
import zipfile
from io import BytesIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Membership, Organization, OrganizationRole
from pipeline.parsing import (
    UnsupportedFile,
    UploadTooLarge,
    check_upload_safety,
    format_megabytes,
    parse_delimited,
)
from pipeline.previews import cancel_preview, create_preview
from pipeline.uploads import create_upload

User = get_user_model()

LIMIT = 200_000_000


def _csv(rows: int, *, cell_pad: int = 0) -> bytes:
    lines = ["supplier,model,price"]
    for number in range(rows):
        model = f"P{number}"
        if cell_pad:
            model += "x" * cell_pad
        lines.append(f"Acme,{model},{100 + number}")
    return "\n".join(lines).encode("utf-8")


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class UploadLimitTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.user = User.objects.create_user(
            username="uploader", email="u@example.test", password="x"
        )
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.user, organization=self.org, role=OrganizationRole.OWNER
        )

    def test_default_limit_is_200mb(self):
        self.assertEqual(settings.OFFERFLOW_UPLOAD_MAX_BYTES, LIMIT)
        self.assertEqual(format_megabytes(LIMIT), "200 MB")

    def test_byte_boundary_below_at_above(self):
        with override_settings(OFFERFLOW_UPLOAD_MAX_BYTES=100):
            check_upload_safety(b"x" * 99, "small.csv", max_bytes=100,
                                max_uncompressed_bytes=1000)
            check_upload_safety(b"x" * 100, "exact.csv", max_bytes=100,
                                max_uncompressed_bytes=1000)
            with self.assertRaisesRegex(UnsupportedFile, "must be .* or smaller"):
                check_upload_safety(b"x" * 101, "big.csv", max_bytes=100,
                                    max_uncompressed_bytes=1000)

    def test_real_200mb_boundary(self):
        check_upload_safety(
            b"x" * LIMIT, "exact.csv", max_bytes=LIMIT,
            max_uncompressed_bytes=1000,
        )
        with self.assertRaisesRegex(UnsupportedFile, "200 MB or smaller"):
            check_upload_safety(
                b"x" * (LIMIT + 1), "over.csv", max_bytes=LIMIT,
                max_uncompressed_bytes=1000,
            )

    def test_unsupported_type_wins_over_size(self):
        with self.assertRaisesRegex(UnsupportedFile, "Unsupported file type"):
            check_upload_safety(
                b"x" * (11 * 1024 * 1024), "huge.pdf", max_bytes=LIMIT,
                max_uncompressed_bytes=1000,
            )

    def test_over_10mb_supported_file_imports(self):
        content = _csv(2000, cell_pad=6000)
        self.assertGreater(len(content), 10 * 1024 * 1024)
        preview = create_preview(
            organization=self.org,
            user=self.user,
            uploaded_file=SimpleUploadedFile(
                "fat.csv", content, content_type="text/csv"
            ),
        )
        try:
            self.assertTrue(preview["key"])
            self.assertTrue(
                os.path.exists(
                    os.path.join(self.tmp.name, "previews", f"{preview['key']}.bin")
                )
            )
        finally:
            cancel_preview(key=preview["key"], user=self.user)
        self.assertFalse(
            os.path.exists(
                os.path.join(self.tmp.name, "previews", f"{preview['key']}.bin")
            )
        )

    def test_200mb_table_refused_for_rows_not_bytes(self):
        content = _csv(5000, cell_pad=39_000)
        self.assertGreater(len(content), 10 * 1024 * 1024)
        self.assertLessEqual(len(content), LIMIT)
        with self.assertRaisesRegex(Exception, "row intake limit"):
            create_preview(
                organization=self.org,
                user=self.user,
                uploaded_file=SimpleUploadedFile(
                    "tall.csv", content, content_type="text/csv"
                ),
            )

    def test_delimited_guard_triggers_early(self):
        with self.assertRaisesRegex(UnsupportedFile, "row intake limit"):
            parse_delimited(
                _csv(5000),
                "tall.csv",
                max_rows=2000,
                max_cols=100,
                header_scan_rows=20,
            )

    def test_parse_memory_stays_bounded(self):
        content = _csv(2000, cell_pad=2000)
        tracemalloc.start()
        try:
            parse_delimited(
                content,
                "fat.csv",
                max_rows=2000,
                max_cols=100,
                header_scan_rows=20,
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 15 * len(content))

    def test_malformed_spreadsheet_refused(self):
        with self.assertRaisesRegex(UnsupportedFile, "readable spreadsheet"):
            check_upload_safety(
                b"not a zip" * 100, "bad.xlsx", max_bytes=LIMIT,
                max_uncompressed_bytes=1000,
            )

    def test_archive_expansion_guard(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"0" * 1_000_000)
        content = buffer.getvalue()
        self.assertLess(len(content), 10_000)
        with self.assertRaisesRegex(UnsupportedFile, "expands beyond"):
            check_upload_safety(
                content, "bomb.xlsx", max_bytes=LIMIT,
                max_uncompressed_bytes=1000,
            )

    def test_oversize_is_upload_too_large(self):
        with self.assertRaises(UploadTooLarge):
            check_upload_safety(
                b"x" * (LIMIT + 1), "over.csv", max_bytes=LIMIT,
                max_uncompressed_bytes=1000,
            )
        # Still an UnsupportedFile for every existing guard.
        self.assertTrue(issubclass(UploadTooLarge, UnsupportedFile))

    def test_precheck_refuses_without_reading(self):
        class UnreadableFile:
            name = "huge.csv"
            size = LIMIT + 1

            def read(self):
                raise AssertionError("refused uploads must not be read")

        with self.assertRaises(UploadTooLarge):
            create_upload(
                organization=self.org,
                user=self.user,
                uploaded_file=UnreadableFile(),
            )
        with self.assertRaises(UploadTooLarge):
            create_preview(
                organization=self.org,
                user=self.user,
                uploaded_file=UnreadableFile(),
            )

    def test_precheck_unsupported_wins_without_reading(self):
        class UnreadableFile:
            name = "huge.exe"
            size = LIMIT + 1

            def read(self):
                raise AssertionError("refused uploads must not be read")

        from django.core.exceptions import ValidationError

        with self.assertRaisesRegex(ValidationError, "Unsupported file type"):
            create_upload(
                organization=self.org,
                user=self.user,
                uploaded_file=UnreadableFile(),
            )

    def test_http_boundary_below_at_above(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        with override_settings(OFFERFLOW_UPLOAD_MAX_BYTES=100):
            for size, expected in ((99, 201), (100, 201), (101, 413)):
                upload = SimpleUploadedFile(
                    "probe.csv", b"x" * size, content_type="text/csv"
                )
                response = client.post(
                    "/api/pipeline/uploads/",
                    {"organization_id": self.org.id, "file": upload},
                    format="multipart",
                )
                self.assertEqual(
                    response.status_code, expected,
                    f"size {size}: {response.content!r}",
                )
        rejected = client.post(
            "/api/pipeline/uploads/",
            {
                "organization_id": self.org.id,
                "file": SimpleUploadedFile(
                    "probe.csv", b"x" * 101, content_type="text/csv"
                ),
            },
            format="multipart",
        )
        # Real limit restored: the 101-byte file fits again.
        self.assertEqual(rejected.status_code, 201, rejected.content)

    def test_http_413_message_names_limit(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        with override_settings(OFFERFLOW_UPLOAD_MAX_BYTES=100):
            response = client.post(
                "/api/pipeline/uploads/",
                {
                    "organization_id": self.org.id,
                    "file": SimpleUploadedFile(
                        "over.csv", b"x" * 101, content_type="text/csv"
                    ),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 413, response.content)
        self.assertIn("or smaller", response.json()["detail"])

    def test_http_unsupported_wins_over_size(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        with override_settings(OFFERFLOW_UPLOAD_MAX_BYTES=100):
            response = client.post(
                "/api/pipeline/uploads/",
                {
                    "organization_id": self.org.id,
                    "file": SimpleUploadedFile(
                        "huge.exe", b"x" * 101,
                        content_type="application/octet-stream",
                    ),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Unsupported file type", response.json()["detail"])

    def test_http_preview_413(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        with override_settings(OFFERFLOW_UPLOAD_MAX_BYTES=100):
            response = client.post(
                "/api/pipeline/previews/",
                {
                    "organization_id": self.org.id,
                    "file": SimpleUploadedFile(
                        "over.csv", b"x" * 101, content_type="text/csv"
                    ),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 413, response.content)

    def test_storage_failure_leaves_no_phantom_preview(self):
        content = _csv(3)
        with mock.patch(
            "pipeline.previews.LocalFileAdapter.save_bytes",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                create_preview(
                    organization=self.org,
                    user=self.user,
                    uploaded_file=SimpleUploadedFile(
                        "small.csv", content, content_type="text/csv"
                    ),
                )
        previews = os.path.join(self.tmp.name, "previews")
        leftovers = os.listdir(previews) if os.path.isdir(previews) else []
        self.assertEqual(leftovers, [])
