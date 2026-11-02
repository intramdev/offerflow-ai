"""Tests for spreadsheet intake guards and header detection."""

import io
import os
import zipfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase
from docx import Document
from openpyxl import Workbook

from pipeline.parsing import (
    HeaderNotFound,
    MappingRequired,
    UnsupportedFile,
    candidate_message_text,
    check_archive_safety,
    discover_sheets,
    parse_upload,
    parse_xlsx,
)

HEADERS = [
    "Supplier",
    "Brand",
    "Model",
    "Capacity",
    "Color",
    "Grade",
    "Lock",
    "Qty",
    "Price",
    "Currency",
    "Notes",
]


def make_workbook(rows, sheet_title="Sheet1", visible=True):
    """Build deterministic in-memory workbook bytes for tests."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    if not visible:
        sheet.sheet_state = "hidden"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def parse(content, **kwargs):
    options = {"max_rows": 2000, "max_cols": 100, "header_scan_rows": 20}
    options.update(kwargs)
    return parse_xlsx(content, **options)


def make_multi_workbook(named_sheets):
    """Build workbook bytes from [(title, rows, visible), ...]."""
    workbook = Workbook()
    first = True
    for title, rows, visible in named_sheets:
        sheet = workbook.active if first else workbook.create_sheet(title=title)
        first = False
        sheet.title = title
        if not visible:
            sheet.sheet_state = "hidden"
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def parse_named(content, name, **kwargs):
    options = {"max_rows": 2000, "max_cols": 100, "header_scan_rows": 20}
    options.update(kwargs)
    return parse_upload(content, name, **options)


def make_docx(tables):
    """Build deterministic in-memory .docx bytes holding the given tables."""
    document = Document()
    for rows in tables:
        table = document.add_table(rows=len(rows), cols=len(rows[0]))
        for row_cells, values in zip(table.rows, rows):
            for cell, value in zip(row_cells.cells, values):
                cell.text = "" if value is None else str(value)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class ParsingTests(SimpleTestCase):
    def test_happy_path_first_row(self):
        content = make_workbook(
            [HEADERS, ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", "ok"]]
        )
        sheet = parse(content)
        self.assertEqual(sheet.header_row, 1)
        self.assertEqual(len(sheet.rows), 1)
        row = sheet.rows[0]
        self.assertEqual(row.data["supplier"], "Acme")
        self.assertEqual(row.data["capacity"], "128GB")
        self.assertEqual(row.provenance["sheet"], "Sheet1")
        self.assertEqual(row.provenance["row"], 2)

    def test_headers_beyond_row_one(self):
        content = make_workbook(
            [["Price list Q3"], ["All amounts net"], HEADERS, ["Acme", "", "P1", 64, "", "B", "", 2, 10, "EUR", ""]]
        )
        sheet = parse(content)
        self.assertEqual(sheet.header_row, 3)
        self.assertEqual(len(sheet.rows), 1)
        self.assertEqual(sheet.rows[0].data["model"], "P1")

    def test_missing_required_columns(self):
        headers = [h for h in HEADERS if h != "Price"]
        content = make_workbook([headers, ["Acme", "", "P1", 64, "", "B", "", 2, "EUR", ""]])
        with self.assertRaises(UnsupportedFile):
            parse(content)

    def test_real_world_headers_mapped(self):
        headers = ["Supplier Code", "Product Name", "Storage", "Unit Price (EUR)", "QTY.", "Currency", "Notes"]
        content = make_workbook([headers, ["ACME-1", "Pear P1", "128GB", 299.99, 5, "EUR", "ok"]])
        sheet = parse(content)
        self.assertEqual(sheet.column_map["supplier"], 0)
        self.assertEqual(sheet.column_map["model"], 1)
        self.assertEqual(sheet.column_map["price"], 3)
        self.assertEqual(sheet.column_map["quantity"], 4)
        self.assertEqual(sheet.rows[0].data["supplier"], "ACME-1")
        self.assertEqual(sheet.rows[0].data["price"], 299.99)

    def test_total_price_not_mapped_as_unit_price(self):
        headers = ["Supplier", "Model", "Qty", "Total Price", "Currency"]
        content = make_workbook([headers, ["Acme", "P1", 5, 1499.95, "USD"]])
        with self.assertRaises(UnsupportedFile) as ctx:
            parse(content)
        self.assertIn("price", str(ctx.exception))

    def test_missing_columns_error_names_detected_headers(self):
        headers = ["Supplier", "Model", "Qty", "Currency"]
        content = make_workbook([headers, ["Acme", "P1", 5, "USD"]])
        with self.assertRaises(UnsupportedFile) as ctx:
            parse(content)
        message = str(ctx.exception)
        self.assertIn("price", message)
        self.assertIn("Supplier", message)

    def test_missing_columns_carry_mapping_request(self):
        headers = ["Fornitore", "Product", "Prezzo", "Qty", "Currency"]
        content = make_workbook([headers, ["Acme", "P1", 10, 2, "EUR"]])
        with self.assertRaises(UnsupportedFile) as ctx:
            parse(content)
        self.assertEqual(ctx.exception.missing, ["supplier", "price"])
        self.assertEqual(ctx.exception.headers, headers)
        self.assertEqual(ctx.exception.column_map["model"], 1)

    def test_column_overrides_fill_missing(self):
        headers = ["Fornitore", "Product", "Prezzo", "Qty", "Currency"]
        content = make_workbook([headers, ["Acme", "P1", 10, 2, "EUR"]])
        sheet = parse(content, column_overrides={"supplier": 0, "price": 2})
        self.assertEqual(sheet.rows[0].data["supplier"], "Acme")
        self.assertEqual(sheet.rows[0].data["price"], 10)

    def test_column_overrides_reject_double_booking(self):
        headers = ["Fornitore", "Product", "Prezzo", "Qty", "Currency"]
        content = make_workbook([headers, ["Acme", "P1", 10, 2, "EUR"]])
        with self.assertRaises(UnsupportedFile):
            parse(content, column_overrides={"supplier": 0, "price": 0})

    def test_no_header_found(self):
        content = make_workbook([[1, 2, 3], [4, 5, 6]])
        with self.assertRaises(UnsupportedFile):
            parse(content)

    def test_formula_flagged_not_evaluated(self):
        content = make_workbook([HEADERS, ["Acme", "", "P1", 64, "", "B", "", 2, "=1+1", "EUR", ""]])
        sheet = parse(content)
        row = sheet.rows[0]
        self.assertEqual(row.data["price"], "=1+1")
        self.assertEqual(row.provenance["formulas"], ["price"])

    def test_hidden_sheet_skipped(self):
        workbook = Workbook()
        hidden = workbook.active
        hidden.title = "Old"
        hidden.sheet_state = "hidden"
        hidden.append(["junk"])
        visible = workbook.create_sheet("Current")
        visible.append(HEADERS)
        visible.append(["Acme", "", "P1", 64, "", "B", "", 2, 10, "EUR", ""])
        buffer = io.BytesIO()
        workbook.save(buffer)
        sheet = parse(buffer.getvalue())
        self.assertEqual(sheet.sheet, "Current")
        self.assertEqual(len(sheet.rows), 1)

    def test_blank_rows_skipped(self):
        content = make_workbook([HEADERS, [None] * len(HEADERS), ["Acme", "", "P1", 64, "", "B", "", 2, 10, "EUR", ""]])
        sheet = parse(content)
        self.assertEqual(len(sheet.rows), 1)
        self.assertEqual(sheet.rows[0].index, 3)

    def test_oversized_file_refused(self):
        with self.assertRaises(UnsupportedFile):
            check_archive_safety(b"x" * 11, max_bytes=10, max_uncompressed_bytes=100)

    def test_zip_bomb_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", "A" * (70 * 1024 * 1024))
        with self.assertRaises(UnsupportedFile):
            check_archive_safety(
                buffer.getvalue(), max_bytes=10 * 1024 * 1024, max_uncompressed_bytes=64 * 1024 * 1024
            )

    def test_macro_workbook_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/vbaProject.bin", b"macros")
            archive.writestr("[Content_Types].xml", b"x")
        with self.assertRaises(UnsupportedFile):
            check_archive_safety(
                buffer.getvalue(), max_bytes=1024, max_uncompressed_bytes=1024
            )

    def test_corrupt_file_rejected(self):
        with self.assertRaises(UnsupportedFile):
            check_archive_safety(b"not a zip", max_bytes=1024, max_uncompressed_bytes=1024)
        with self.assertRaises(UnsupportedFile):
            parse(b"not a zip")

    def test_xls_workbook_parses(self):
        path = Path(__file__).parent / "fixtures" / "sup.xls"
        sheet = parse_named(path.read_bytes(), "sup.xls")
        self.assertEqual(sheet.headers[:3], ["Supplier", "Brand", "Model"])
        self.assertEqual(sheet.rows[0].data["supplier"], "Acme")
        self.assertEqual(sheet.rows[0].data["model"], "P1")
        self.assertEqual(sheet.rows[0].data["price"], 299.99)

    def test_csv_parses_with_sniffed_delimiter(self):
        content = b"Supplier,Model,Price,Qty,Currency\nAcme,P1,10.5,2,EUR\n"
        sheet = parse_named(content, "prices.csv")
        row = sheet.rows[0].data
        self.assertEqual(row["supplier"], "Acme")
        self.assertEqual(row["price"], "10.5")

    def test_csv_semicolon_keeps_decimal_comma(self):
        content = "Supplier;Model;Price;Qty\nAcme;P1;10,5;2\n".encode("utf-8")
        sheet = parse_named(content, "prezzi.csv")
        self.assertEqual(sheet.rows[0].data["price"], "10,5")

    def test_csv_latin1_fallback(self):
        content = "Supplier,Model,Price\nAcme,P1,10\nSociété,X2,20\n".encode("cp1252")
        sheet = parse_named(content, "prices.csv")
        self.assertEqual(sheet.rows[1].data["supplier"], "Société")

    def test_tsv_forces_tab(self):
        content = b"Supplier\tModel\tUnit Price\tQty\nAcme\tP1\t10,5\t2\n"
        sheet = parse_named(content, "prices.tsv")
        self.assertEqual(sheet.rows[0].data["price"], "10,5")

    def test_txt_sniffs_delimiter(self):
        content = b"Supplier;Model;Price;Qty\nAcme;P1;10;2\n"
        sheet = parse_named(content, "prices.txt")
        self.assertEqual(sheet.rows[0].data["model"], "P1")

    def test_message_fallback_reads_one_line_per_row(self):
        content = b"iPhone 15 128GB Black 450\n\nSamsung S24 128GB 390\n"
        sheet = parse_named(content, "paste.txt", message_fallback=True)
        self.assertEqual(sheet.headers, ["message"])
        self.assertEqual(sheet.column_map, {"message": 0})
        self.assertEqual(len(sheet.rows), 2)
        self.assertEqual(
            sheet.rows[0].data["message"], "iPhone 15 128GB Black 450"
        )
        # Absolute paste line numbers survive as provenance.
        self.assertEqual(sheet.rows[0].provenance["row"], 1)
        self.assertEqual(sheet.rows[1].provenance["row"], 3)

    def test_message_fallback_rejects_multicolumn_text(self):
        content = b"a,b\n1,2\n"
        with self.assertRaises(UnsupportedFile):
            parse_named(content, "paste.txt", message_fallback=True)

    def test_message_fallback_off_keeps_hard_error(self):
        content = b"iPhone 15 128GB Black 450\n"
        with self.assertRaises(UnsupportedFile):
            parse_named(content, "paste.txt")

    def test_docx_first_table_parses(self):
        tables = [[["Supplier", "Model", "Price", "Qty"], ["Acme", "P1", "10", "2"]]]
        sheet = parse_named(make_docx(tables), "prices.docx")
        self.assertEqual(sheet.sheet, "table1")
        self.assertEqual(sheet.rows[0].data["supplier"], "Acme")

    def test_docx_skips_headerless_tables(self):
        poor = [["Price", "Qty"], ["10", "2"]]
        rich = [["Supplier", "Model", "Price", "Qty"], ["Acme", "P1", "10", "2"]]
        sheet = parse_named(make_docx([poor, rich]), "prices.docx")
        self.assertEqual(sheet.sheet, "table2")

    def test_docx_reports_richest_partial_table(self):
        poor = [["Supplier", "Model", "Qty"], ["Acme", "P1", "2"]]
        richer = [["Supplier", "Model", "Qty", "Currency"], ["Acme", "P1", "2", "EUR"]]
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(make_docx([poor, richer]), "prices.docx")
        self.assertEqual(ctx.exception.headers, richer[0])

    def test_doc_refused_with_guidance(self):
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(b"junk", "prices.doc")
        self.assertIn("docx", str(ctx.exception))

    def test_unknown_extension_refused(self):
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(b"junk", "prices.pdf")
        self.assertIn("Accepted formats", str(ctx.exception))

    def test_xlsx_still_dispatched(self):
        content = make_workbook(
            [HEADERS, ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", ""]]
        )
        sheet = parse_named(content, "sup.xlsx")
        self.assertEqual(sheet.rows[0].data["model"], "P1")

    def test_default_constant_yields_to_file_column(self):
        content = make_workbook(
            [HEADERS, ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", ""]]
        )
        sheet = parse_named(
            content,
            "sup.xlsx",
            column_constants={"currency": "EUR"},
            constant_sources={"currency": "default"},
        )
        self.assertEqual(sheet.rows[0].data["currency"], "USD")

    def test_manual_constant_still_conflicts_with_file_column(self):
        content = make_workbook(
            [HEADERS, ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", ""]]
        )
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(
                content,
                "sup.xlsx",
                column_constants={"currency": "EUR"},
                constant_sources={"currency": "manual"},
            )
        self.assertIn("both a column and a set value", str(ctx.exception))


PHONE_ROW = ["PhoneCo", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", ""]
TABLET_ROW = ["TabCo", "Pear", "T1", "64GB", "White", "B", "Unlocked", 2, 199.0, "USD", ""]


def two_sheet_workbook():
    return make_multi_workbook(
        [("Phones", [HEADERS, PHONE_ROW], True), ("Tablets", [HEADERS, TABLET_ROW], True)]
    )


FIXTURE_PROVIDER_ENV = {
    "OFFERFLOW_PROVIDER": "fixture",
    "OFFERFLOW_ALLOW_PAID": "false",
    "OFFERFLOW_META_API_KEY": "",
}


def force_fixture_provider(testcase):
    """Pin flow tests to the deterministic fixture provider.

    Provider selection reads os.environ at call time, so an ambient paid
    configuration would otherwise dispatch live model calls (and spend)
    from tests whose expectations assume the fixture.
    """
    guard = mock.patch.dict(os.environ, FIXTURE_PROVIDER_ENV, clear=False)
    guard.start()
    testcase.addCleanup(guard.stop)


class SheetDiscoveryTests(SimpleTestCase):
    def test_discover_lists_worksheets_with_visibility(self):
        content = make_multi_workbook(
            [
                ("Secret", [HEADERS, PHONE_ROW], False),
                ("Phones", [HEADERS, PHONE_ROW], True),
                ("Tablets", [HEADERS, TABLET_ROW], True),
            ]
        )
        entries = discover_sheets(content, "sup.xlsx")
        self.assertEqual([e["name"] for e in entries], ["Secret", "Phones", "Tablets"])
        self.assertEqual([e["kind"] for e in entries], ["worksheet"] * 3)
        self.assertEqual(
            [e["visible"] for e in entries], [False, True, True]
        )
        self.assertTrue(all(e["rows"] >= 2 and e["cols"] >= 10 for e in entries))

    def test_default_selects_first_visible_sheet(self):
        content = make_multi_workbook(
            [("Secret", [HEADERS, PHONE_ROW], False), ("Phones", [HEADERS, PHONE_ROW], True)]
        )
        sheet = parse_named(content, "sup.xlsx")
        self.assertEqual(sheet.sheet, "Phones")
        self.assertEqual(sheet.rows[0].data["supplier"], "PhoneCo")

    def test_explicit_selection_parses_named_sheet(self):
        sheet = parse_named(two_sheet_workbook(), "sup.xlsx", sheet_name="Tablets")
        self.assertEqual(sheet.sheet, "Tablets")
        self.assertEqual(sheet.rows[0].data["supplier"], "TabCo")
        self.assertEqual(sheet.rows[0].data["model"], "T1")

    def test_unknown_sheet_names_available(self):
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(two_sheet_workbook(), "sup.xlsx", sheet_name="Nope")
        self.assertIn("Available: Phones, Tablets", str(ctx.exception))

    def test_hidden_sheet_refused(self):
        content = make_multi_workbook(
            [("Secret", [HEADERS, PHONE_ROW], False), ("Phones", [HEADERS, PHONE_ROW], True)]
        )
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(content, "sup.xlsx", sheet_name="Secret")
        self.assertIn("is hidden", str(ctx.exception))

    def test_headerless_table_raises_header_not_found(self):
        content = make_workbook([["hello"], ["world"]])
        with self.assertRaises(HeaderNotFound) as ctx:
            parse_named(content, "sup.xlsx")
        self.assertIn("No header row", str(ctx.exception))
        self.assertIsInstance(ctx.exception, UnsupportedFile)

    def test_mapping_required_carries_sheet(self):
        content = make_multi_workbook(
            [("Phones", [["Supplier", "Model", "Qty"], ["Acme", "P1", 2]], True)]
        )
        with self.assertRaises(MappingRequired) as ctx:
            parse_named(content, "sup.xlsx")
        self.assertEqual(ctx.exception.sheet, "Phones")

    def test_discover_docx_tables_and_selection(self):
        content = make_docx([[HEADERS, PHONE_ROW], [HEADERS, TABLET_ROW]])
        entries = discover_sheets(content, "sup.docx")
        self.assertEqual([e["name"] for e in entries], ["table1", "table2"])
        self.assertEqual([e["kind"] for e in entries], ["table", "table"])
        sheet = parse_named(content, "sup.docx", sheet_name="table2")
        self.assertEqual(sheet.sheet, "table2")
        self.assertEqual(sheet.rows[0].data["supplier"], "TabCo")
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(content, "sup.docx", sheet_name="table9")
        self.assertIn("Available: table1, table2", str(ctx.exception))

    def test_discover_text_is_a_single_section(self):
        entries = discover_sheets(b"a,b\n1,2\n", "chat.csv")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "text")
        self.assertEqual(entries[0]["name"], "chat.csv")
        self.assertEqual(entries[0]["rows"], 2)

    def test_delimited_section_mismatch_refused(self):
        with self.assertRaises(UnsupportedFile) as ctx:
            parse_named(b"a,b\n1,2\n", "chat.csv", sheet_name="other")
        self.assertIn("Available: chat.csv", str(ctx.exception))

    def test_candidate_message_text_cap(self):
        text, truncated, total = candidate_message_text(b"hello world")
        self.assertEqual((text, truncated, total), ("hello world", False, 11))
        big = b"x" * 300_000
        text, truncated, total = candidate_message_text(big)
        self.assertTrue(truncated)
        self.assertEqual(total, 300_000)
        self.assertEqual(len(text), 256_000)
