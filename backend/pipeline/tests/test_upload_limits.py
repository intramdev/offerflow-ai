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


ARGS = {"max_rows": 2000, "max_cols": 100, "header_scan_rows": 20}


def _full(content: bytes, name="sheet.csv", **kw):
    from pipeline.parsing import parse_upload

    args = dict(ARGS)
    args.update(kw)
    return parse_upload(content, name, **args)


def _sample(content: bytes, name="sheet.csv", budget=5, **kw):
    from pipeline.parsing import parse_delimited_sample

    args = dict(ARGS)
    args.update(kw)
    title = name.rsplit("/", 1)[-1]
    return parse_delimited_sample(content, title, data_row_budget=budget, **args)


class DelimitedSampleEquivalenceTests(TestCase):
    """The bounded sample parse must match the full parse exactly."""

    def test_headers_data_total_and_indices_match(self):
        content = (
            "junk line one\n"
            "junk line two\n"
            "\n"
            "Supplier,Model,Price,Quantity,Currency,Notes\n"
            'Acme,"P1, Pro",19.95,3,EUR,"a note\nwith newline"\n'
            "\n"
            "Acme,P2,29.95,7,EUR,plain\n"
            "Acme,P3,9.95,,EUR,\n"
        ).encode("utf-8")
        sheet, total = _sample(content, budget=5)
        full = _full(content)
        self.assertEqual(sheet.headers, full.headers)
        self.assertEqual(sheet.column_map, full.column_map)
        self.assertEqual(sheet.sources, full.sources)
        self.assertEqual(total, len(full.rows))
        self.assertEqual(total, 3)
        for head_row, full_row in zip(sheet.rows, full.rows):
            self.assertEqual(head_row.index, full_row.index)
            self.assertEqual(head_row.data, full_row.data)
        self.assertEqual(sheet.rows[0].index, 5)

    def test_missing_columns_carry_same_detail_and_total(self):
        from pipeline.parsing import MappingRequired

        content = b"Supplier,Model,Notes\nAcme,P1,hi\nAcme,P2,ho\n"
        with self.assertRaises(MappingRequired) as ctx:
            _sample(content, budget=5)
        with self.assertRaises(MappingRequired) as full_ctx:
            _full(content)
        self.assertEqual(ctx.exception.missing, full_ctx.exception.missing)
        self.assertEqual(ctx.exception.headers, full_ctx.exception.headers)
        self.assertEqual(ctx.exception.total_rows, len(full_ctx.exception.rows))
        self.assertEqual(len(ctx.exception.rows), len(full_ctx.exception.rows))

    def test_row_limit_refusal_matches(self):
        content = _csv(11)
        with self.assertRaises(UnsupportedFile) as ctx:
            _sample(content, budget=5, max_rows=10)
        with self.assertRaises(UnsupportedFile) as full_ctx:
            _full(content, max_rows=10)
        self.assertEqual(str(ctx.exception), str(full_ctx.exception))

    def test_early_row_cap_refusal_matches(self):
        content = _csv(15)
        with self.assertRaises(UnsupportedFile) as ctx:
            _sample(content, budget=5, max_rows=10, header_scan_rows=2)
        self.assertEqual(str(ctx.exception), "Table exceeds the row intake limit.")

    def test_deep_column_violation_matches(self):
        lines = ["supplier,model,price"] + ["a,b,1"] * 28 + ["a,b,c,d,e"]
        content = "\n".join(lines).encode("utf-8")
        with self.assertRaises(UnsupportedFile) as ctx:
            _sample(content, budget=5, max_cols=3)
        with self.assertRaises(UnsupportedFile) as full_ctx:
            _full(content, max_cols=3)
        self.assertEqual(str(ctx.exception), str(full_ctx.exception))

    def test_row_limit_before_deeper_column_violation(self):
        # Data row 11 already exceeds max_rows=10 at record 12, before
        # the too-wide record 13 is reached, so the row refusal wins —
        # exactly like the full parse.
        lines = ["supplier,model,price"] + ["a,b,1"] * 11 + ["a,b,c,d,e"]
        content = "\n".join(lines).encode("utf-8")
        with self.assertRaises(UnsupportedFile) as ctx:
            _sample(content, budget=5, max_rows=10, max_cols=3)
        with self.assertRaises(UnsupportedFile) as full_ctx:
            _full(content, max_rows=10, max_cols=3)
        self.assertEqual(str(ctx.exception), str(full_ctx.exception))
        self.assertEqual(str(ctx.exception), "Table exceeds the row intake limit.")

    def test_wide_violation_at_same_row_as_row_limit_wins(self):
        # Data row 11 is both over max_rows and too wide: the column
        # check runs first for that row, like the full parse.
        lines = ["supplier,model,price"] + ["a,b,1"] * 10 + ["a,b,c,d,e"]
        content = "\n".join(lines).encode("utf-8")
        with self.assertRaises(UnsupportedFile) as ctx:
            _sample(content, budget=5, max_rows=10, max_cols=4)
        with self.assertRaises(UnsupportedFile) as full_ctx:
            _full(content, max_rows=10, max_cols=4)
        self.assertEqual(str(ctx.exception), str(full_ctx.exception))
        self.assertEqual(
            str(ctx.exception), "Table exceeds the column intake limit."
        )

    def test_tsv_and_crlf_match(self):
        content = (
            "Supplier\tModel\tPrice\r\nAcme\tP1\t19.95\r\nAcme\tP2\t29.95\r\n"
        ).encode("utf-8")
        sheet, total = _sample(
            content, name="sheet.tsv", budget=5, forced_delimiter="\t"
        )
        full = _full(content, name="sheet.tsv")
        self.assertEqual(sheet.headers, full.headers)
        self.assertEqual(total, 2)
        self.assertEqual(sheet.rows[1].data, full.rows[1].data)

    def test_cp1252_bytes_match(self):
        content = "Supplier,Model,Price\nAcme,P1,\u201c9.95\u201d\n".encode("cp1252")
        sheet, total = _sample(content, budget=5)
        full = _full(content)
        self.assertEqual(sheet.headers, full.headers)
        self.assertEqual(total, 1)
        self.assertEqual(sheet.rows[0].data, full.rows[0].data)

    def test_overrides_constants_ignored_match(self):
        # "Total" never auto-maps to price, so the override is load-bearing.
        content = (
            b"Supplier,Model,Total,Quantity\n"
            b"Acme,P1,9.95,3\n"
            b"Acme,P2,19.95,7\n"
        )
        kw = {
            "column_overrides": {"price": 2},
            "column_constants": {"currency": "USD"},
            "ignored_columns": [],
        }
        sheet, _total = _sample(content, budget=5, **kw)
        full = _full(content, **kw)
        self.assertEqual(sheet.rows[0].data, full.rows[0].data)
        self.assertEqual(sheet.rows[0].data["currency"], "USD")

    def test_sheet_mismatch_refused(self):
        from pipeline.parsing import parse_delimited_sample

        with self.assertRaises(UnsupportedFile):
            parse_delimited_sample(
                b"supplier,model,price\na,b,1\n",
                "sheet.csv",
                sheet_name="other.csv",
                data_row_budget=5,
                **ARGS,
            )

    def test_bad_row_numbers_rejected(self):
        from pipeline.parsing import _table_to_sheet

        with self.assertRaises(ValueError):
            _table_to_sheet(
                [[("supplier", False)]],
                "t",
                row_numbers=[1, 2],
                **ARGS,
            )

    def test_sample_needs_no_full_materialization(self):
        # 300 data rows with 20 KB cells: the sample keeps only its head
        # rows plus a bounded decode, far below the full content size.
        lines = ["supplier,model,price"]
        for number in range(300):
            lines.append(f"Acme,P{number},{'y' * 20000}")
        content = "\n".join(lines).encode("utf-8")
        self.assertGreater(len(content), 5_000_000)
        tracemalloc.start()
        try:
            sheet, total = _sample(content, budget=5)
        finally:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        self.assertEqual(total, 300)
        # The head holds the scan window plus the budget; every head
        # row matches the full parse prefix exactly.
        full = _full(content)
        self.assertEqual(len(sheet.rows), len(full.rows[: len(sheet.rows)]))
        for head_row, full_row in zip(sheet.rows, full.rows):
            self.assertEqual(head_row.index, full_row.index)
            self.assertEqual(head_row.data, full_row.data)
        # One bounded decode plus head rows: far below the ~3x the
        # full materialization (text + split + rows + sheet) needs.
        self.assertLess(peak, len(content) * 1.5)


class DiscoverLineCountTests(TestCase):
    def test_counts_match_splitlines(self):
        from pipeline.parsing import discover_sheets

        cases = [
            "",
            "a",
            "a\n",
            "a\nb",
            "a\nb\n",
            "a\r\nb\r\n",
            "a\rb",
            "a b",
            "\n\n\n",
            "a\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                entries = discover_sheets(
                    text.encode("utf-8"), "sheet.csv"
                )
                self.assertEqual(entries[0]["rows"], len(text.splitlines()))


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PreviewTotalRowsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.user = User.objects.create_user(
            username="previewer", email="p@example.test", password="x"
        )
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.user, organization=self.org, role=OrganizationRole.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _upload(self, content: bytes, name="small.csv"):
        return self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org.id,
                "file": SimpleUploadedFile(
                    name, content, content_type="text/csv"
                ),
            },
            format="multipart",
        )

    def test_create_persists_total_rows(self):
        import json

        response = self._upload(_csv(7))
        self.assertEqual(response.status_code, 201, response.content)
        key = response.json()["key"]
        meta_path = os.path.join(self.tmp.name, "previews", f"{key}.json")
        with open(meta_path) as handle:
            meta = json.load(handle)
        self.assertEqual(meta["total_rows"], 7)
        self.assertEqual(response.json()["total_rows"], 7)

    def test_resume_uses_stored_total_without_rederive(self):
        response = self._upload(_csv(7))
        key = response.json()["key"]
        with mock.patch(
            "pipeline.previews._derive_sheet",
            side_effect=AssertionError("must not re-derive"),
        ):
            resumed = self.client.get(f"/api/pipeline/previews/{key}/resume/")
        self.assertEqual(resumed.status_code, 200, resumed.content)
        self.assertEqual(resumed.json()["total_rows"], 7)

    def test_resume_backfills_missing_total(self):
        import json

        response = self._upload(_csv(7))
        key = response.json()["key"]
        meta_path = os.path.join(self.tmp.name, "previews", f"{key}.json")
        with open(meta_path) as handle:
            meta = json.load(handle)
        del meta["total_rows"]
        with open(meta_path, "w") as handle:
            json.dump(meta, handle)
        resumed = self.client.get(f"/api/pipeline/previews/{key}/resume/")
        self.assertEqual(resumed.status_code, 200, resumed.content)
        self.assertEqual(resumed.json()["total_rows"], 7)

    def test_render_samples_match_full_parse_head(self):
        from pipeline.parsing import parse_upload

        content = _csv(12)
        response = self._upload(content)
        key = response.json()["key"]
        rendered = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {"supplier": 0, "model": 1, "price": 2}},
            format="json",
        )
        self.assertEqual(rendered.status_code, 200, rendered.content)
        full = parse_upload(content, "small.csv", **ARGS)
        rows = rendered.json()["samples"]["rows"]
        self.assertEqual(len(rows), 5)
        for sample, full_row in zip(rows, full.rows):
            self.assertEqual(sample["index"], full_row.index)
            self.assertEqual(sample["cells"]["supplier"], "Acme")

    def test_render_skips_excluded_head_rows(self):
        from pipeline.previews import _render_samples

        content = _csv(12)
        samples = _render_samples(
            content,
            "small.csv",
            {"supplier": 0, "model": 1, "price": 2},
            {},
            [],
            "USD",
            message_fallback=False,
            sheet_name="small.csv",
            exclude_lines=[2, 3, 4],
        )
        rows = samples["rows"]
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["index"], 5)

    def test_supplier_hint_reads_first_row(self):
        from pipeline.previews import _supplier_hint

        content = _csv(4)
        self.assertEqual(
            _supplier_hint(content, "small.csv", {"supplier": 0}, {}), "Acme"
        )
        # Detectable headers need no override; unmappable ones yield "".
        self.assertEqual(_supplier_hint(content, "small.csv", {}, {}), "Acme")
        no_supplier = b"Model,Price,Quantity\nP1,9.95,3\n"
        self.assertEqual(
            _supplier_hint(no_supplier, "small.csv", {}, {}), ""
        )

    def test_from_text_over_limit_refused(self):
        with override_settings(OFFERFLOW_UPLOAD_MAX_BYTES=100):
            response = self.client.post(
                "/api/pipeline/previews/from-text/",
                {"organization_id": self.org.id, "text": "x" * 101},
                format="json",
            )
        self.assertEqual(response.status_code, 413, response.content)
