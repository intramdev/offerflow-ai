"""Multi-format intake: hostile-file guards, header detection, raw rows.

Accepted formats are xlsx, xls, csv, txt, tsv, and docx. Every reader
reduces its input to plain value rows and shares one header-scan core, so
column mapping behaves identically whatever the container. The importer
never executes macros, never evaluates formulas, never follows links or
external references. Formula cells are preserved as evidence text and flagged
so downstream stages treat them as unsupported input, not values.

Header matching tolerates vendor phrasing ("Unit Price (EUR)", "Supplier
code", "QTY.") but never maps a column whose meaning differs: a total,
tax, or retail price is not a unit price, so it stays unmapped and the
failure message names the headers that were actually seen.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal

import xlrd
from docx import Document
from openpyxl import load_workbook


class UnsupportedFile(Exception):
    """The uploaded file cannot be safely parsed."""


class HeaderNotFound(UnsupportedFile):
    """No header row in the scanned rows (message lane may still read it)."""


class MessageCandidateFile(UnsupportedFile):
    """A .txt/.csv upload the message lane can read as pasted text.

    Subclasses UnsupportedFile so plain callers still treat it as a
    refusal; the preview endpoint translates it into an explicit
    "Import as message" offer instead of a bare header failure.
    """

    def __init__(self, text: str, *, truncated: bool, total_chars: int) -> None:
        self.text = text
        self.truncated = truncated
        self.total_chars = total_chars
        super().__init__(
            "No table headers found. This looks like pasted chat text — "
            "import it as a message instead."
        )


class MappingRequired(UnsupportedFile):
    """Headers were found but required columns are missing.

    Carries everything the reviewer needs to finish the mapping, so the
    run can ask instead of failing. Subclasses UnsupportedFile so plain
    callers still treat it as a refusal.
    """

    def __init__(
        self, *, headers: list, column_map: dict, sources: dict, missing: list, rows: list,
        sheet: str = "",
    ) -> None:
        self.headers = list(headers)
        self.column_map = dict(column_map)
        self.sources = dict(sources)
        self.missing = list(missing)
        self.rows = list(rows)
        self.sheet = sheet
        seen = ", ".join(h for h in headers if h) or "(none)"
        super().__init__(
            f"Missing required columns: {', '.join(missing)}. "
            f"Detected headers: {seen}."
        )


HEADER_TOKENS = {
    "supplier": {
        "supplier", "suppliers", "vendor", "vendors", "supplier name",
        "vendor name", "seller", "provider", "distributor",
        "supplier code", "supplier id", "vendor code", "vendor id",
    },
    "brand": {"brand", "brands", "manufacturer", "make"},
    "model": {
        "model", "models", "model name", "model number", "model no",
        "product", "products", "product name", "product code",
        "item", "items", "item name", "item code", "description", "sku",
    },
    "capacity": {"capacity", "storage", "size", "gb", "memory"},
    "color": {"color", "colors", "colour", "colours"},
    "grade": {"grade", "grades", "condition", "quality"},
    "lock": {"lock", "locked", "lock state", "lock status", "icloud", "carrier", "sim lock"},
    "quantity": {"qty", "quantity", "quantities", "stock", "units", "pcs"},
    "price": {
        "price", "prices", "unit price", "unitprice", "amount", "cost",
        "costs", "rate", "rates", "unit cost", "net price",
        "sell price", "selling price",
    },
    "currency": {"currency", "cur", "ccy", "curr", "currency code"},
    "conditions": {"conditions", "condition notes", "notes", "terms", "remarks", "comments"},
}

# Words that change a column's meaning. A header containing one of these
# must not fuzzy-match the canonical, or money and storage would be read
# from the wrong column. Exact token matches always win over these guards
# because they run in a separate earlier pass.
PRICE_DISQUALIFIERS = frozenset({
    "total", "subtotal", "sum", "average", "avg", "tax", "vat",
    "discount", "deposit", "fee", "fees", "shipping", "retail",
    "msrp", "rrp", "list", "suggested", "was", "old",
    "exchange", "fx", "conversion",
})
CAPACITY_DISQUALIFIERS = frozenset({"screen", "display", "inch"})
SUPPLIER_DISQUALIFIERS = frozenset({"network"})

REQUIRED_COLUMNS = ("supplier", "model", "price")

#: Synthetic single column for pasted chat-style text: each non-empty
#: line becomes one row whose raw line the model (or the reviewer) reads.
MESSAGE_COLUMN = "message"

ACCEPTED_EXTENSIONS = frozenset({".xlsx", ".xls", ".csv", ".txt", ".tsv", ".docx"})
ARCHIVE_EXTENSIONS = frozenset({".xlsx", ".docx"})

#: Discovery cap: a hostile workbook could list thousands of empty sheets.
MAX_DISCOVERED_SHEETS = 64
LEGACY_DOC_MESSAGE = (
    "Legacy .doc files cannot be read reliably. "
    "Save the document as .docx and upload it again."
)


@dataclass
class ParsedRow:
    index: int
    data: dict
    provenance: dict


@dataclass
class ParsedSheet:
    sheet: str
    header_row: int
    headers: list
    column_map: dict
    rows: list
    sources: dict = field(default_factory=dict)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")


def column_letter(position: int) -> str:
    """Return the spreadsheet column label (0 -> A) for mapping choices."""
    label = ""
    position += 1
    while position:
        position, remainder = divmod(position - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _normalize_header(value) -> tuple:
    """Return (scan_text, match_text) for a raw header cell.

    scan_text keeps parenthetical qualifiers so disqualifier words inside
    them still veto a match; match_text drops them for token matching.
    """
    if value is None:
        return "", ""
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip(" :;.,")
    scan = re.sub(r"\s+", " ", text)
    match = re.sub(r"\s+", " ", _PAREN_RE.sub("", text)).strip(" :;.,")
    return scan, match


def header_signature(headers: list) -> str:
    """Fingerprint ordered normalized headers for mapping memory.

    Order matters: the same headers in another order need another mapping.
    """
    parts = [f"{index}:{_normalize_header(h)[1]}" for index, h in enumerate(headers)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _extension_of(filename: str) -> str:
    chunks = (filename or "").lower().rsplit(".", 1)
    return f".{chunks[-1]}" if len(chunks) > 1 else ""


def format_megabytes(num_bytes: int) -> str:
    """Decimal megabytes for user-facing size messages ("200 MB")."""
    whole, remainder = divmod(int(num_bytes), 1_000_000)
    if remainder == 0:
        return f"{whole} MB"
    return f"{num_bytes / 1_000_000:.1f} MB"


class UploadTooLarge(UnsupportedFile):
    """An upload whose byte size exceeds the intake limit.

    Subclasses UnsupportedFile so existing guards keep catching it;
    views map it to 413 instead of 400.
    """


def check_upload_extension(filename: str) -> str:
    """Refuse unsupported extensions without touching file bytes."""
    extension = _extension_of(filename)
    if extension == ".doc":
        raise UnsupportedFile(LEGACY_DOC_MESSAGE)
    if extension not in ACCEPTED_EXTENSIONS:
        accepted = ", ".join(sorted(e[1:] for e in ACCEPTED_EXTENSIONS))
        raise UnsupportedFile(
            f"Unsupported file type {extension or '(none)'}. "
            f"Accepted formats: {accepted}."
        )
    return extension


def refuse_oversize(num_bytes: int, *, max_bytes: int) -> None:
    """Refuse an oversized upload from its declared size, no read needed."""
    if num_bytes > max_bytes:
        raise UploadTooLarge(
            f"File is {format_megabytes(num_bytes)}; uploads must be "
            f"{format_megabytes(max_bytes)} or smaller."
        )


def check_plain_size(content: bytes, *, max_bytes: int) -> None:
    """Refuse an oversized non-archive upload."""
    refuse_oversize(len(content), max_bytes=max_bytes)


def check_archive_safety(
    content: bytes, *, max_bytes: int, max_uncompressed_bytes: int
) -> None:
    """Refuse oversized, corrupt, macro-enabled, or zip-bombed uploads."""
    refuse_oversize(len(content), max_bytes=max_bytes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise UnsupportedFile("File is not a readable spreadsheet.") from None
    names = archive.namelist()
    if "xl/vbaProject.bin" in names or "word/vbaProject.bin" in names:
        raise UnsupportedFile("Macro-enabled documents are not accepted.")
    total = sum(info.file_size for info in archive.infolist())
    if total > max_uncompressed_bytes:
        raise UnsupportedFile("Archive expands beyond the intake limit.")


def check_upload_safety(
    content: bytes, filename: str, *, max_bytes: int, max_uncompressed_bytes: int
) -> str:
    """Dispatch hostile-file guards by extension. Returns the extension."""
    extension = check_upload_extension(filename)
    if extension in ARCHIVE_EXTENSIONS:
        check_archive_safety(
            content,
            max_bytes=max_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    else:
        check_plain_size(content, max_bytes=max_bytes)
    return extension


def _match_columns(values: list) -> tuple:
    """Map canonical names to positions plus how each one matched.

    Returns (matched, sources): exact matches first, then whole-word
    matches for still-missing canonicals, so a precise header always wins
    over a wordy neighbor, and each position maps to at most one canonical.
    Sources record "exact" or "word" per canonical for preview confidence.
    """
    scans: list = []
    matches: list = []
    for value in values:
        scan, match = _normalize_header(value)
        scans.append(scan)
        matches.append(match)
    matched: dict = {}
    sources: dict = {}
    for position, text in enumerate(matches):
        if not text:
            continue
        for canonical, tokens in HEADER_TOKENS.items():
            if canonical not in matched and text in tokens:
                matched[canonical] = position
                sources[canonical] = "exact"
    used = set(matched.values())
    for position, text in enumerate(matches):
        if not text or position in used:
            continue
        words = set(text.split()) | set(scans[position].split())
        for canonical, tokens in HEADER_TOKENS.items():
            if canonical in matched:
                continue
            if canonical == "price" and words & PRICE_DISQUALIFIERS:
                continue
            if canonical == "capacity" and words & CAPACITY_DISQUALIFIERS:
                continue
            if canonical == "supplier" and words & SUPPLIER_DISQUALIFIERS:
                continue
            for token in tokens:
                if token in words or (" " in token and token in text):
                    matched[canonical] = position
                    sources[canonical] = "word"
                    used.add(position)
                    break
            if position in used:
                break
    return matched, sources


def _table_to_sheet(
    rows: list,
    title: str,
    *,
    max_rows: int,
    max_cols: int,
    header_scan_rows: int,
    column_overrides: dict | None = None,
    column_constants: dict | None = None,
    ignored_columns: list | None = None,
    constant_sources: dict | None = None,
    message_fallback: bool = False,
    raw_lines: list | None = None,
) -> ParsedSheet:
    """Run header scan plus row extraction over plain value rows.

    Rows are lists of (value, is_formula) pairs so every container shares
    one mapping core. Overrides map canonical names to header positions
    and win over automatic detection. Constants stamp one raw value onto
    every row (recorded as a manual source); ignored names are dropped
    from the detected map.

    With message_fallback (pasted chat-style text only), a headerless
    table becomes message rows — one per source line, with the exact
    original line text — instead of failing, so the interpretation
    lane can read each line. raw_lines carries those exact lines
    (blank lines stay in place so row numbers match the source);
    separator-split cells are discarded because the message lane
    re-reads the raw lines, never the guessed columns.
    """
    header_index = None
    headers: list = []
    column_map: dict = {}
    sources: dict = {}
    for row_number, row in enumerate(rows, start=1):
        if row_number > header_scan_rows:
            break
        values = [value for value, _flag in row]
        if len(row) > max_cols:
            raise UnsupportedFile("Table exceeds the column intake limit.")
        matched, found_sources = _match_columns(values)
        criticals = set(matched) & set(REQUIRED_COLUMNS)
        if len(matched) >= 3 and criticals:
            header_index = row_number
            headers = [("" if value is None else str(value).strip()) for value in values]
            column_map = matched
            sources = found_sources
            break
    if header_index is None:
        if not message_fallback:
            raise HeaderNotFound("No header row found in the scanned rows.")
        if raw_lines is None:
            content_rows = [
                row
                for row in rows
                if any(
                    value is not None and str(value).strip()
                    for value, _flag in row
                )
            ]
            if not content_rows:
                raise UnsupportedFile("No readable rows in the pasted text.")
            if any(len(row) != 1 for row in content_rows):
                raise UnsupportedFile(
                    "No header row found. Add a header row such as "
                    "Model, Price, Quantity — or paste one product per line "
                    "so each line can be read as a message."
                )
        else:
            if not any(
                isinstance(line, str) and line.strip() for line in raw_lines
            ):
                raise UnsupportedFile("No readable rows in the pasted text.")
            # Rebuild from the exact source lines: whatever the
            # delimiter sniff split stays out of the message lane.
            rows = [[(line, False)] for line in raw_lines]
        headers = [MESSAGE_COLUMN]
        header_index = 0
        column_map = {MESSAGE_COLUMN: 0}
        sources = {MESSAGE_COLUMN: "pasted"}
    if column_overrides:
        for canonical, position in column_overrides.items():
            if canonical not in HEADER_TOKENS:
                raise UnsupportedFile(f"Unknown column {canonical!r}.")
            if (
                isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
                or position >= len(headers)
            ):
                raise UnsupportedFile(
                    f"Column {canonical!r} points outside the headers."
                )
            column_map[canonical] = position
            sources[canonical] = "confirmed"
        if len(set(column_map.values())) != len(column_map):
            raise UnsupportedFile("The column mapping assigns one column twice.")
    ignored = list(ignored_columns or [])
    for canonical in ignored:
        if canonical not in HEADER_TOKENS:
            raise UnsupportedFile(f"Unknown column {canonical!r}.")
    constants = dict(column_constants or {})
    labels = constant_sources or {}
    for canonical, value in list(constants.items()):
        if canonical not in HEADER_TOKENS:
            raise UnsupportedFile(f"Unknown column {canonical!r}.")
        if canonical in column_map and canonical not in ignored:
            if labels.get(canonical) == "default":
                # The organization default only fills unspecified
                # fields; a real file column wins over it.
                del constants[canonical]
                continue
            raise UnsupportedFile(
                f"Column {canonical!r} has both a column and a set value."
            )
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise UnsupportedFile(
                f"Value for {canonical!r} must be text or a number."
            )
        if isinstance(value, str) and not value.strip():
            raise UnsupportedFile(f"Value for {canonical!r} must not be blank.")
    for canonical in ignored:
        column_map.pop(canonical, None)
        sources.pop(canonical, None)
    for canonical in constants:
        sources[canonical] = labels.get(canonical, "manual")

    def _extract() -> list:
        """Extract data rows with the final mapping (partial maps allowed)."""
        found: list = []
        for row_number, row in enumerate(rows, start=1):
            if row_number <= header_index:
                continue
            values = [value for value, _flag in row]
            if len(row) > max_cols:
                raise UnsupportedFile("Table exceeds the column intake limit.")
            if all(value is None or (isinstance(value, str) and not value.strip()) for value in values):
                continue
            cells: dict = {}
            formulas: list = []
            for canonical, position in column_map.items():
                value, is_formula = row[position] if position < len(row) else (None, False)
                if is_formula:
                    formulas.append(canonical)
                cells[canonical] = _json_safe(value)
            for canonical, value in constants.items():
                cells[canonical] = _json_safe(value)
            mapped_positions = set(column_map.values())
            extra: dict = {}
            for position, (value, _flag) in enumerate(row):
                if position in mapped_positions:
                    continue
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                label = headers[position] if position < len(headers) and headers[position] else f"col{position + 1}"
                extra[label] = _json_safe(value)
            found.append(
                ParsedRow(
                    index=row_number,
                    data=cells,
                    provenance={
                        "sheet": title,
                        "row": row_number,
                        "formulas": formulas,
                        "extra": extra,
                    },
                )
            )
            if len(found) > max_rows:
                raise UnsupportedFile("Table exceeds the row intake limit.")
        return found

    data = _extract()
    missing = [
        name
        for name in REQUIRED_COLUMNS
        if name not in column_map and name not in constants
    ]
    if missing and not (
        message_fallback and set(column_map) == {MESSAGE_COLUMN}
    ):
        raise MappingRequired(
            headers=headers,
            column_map=column_map,
            sources=sources,
            missing=missing,
            rows=data,
            sheet=title,
        )
    return ParsedSheet(
        sheet=title,
        header_row=header_index,
        headers=headers,
        column_map=column_map,
        rows=data,
        sources=sources,
    )


def _select_xlsx_sheet(workbook, sheet_name: str | None):
    """Pick an explicit worksheet or the first visible one."""
    names = [candidate.title for candidate in workbook.worksheets]
    if sheet_name is not None:
        for candidate in workbook.worksheets:
            if candidate.title == sheet_name:
                if candidate.sheet_state != "visible":
                    raise UnsupportedFile(
                        f"Worksheet {sheet_name!r} is hidden."
                    )
                return candidate
        seen = ", ".join(names) or "(none)"
        raise UnsupportedFile(
            f"Worksheet {sheet_name!r} not found. Available: {seen}."
        )
    for candidate in workbook.worksheets:
        if candidate.sheet_state == "visible":
            return candidate
    raise UnsupportedFile("Workbook has no visible worksheet.")


def _select_xls_sheet(book, sheet_name: str | None):
    """Pick an explicit .xls sheet or the first visible one."""
    names = [candidate.name for candidate in book.sheets()]
    if sheet_name is not None:
        for candidate in book.sheets():
            if candidate.name == sheet_name:
                if candidate.visibility != 0:
                    raise UnsupportedFile(
                        f"Worksheet {sheet_name!r} is hidden."
                    )
                return candidate
        seen = ", ".join(names) or "(none)"
        raise UnsupportedFile(
            f"Worksheet {sheet_name!r} not found. Available: {seen}."
        )
    for candidate in book.sheets():
        if candidate.visibility == 0:
            return candidate
    if not book.nsheets:
        raise UnsupportedFile("Workbook has no visible worksheet.")
    return book.sheet_by_index(0)


def discover_sheets(content: bytes, filename: str) -> list:
    """List every selectable section without parsing rows.

    Worksheets for xlsx/xls (hidden ones flagged, never defaulted),
    tables for docx, one text section for delimited files. Raises
    UnsupportedFile for unreadable bytes, unknown extensions, or
    legacy .doc — the same refusals as parse_upload.
    """
    extension = _extension_of(filename)
    if extension == ".xlsx":
        try:
            workbook = load_workbook(
                io.BytesIO(content), read_only=True, data_only=True
            )
        except Exception as exc:
            raise UnsupportedFile(
                "File is not a readable spreadsheet."
            ) from exc
        sheets = workbook.worksheets
        if len(sheets) > MAX_DISCOVERED_SHEETS:
            raise UnsupportedFile("Workbook lists too many worksheets.")
        return [
            {
                "name": candidate.title,
                "kind": "worksheet",
                "visible": candidate.sheet_state == "visible",
                "rows": candidate.max_row or 0,
                "cols": candidate.max_column or 0,
            }
            for candidate in sheets
        ]
    if extension == ".xls":
        try:
            book = xlrd.open_workbook(file_contents=content)
        except Exception as exc:
            raise UnsupportedFile(
                "File is not a readable spreadsheet."
            ) from exc
        sheets = book.sheets()
        if len(sheets) > MAX_DISCOVERED_SHEETS:
            raise UnsupportedFile("Workbook lists too many worksheets.")
        return [
            {
                "name": candidate.name,
                "kind": "worksheet",
                "visible": candidate.visibility == 0,
                "rows": candidate.nrows,
                "cols": candidate.ncols,
            }
            for candidate in sheets
        ]
    if extension == ".docx":
        try:
            document = Document(io.BytesIO(content))
        except Exception as exc:
            raise UnsupportedFile("File is not a readable document.") from exc
        if len(document.tables) > MAX_DISCOVERED_SHEETS:
            raise UnsupportedFile("Document lists too many tables.")
        return [
            {
                "name": f"table{index + 1}",
                "kind": "table",
                "visible": True,
                "rows": len(table.rows),
                "cols": max((len(row.cells) for row in table.rows), default=0),
            }
            for index, table in enumerate(document.tables)
        ]
    if extension in (".csv", ".txt", ".tsv"):
        title = (filename or "upload").rsplit("/", 1)[-1]
        try:
            lines = _decode_text(content).splitlines()
        except UnsupportedFile:
            raise
        return [
            {
                "name": title,
                "kind": "text",
                "visible": True,
                "rows": len(lines),
                "cols": None,
            }
        ]
    if extension == ".doc":
        raise UnsupportedFile(LEGACY_DOC_MESSAGE)
    accepted = ", ".join(sorted(e[1:] for e in ACCEPTED_EXTENSIONS))
    raise UnsupportedFile(f"Accepted formats: {accepted}.")


def parse_xlsx(
    content: bytes,
    *,
    max_rows: int,
    max_cols: int,
    header_scan_rows: int,
    column_overrides: dict | None = None,
    column_constants: dict | None = None,
    ignored_columns: list | None = None,
    constant_sources: dict | None = None,
    sheet_name: str | None = None,
) -> ParsedSheet:
    """Parse one worksheet into canonical raw rows (first visible by default)."""
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except Exception as exc:
        raise UnsupportedFile("File is not a readable spreadsheet.") from exc
    sheet = _select_xlsx_sheet(workbook, sheet_name)
    if sheet.max_row is not None and sheet.max_row > max_rows:
        raise UnsupportedFile("Worksheet exceeds the row intake limit.")
    if sheet.max_column is not None and sheet.max_column > max_cols:
        raise UnsupportedFile("Worksheet exceeds the column intake limit.")
    rows = [
        [(cell.value, getattr(cell, "data_type", None) == "f") for cell in row]
        for row in sheet.iter_rows()
    ]
    return _table_to_sheet(
        rows,
        sheet.title,
        max_rows=max_rows,
        max_cols=max_cols,
        header_scan_rows=header_scan_rows,
        column_overrides=column_overrides,
        column_constants=column_constants,
        ignored_columns=ignored_columns,
        constant_sources=constant_sources,
    )


def parse_xls(
    content: bytes,
    *,
    max_rows: int,
    max_cols: int,
    header_scan_rows: int,
    column_overrides: dict | None = None,
    column_constants: dict | None = None,
    ignored_columns: list | None = None,
    constant_sources: dict | None = None,
    sheet_name: str | None = None,
) -> ParsedSheet:
    """Parse a legacy .xls workbook. Cached values are read as plain data;
    nothing is ever evaluated."""
    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        raise UnsupportedFile("File is not a readable spreadsheet.") from exc
    sheet = _select_xls_sheet(book, sheet_name)
    if sheet.nrows > max_rows:
        raise UnsupportedFile("Worksheet exceeds the row intake limit.")
    if sheet.ncols > max_cols:
        raise UnsupportedFile("Worksheet exceeds the column intake limit.")
    rows = [
        [(sheet.cell_value(row, col), False) for col in range(sheet.ncols)]
        for row in range(sheet.nrows)
    ]
    return _table_to_sheet(
        rows,
        sheet.name,
        max_rows=max_rows,
        max_cols=max_cols,
        header_scan_rows=header_scan_rows,
        column_overrides=column_overrides,
        column_constants=column_constants,
        ignored_columns=ignored_columns,
        constant_sources=constant_sources,
    )


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnsupportedFile("File could not be decoded as text.")


#: Cap for the "Import as message" candidate payload; the from-text
#: endpoint enforces its own byte limits on the actual import.
MESSAGE_CANDIDATE_MAX_CHARS = 256_000


def candidate_message_text(content: bytes) -> tuple:
    """Decode upload bytes for a message-candidate offer.

    Returns (text, truncated, total_chars) with the same codecs as
    the delimited reader, so the offered text is exactly what the
    message lane would read.
    """
    text = _decode_text(content)
    total = len(text)
    if total > MESSAGE_CANDIDATE_MAX_CHARS:
        return text[:MESSAGE_CANDIDATE_MAX_CHARS], True, total
    return text, False, total


def _sniff_delimiter(sample_lines: list) -> str:
    """Pick the delimiter with a consistent nonzero count per line.

    Semicolon wins ties so European decimal commas never split a column.
    """
    candidates = []
    for delimiter in (";", "\t", "|", ","):
        counts = [line.count(delimiter) for line in sample_lines]
        if counts and all(count == counts[0] and count > 0 for count in counts):
            candidates.append(delimiter)
    if candidates:
        return candidates[0]
    return ","


def _require_text_section(sheet_name: str | None, title: str) -> None:
    """Delimited input has exactly one text section; refuse anything else."""
    if sheet_name is not None and sheet_name != title:
        raise UnsupportedFile(
            f"Section {sheet_name!r} not found. Available: {title}."
        )


def parse_delimited(
    content: bytes,
    title: str,
    *,
    max_rows: int,
    max_cols: int,
    header_scan_rows: int,
    column_overrides: dict | None = None,
    column_constants: dict | None = None,
    ignored_columns: list | None = None,
    constant_sources: dict | None = None,
    forced_delimiter: str | None = None,
    message_fallback: bool = False,
    sheet_name: str | None = None,
) -> ParsedSheet:
    """Parse csv/txt/tsv bytes with a sniffed (or forced) delimiter."""
    _require_text_section(sheet_name, title)
    text = _decode_text(content)
    sample = [line for line in text.splitlines() if line.strip()][:5]
    delimiter = forced_delimiter or _sniff_delimiter(sample)
    # Refuse an over-limit table while reading, before materializing
    # millions of rows: the header sits within the first
    # header_scan_rows non-blank rows, so anything beyond max_rows
    # data rows past that window can never import.
    early_cap = max_rows + header_scan_rows
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = []
        content_count = 0
        for raw in reader:
            rows.append([(value, False) for value in raw])
            if any(str(value).strip() for value in raw):
                content_count += 1
                if content_count > early_cap:
                    raise UnsupportedFile(
                        "Table exceeds the row intake limit."
                    )
    except csv.Error as exc:
        raise UnsupportedFile("File could not be parsed as delimited text.") from exc
    return _table_to_sheet(
        rows,
        title,
        max_rows=max_rows,
        max_cols=max_cols,
        header_scan_rows=header_scan_rows,
        column_overrides=column_overrides,
        column_constants=column_constants,
        ignored_columns=ignored_columns,
        constant_sources=constant_sources,
        message_fallback=message_fallback,
        # Split on "\n" exactly like the message lane's content
        # lines, so fallback row numbers match the source lines.
        raw_lines=text.split("\n"),
    )


def parse_docx(
    content: bytes,
    *,
    max_rows: int,
    max_cols: int,
    header_scan_rows: int,
    column_overrides: dict | None = None,
    column_constants: dict | None = None,
    ignored_columns: list | None = None,
    constant_sources: dict | None = None,
    sheet_name: str | None = None,
) -> ParsedSheet:
    """Parse one table of a .docx document.

    By default the first fully readable table wins; otherwise the table
    with the most recognized columns is reported so the reviewer can
    finish its mapping. An explicit table name (table1, table2, …)
    selects exactly that table instead.
    """
    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        raise UnsupportedFile("File is not a readable document.") from exc
    if not document.tables:
        raise UnsupportedFile("Document contains no tables.")
    names = [f"table{index + 1}" for index in range(len(document.tables))]
    # A table this tall can never import (headers live in the first
    # header_scan_rows rows, data is capped at max_rows), so refuse or
    # skip it before copying every cell.
    tall = max_rows + header_scan_rows
    if sheet_name is not None:
        if sheet_name not in names:
            seen = ", ".join(names)
            raise UnsupportedFile(
                f"Table {sheet_name!r} not found. Available: {seen}."
            )
        table = document.tables[names.index(sheet_name)]
        if len(table.rows) > tall:
            raise UnsupportedFile("Table exceeds the row intake limit.")
        rows = [
            [(cell.text.strip(), False) for cell in row.cells] for row in table.rows
        ]
        return _table_to_sheet(
            rows,
            sheet_name,
            max_rows=max_rows,
            max_cols=max_cols,
            header_scan_rows=header_scan_rows,
            column_overrides=column_overrides,
            column_constants=column_constants,
            ignored_columns=ignored_columns,
            constant_sources=constant_sources,
        )
    best: MappingRequired | None = None
    saw_tall = False
    for index, table in enumerate(document.tables):
        if len(table.rows) > tall:
            saw_tall = True
            continue
        rows = [
            [(cell.text.strip(), False) for cell in row.cells] for row in table.rows
        ]
        try:
            return _table_to_sheet(
                rows,
                f"table{index + 1}",
                max_rows=max_rows,
                max_cols=max_cols,
                header_scan_rows=header_scan_rows,
                column_overrides=column_overrides,
        column_constants=column_constants,
        ignored_columns=ignored_columns,
        constant_sources=constant_sources,
            )
        except MappingRequired as exc:
            if best is None or len(exc.column_map) > len(best.column_map):
                best = exc
        except UnsupportedFile:
            continue
    if best is not None:
        raise best
    if saw_tall:
        raise UnsupportedFile("Table exceeds the row intake limit.")
    raise UnsupportedFile("Document tables hold no recognizable headers.")


def parse_upload(
    content: bytes,
    filename: str,
    *,
    max_rows: int,
    max_cols: int,
    header_scan_rows: int,
    column_overrides: dict | None = None,
    column_constants: dict | None = None,
    ignored_columns: list | None = None,
    constant_sources: dict | None = None,
    message_fallback: bool = False,
    sheet_name: str | None = None,
) -> ParsedSheet:
    """Dispatch bytes to the reader matching the filename extension.

    Callers run check_upload_safety first; this only refuses unknown
    extensions and legacy .doc with guidance. message_fallback (pasted
    text only) lets headerless delimited text become message rows;
    spreadsheets and documents never fall back.
    sheet_name selects one worksheet/table (see discover_sheets);
    delimited input has a single text section and must match it.
    """
    extension = _extension_of(filename)
    options = {
        "max_rows": max_rows,
        "max_cols": max_cols,
        "header_scan_rows": header_scan_rows,
        "column_overrides": column_overrides,
        "column_constants": column_constants,
        "ignored_columns": ignored_columns,
        "constant_sources": constant_sources,
        "sheet_name": sheet_name,
    }
    if extension == ".xlsx":
        return parse_xlsx(content, **options)
    if extension == ".xls":
        return parse_xls(content, **options)
    if extension == ".docx":
        return parse_docx(content, **options)
    if extension == ".tsv":
        title = (filename or "upload").rsplit("/", 1)[-1]
        _require_text_section(sheet_name, title)
        return parse_delimited(content, title, forced_delimiter="\t", **options)
    if extension in (".csv", ".txt"):
        title = (filename or "upload").rsplit("/", 1)[-1]
        _require_text_section(sheet_name, title)
        return parse_delimited(
            content, title, message_fallback=message_fallback, **options
        )
    if extension == ".doc":
        raise UnsupportedFile(LEGACY_DOC_MESSAGE)
    accepted = ", ".join(sorted(e[1:] for e in ACCEPTED_EXTENSIONS))
    raise UnsupportedFile(f"Accepted formats: {accepted}.")
