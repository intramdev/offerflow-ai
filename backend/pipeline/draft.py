"""Versioned import drafts over immutable source lines.

A draft is the typed, server-persisted interpretation of one pasted
message (or message-lane file): confirmed rules, a document policy, and
the candidate records derived from them. Source lines keep their
original 1-based numbers forever — rules reference lines, never
positions in a rewritten copy.

derive_records is pure: the same lines plus the same rules always
produce the same records, so the preview, the worker import, and the
exports agree. The model may propose operations, but only validated
operations applied here change the draft, and every change bumps the
revision: stale writers get an explicit conflict, never a silent
overwrite.
"""

import re
from decimal import Decimal

from pipeline.normalize import (
    parse_capacity,
    parse_currency,
    parse_price,
    parse_quantity,
)
from pipeline.normalize import terminal_amount as _terminal_span
from pipeline.parsing import HEADER_TOKENS

DRAFT_VERSION = 1

DOCUMENT_POLICIES = ("stock", "price_only")

#: Fields a rule may set with a confirmed value (canonical cell names).
RULE_VALUE_FIELDS = frozenset(HEADER_TOKENS) - {"message"}

#: Fields a section rule may set (identity/grading of one block).
SECTION_FIELDS = frozenset(
    {"supplier", "brand", "grade", "currency", "color", "conditions"}
)

#: Line classes a classify rule may assign.
LINE_CLASSES = frozenset(
    {"header", "metadata", "product", "continuation", "footer", "note"}
)

#: Classes that never become candidate product records.
NON_PRODUCT_CLASSES = frozenset({"header", "metadata", "footer", "note"})

OP_KINDS = frozenset(
    {
        "global",
        "exclude_lines",
        "classify",
        "terminal_price",
        "section_value",
        "join_lines",
        "correct",
        "absent",
        "policy",
        "drop_rule",
        "field_split",
    }
)

MAX_OPERATIONS = 50
MAX_JOIN_LINES = 20
MAX_RULES = 200

#: Separators a field_split rule may use: the four delimited shapes
#: the intake can read. Anything else stays a single message line.
SPLIT_SEPARATORS = (";", ",", "\t", "|")

#: A split pattern names between two and eight field positions.
SPLIT_MIN_FIELDS = 2
SPLIT_MAX_FIELDS = 8

_NUMBER_TOKEN_RE = re.compile(r"\d[\d.,]*")
_RECORD_RE = re.compile(r"^L(\d+)(?:-L(\d+))?$")

#: Currency mentions inside one split cell: a 3-letter code, US$, or an
#: unambiguous symbol. More than one distinct mention refuses the cell.
_SPLIT_CURRENCY_RE = re.compile(r"US\$|[$€£]|[Kk][čČ]|\b[A-Za-z]{3}\b")

#: Trailing unit words a split quantity cell may carry ("12 pcs").
_SPLIT_QTY_UNIT_RE = re.compile(
    r"^(.*?)\s*(pc|pcs|units?|pieces?)$", re.IGNORECASE
)


class DraftError(Exception):
    """One apply batch failed validation; nothing was stored."""


class DraftConflict(Exception):
    """The writer's revision is stale; reload and review before retrying."""


def terminal_amount(text: str) -> Decimal | None:
    """Read the final number of a line as a price, or refuse.

    The span-aware terminal parse binds the token to its source span:
    a "final number" buried mid-line, glued to letters ("SKU123"), or
    glued to a storage unit ("256GB") is not one. The token must then
    parse unambiguously — "1.234" yields None instead of a guess;
    callers turn that into a visible unresolved issue. A trailing
    3-letter word such as a currency code ("417 USD") is explicit
    evidence, not guessing, and does not block the terminal read.
    """
    if not isinstance(text, str):
        return None
    found = _terminal_span(text)
    if found is None:
        return None
    return parse_price(found[0])


def _clean_value(field: str, value):
    """Validate a confirmed value; returns the stored form."""
    if field not in RULE_VALUE_FIELDS:
        raise DraftError(f"Unknown field {field!r}.")
    if isinstance(value, bool):
        raise DraftError(f"Value for {field!r} must be text or a number.")
    if field == "quantity":
        parsed = parse_quantity(value)
        if parsed is None:
            raise DraftError(f"Value for {field!r} must be a whole number.")
        return parsed
    if field == "price":
        parsed = parse_price(value)
        if parsed is None:
            raise DraftError(
                f"Value for {field!r} is not a readable price."
            )
        return str(parsed)
    if field == "capacity":
        parsed = parse_capacity(value)
        if parsed is None:
            raise DraftError(
                f"Value for {field!r} must read as gigabytes."
            )
        return parsed
    if field == "currency":
        code = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise DraftError(f"Value for {field!r} must be a 3-letter code.")
        return code
    text = str(value or "").strip()
    if not text:
        raise DraftError(f"Value for {field!r} must not be blank.")
    return text[:255]


def _line_numbers(raw, *, line_count: int, name: str) -> list:
    if not isinstance(raw, list) or not raw:
        raise DraftError(f"{name} must be a non-empty list of line numbers.")
    numbers = []
    for number in raw:
        if isinstance(number, bool) or not isinstance(number, int):
            raise DraftError(f"{name} must hold line numbers.")
        if not 1 <= number <= line_count:
            raise DraftError(f"Line {number} is outside the source.")
        numbers.append(number)
    return sorted(set(numbers))


def _check_op_fields(op: dict, allowed: set, kind: str) -> None:
    unknown = set(op) - allowed - {"op"}
    if unknown:
        raise DraftError(
            f"Unsupported field(s) for op {kind!r}: "
            f"{', '.join(sorted(str(name) for name in unknown))}."
        )


def _section_ranges(rules: list) -> list:
    """Header line -> covered line range for section rules."""
    headers = sorted(
        {rule["header_line"] for rule in rules if rule["kind"] == "section_value"}
    )
    ranges = []
    for pos, header in enumerate(headers):
        stop = headers[pos + 1] if pos + 1 < len(headers) else None
        ranges.append((header, stop))
    return ranges


def _section_of(rules: list, lineno: int):
    """Return the header line whose section covers lineno, if any."""
    for header, stop in _section_ranges(rules):
        if lineno > header and (stop is None or lineno < stop):
            return header
    return None


def _joins_of(rules: list) -> list:
    return [rule for rule in rules if rule["kind"] == "join_lines"]


def _split_with_spans(line: str, separator: str) -> list:
    """Split one line, keeping each cell's exact character span."""
    parts = []
    start = 0
    for chunk in line.split(separator):
        parts.append((chunk, start, start + len(chunk)))
        start += len(chunk) + len(separator)
    return parts


def _split_price_cell(cell: str):
    """Read (amount, currency) from one split price cell.

    Returns (amount_str, currency_or_None), or None when the cell is
    blank, ambiguous, or unreadable. A bare number takes no currency;
    one currency mention rides along; two mentions refuse the cell
    instead of guessing which one prices the product.
    """
    text = (cell or "").strip()
    if not text:
        return None
    mentions = _SPLIT_CURRENCY_RE.findall(text)
    if not mentions:
        amount = parse_price(text)
        return (str(amount), None) if amount is not None else None
    if len(mentions) > 1:
        return None
    currency = parse_currency(mentions[0])
    if currency is None:
        return None
    remainder = _SPLIT_CURRENCY_RE.sub("", text, count=1).strip()
    amount = parse_price(remainder)
    if amount is None:
        return None
    return str(amount), currency


def _split_quantity_cell(cell: str):
    """Read a stock count from one split quantity cell.

    A bare whole number, optionally trailed by a unit word
    ("12 pcs"). Anything else stays unknown for the reviewer.
    """
    text = (cell or "").strip()
    if not text:
        return None
    match = _SPLIT_QTY_UNIT_RE.match(text)
    number = match.group(1).strip() if match else text
    if not number:
        return None
    return parse_quantity(number)


def _split_field_value(field: str, cell: str):
    """Parse one split position; returns (value, currency) or None.

    The currency element is only ever set for price cells that name
    their own currency ("289 EUR"); every other field yields None.
    """
    if field == "price":
        return _split_price_cell(cell)
    if field == "quantity":
        parsed = _split_quantity_cell(cell)
        return (parsed, None) if parsed is not None else None
    if field == "capacity":
        parsed = parse_capacity((cell or "").strip())
        return (parsed, None) if parsed is not None else None
    if field == "currency":
        parsed = parse_currency((cell or "").strip())
        return (parsed, None) if parsed is not None else None
    text = (cell or "").strip()
    if not text:
        return None
    return text[:255], None


def derive_records(texts: list, rules: list) -> tuple[list, list, list]:
    """Derive candidate records, unresolved issues, and sections.

    texts is every source line in order (index + 1 is the original
    line number, blanks included). Returns (records, unresolved,
    sections); each record carries stable line-anchored ids, spans,
    field values with provenance states, and its class.
    """
    rules = list(rules or [])
    excluded: set = set()
    for rule in rules:
        if rule["kind"] == "exclude_lines":
            excluded.update(rule["lines"])
    classes: dict = {}
    for rule in rules:
        if rule["kind"] == "classify":
            for lineno in rule["lines"]:
                classes[lineno] = rule["class"]
    for header, _stop in _section_ranges(rules):
        classes.setdefault(header, "header")
    joined: dict = {}
    for rule in _joins_of(rules):
        lines = [n for n in rule["lines"] if n not in excluded]
        if len(lines) < 2:
            continue
        if any(n in joined for n in lines):
            continue
        for n in lines:
            joined[n] = lines
    corrects: dict = {}
    for rule in rules:
        if rule["kind"] == "correct":
            corrects.setdefault(rule["record"], {})[rule["field"]] = rule
    globals_: dict = {}
    for rule in rules:
        if rule["kind"] == "global":
            globals_[rule["field"]] = rule
    sectioned: dict = {}
    for rule in rules:
        if rule["kind"] == "section_value":
            sectioned.setdefault(rule["header_line"], {})[rule["field"]] = rule
    terminals = [rule for rule in rules if rule["kind"] == "terminal_price"]
    splits = [rule for rule in rules if rule["kind"] == "field_split"]
    records = []
    seen: set = set()
    for lineno, text in enumerate(texts, start=1):
        if lineno in excluded or lineno in seen:
            continue
        if not (isinstance(text, str) and text.strip()):
            continue
        if lineno in joined:
            group = joined[lineno]
            seen.update(group)
            record_id = f"L{group[0]}-L{group[-1]}"
            spans = list(group)
        else:
            record_id = f"L{lineno}"
            spans = [lineno]
        record_class = classes.get(lineno, "unknown")
        if lineno in joined:
            record_class = classes.get(lineno, "product")
            for n in spans[1:]:
                if classes.get(n, "product") not in ("product", "continuation"):
                    record_class = "unknown"
        if record_class in NON_PRODUCT_CLASSES:
            continue
        section = _section_of(rules, lineno)
        fields: dict = {}
        for field in sorted(RULE_VALUE_FIELDS):
            entry = {"value": None, "state": "unknown", "by": None, "rule": None}
            rule = globals_.get(field)
            if rule is not None:
                entry = {
                    "value": rule["value"],
                    "state": "user_confirmed",
                    "by": "global",
                    "rule": rule.get("id"),
                }
            if section is not None and field in sectioned.get(section, {}):
                rule = sectioned[section][field]
                entry = {
                    "value": rule["value"],
                    "state": "user_confirmed",
                    "by": f"section L{section}",
                    "rule": rule.get("id"),
                }
            fields[field] = entry
        # Confirmed split positions apply next: only single-line
        # records whose width matches the pattern, only compatible
        # cells, and an explicit currency position always wins over
        # a currency riding inside a price cell. Corrections apply
        # after the split, so a per-record fix can repair one
        # exceptional line without changing the pattern.
        for rule in splits:
            scoped = rule.get("lines")
            if scoped is not None and lineno not in scoped:
                continue
            if len(spans) != 1:
                continue
            parts = _split_with_spans(texts[lineno - 1], rule["separator"])
            if len(parts) != len(rule["fields"]):
                continue
            embedded_currency = None
            for position, field in enumerate(rule["fields"]):
                parsed = _split_field_value(field, parts[position][0])
                if parsed is None:
                    continue
                value, extra = parsed
                if field == "price" and extra is not None:
                    embedded_currency = (extra, parts[position][1:])
                fields[field] = {
                    "value": value,
                    "state": "derived",
                    "by": f"split L{lineno}",
                    "rule": rule.get("id"),
                    "span": list(parts[position][1:]),
                    "line": lineno,
                }
            if embedded_currency is not None and fields["currency"]["state"] == "unknown":
                extra, span = embedded_currency
                fields["currency"] = {
                    "value": extra,
                    "state": "derived",
                    "by": f"split L{lineno}",
                    "rule": rule.get("id"),
                    "span": list(span),
                    "line": lineno,
                }
        for field in sorted(RULE_VALUE_FIELDS):
            override = corrects.get(record_id, {}).get(field)
            if override is not None:
                fields[field] = {
                    "value": override["value"],
                    "state": "user_confirmed",
                    "by": "correction",
                    "rule": override.get("id"),
                }
        # Terminal derivation is part of the resolved record, not a
        # second view: the candidate table, unresolved counts, samples,
        # worker snapshot, and export validation all read these fields.
        # (Manual price corrections are rejected while a terminal rule
        # covers the record, so derivation cannot clobber a correction.)
        for rule in terminals:
            if not _terminal_covers(rule, rules, record_id):
                continue
            last = spans[-1]
            found = _terminal_span(texts[last - 1])
            if found is None:
                continue
            amount = parse_price(found[0])
            if amount is None:
                continue
            derived = {
                "state": "derived",
                "by": f"terminal L{last}",
                "rule": rule.get("id"),
                "span": [found[1], found[2]],
                "line": last,
            }
            fields["price"] = {"value": str(amount), **dict(derived)}
            fields["currency"] = {
                "value": rule["currency"], **dict(derived)
            }
        records.append(
            {
                "id": record_id,
                "lines": spans,
                "class": record_class,
                "section": section,
                "fields": fields,
            }
        )
    sections = []
    for header, stop in _section_ranges(rules):
        covered = [
            record["id"]
            for record in records
            if record["section"] == header
        ]
        sections.append({"header_line": header, "stop": stop, "records": covered})
    unresolved = _unresolved(records, rules)
    return records, unresolved, sections


def _unresolved(records: list, rules: list) -> list:
    issues = []
    known = {rule["field"] for rule in rules if rule["kind"] == "global"}
    split_fields = {
        field
        for rule in rules
        if rule["kind"] == "field_split"
        for field in rule.get("fields") or []
    }
    if (
        "supplier" not in known
        and "supplier" not in split_fields
        and not any(
            rule["kind"] == "section_value" and rule["field"] == "supplier"
            for rule in rules
        )
    ):
        issues.append(
            {
                "scope": "document",
                "field": "supplier",
                "reason": "No supplier confirmed for this document.",
            }
        )
    currency_known = (
        "currency" in known
        or "currency" in split_fields
        or any(
            rule["kind"] in ("section_value", "terminal_price")
            and rule.get("currency", rule.get("value"))
            for rule in rules
        )
        or (
            bool(records)
            and all(
                record["fields"]["currency"]["state"] != "unknown"
                for record in records
            )
        )
    )
    if not currency_known:
        issues.append(
            {
                "scope": "document",
                "field": "currency",
                "reason": "No currency confirmed for this document.",
            }
        )
    for record in records:
        fields = record["fields"]
        for field in ("model", "price", "capacity", "grade"):
            if fields[field]["state"] == "unknown":
                issues.append(
                    {
                        "scope": "record",
                        "record": record["id"],
                        "field": field,
                        "reason": f"{record['id']}: {field} is unread.",
                    }
                )
        if record["class"] == "continuation":
            issues.append(
                {
                    "scope": "record",
                    "record": record["id"],
                    "field": "class",
                    "reason": (
                        f"{record['id']}: continuation line is not "
                        "joined to a product."
                    ),
                }
            )
    return issues


def _record_ids(texts: list, rules: list) -> set:
    records, _unresolved, _sections = derive_records(texts, rules)
    return {record["id"] for record in records}


def _validate_one(op: dict, *, texts, rules, policy: str) -> dict | None:
    """Validate a single op; returns a rule to store, or None.

    Policy ops return None and are applied by the caller. Raises
    DraftError on anything unsupported or contradictory.
    """
    if not isinstance(op, dict):
        raise DraftError("Each operation must be an object.")
    kind = op.get("op")
    if kind not in OP_KINDS:
        raise DraftError(f"Unsupported operation {kind!r}.")
    line_count = len(texts)
    if kind == "global":
        _check_op_fields(op, {"field", "value"}, kind)
        field = op.get("field")
        if field not in RULE_VALUE_FIELDS:
            raise DraftError(f"Unknown field {field!r}.")
        return {"kind": "global", "field": field, "value": _clean_value(field, op.get("value"))}
    if kind == "exclude_lines":
        _check_op_fields(op, {"lines", "reason"}, kind)
        lines = _line_numbers(op.get("lines"), line_count=line_count, name="lines")
        rule: dict = {"kind": "exclude_lines", "lines": lines}
        if op.get("reason") is not None:
            rule["reason"] = str(op["reason"])[:255]
        return rule
    if kind == "classify":
        _check_op_fields(op, {"lines", "class"}, kind)
        if op.get("class") not in LINE_CLASSES:
            raise DraftError(f"Unknown class {op.get('class')!r}.")
        return {
            "kind": "classify",
            "lines": _line_numbers(op.get("lines"), line_count=line_count, name="lines"),
            "class": op["class"],
        }
    if kind == "terminal_price":
        _check_op_fields(op, {"currency", "section_header_line"}, kind)
        currency = _clean_value("currency", op.get("currency"))
        rule = {"kind": "terminal_price", "currency": currency, "scope": "document"}
        header = op.get("section_header_line")
        if header is not None:
            if isinstance(header, bool) or not isinstance(header, int):
                raise DraftError("section_header_line must be a line number.")
            if not 1 <= header <= line_count:
                raise DraftError(f"Line {header} is outside the source.")
            rule["scope"] = {"section_header_line": header}
        return rule
    if kind == "section_value":
        _check_op_fields(op, {"header_line", "field", "value", "label"}, kind)
        header = op.get("header_line")
        if isinstance(header, bool) or not isinstance(header, int):
            raise DraftError("header_line must be a line number.")
        if not 1 <= header <= line_count:
            raise DraftError(f"Line {header} is outside the source.")
        if not (isinstance(texts[header - 1], str) and texts[header - 1].strip()):
            raise DraftError(f"Line {header} is blank; pick the header line.")
        field = op.get("field")
        if field not in SECTION_FIELDS:
            raise DraftError(f"Field {field!r} cannot be section-scoped.")
        rule = {
            "kind": "section_value",
            "header_line": header,
            "header_text": texts[header - 1].strip()[:120],
            "field": field,
            "value": _clean_value(field, op.get("value")),
        }
        if op.get("label") is not None:
            rule["label"] = str(op["label"])[:255]
        return rule
    if kind == "join_lines":
        _check_op_fields(op, {"lines"}, kind)
        lines = _line_numbers(op.get("lines"), line_count=line_count, name="lines")
        if len(lines) < 2:
            raise DraftError("join_lines needs at least two lines.")
        if len(lines) > MAX_JOIN_LINES:
            raise DraftError(f"join_lines covers at most {MAX_JOIN_LINES} lines.")
        if lines != list(range(lines[0], lines[-1] + 1)):
            raise DraftError("join_lines needs contiguous lines.")
        taken: set = set()
        for rule in rules:
            if rule["kind"] == "exclude_lines":
                taken.update(rule["lines"])
            elif rule["kind"] == "join_lines":
                taken.update(rule["lines"])
        clash = [n for n in lines if n in taken]
        if clash:
            raise DraftError(
                f"Line(s) {clash} are already excluded or joined."
            )
        return {"kind": "join_lines", "lines": lines}
    if kind == "correct":
        _check_op_fields(op, {"record", "field", "value"}, kind)
        record = op.get("record")
        if not isinstance(record, str) or not _RECORD_RE.match(record):
            raise DraftError(f"Unknown record {record!r}.")
        if record not in _record_ids(texts, rules):
            raise DraftError(f"Record {record} is not a current candidate.")
        field = op.get("field")
        if field not in RULE_VALUE_FIELDS:
            raise DraftError(f"Unknown field {field!r}.")
        value = _clean_value(field, op.get("value"))
        for rule in rules:
            if (
                rule["kind"] == "terminal_price"
                and field == "price"
                and _terminal_covers(rule, rules, record)
                and str(value) != _terminal_expected(texts, record, rules)
            ):
                raise DraftError(
                    "That price contradicts the confirmed terminal-price "
                    "rule; change the rule instead of the record."
                )
        return {"kind": "correct", "record": record, "field": field, "value": value}
    if kind == "absent":
        _check_op_fields(op, {"field", "scope"}, kind)
        if op.get("field") not in ("quantity", "capacity"):
            raise DraftError("Only quantity or capacity can be declared absent.")
        if op.get("scope", "document") != "document":
            raise DraftError("absent supports document scope only.")
        if op.get("field") == "quantity" and policy != "price_only":
            raise DraftError(
                "Quantity declared absent needs the price_only policy; "
                "set the policy first."
            )
        return {"kind": "absent", "field": op["field"], "scope": "document"}
    if kind == "policy":
        _check_op_fields(op, {"policy"}, kind)
        if op.get("policy") not in DOCUMENT_POLICIES:
            raise DraftError(f"Unknown policy {op.get('policy')!r}.")
        return None
    if kind == "drop_rule":
        _check_op_fields(op, {"rule"}, kind)
        target = op.get("rule")
        if not isinstance(target, str) or not target:
            raise DraftError("drop_rule needs a rule id.")
        if not any(rule.get("id") == target for rule in rules):
            raise DraftError(f"Rule {target} is not on this draft.")
        return {"kind": "drop_rule", "rule": target}
    if kind == "field_split":
        _check_op_fields(op, {"separator", "fields", "lines"}, kind)
        separator = op.get("separator")
        if separator not in SPLIT_SEPARATORS:
            raise DraftError(
                f"Separator must be one of {', '.join(repr(s) for s in SPLIT_SEPARATORS)}."
            )
        fields = op.get("fields")
        if (
            not isinstance(fields, list)
            or not (SPLIT_MIN_FIELDS <= len(fields) <= SPLIT_MAX_FIELDS)
            or any(field not in RULE_VALUE_FIELDS for field in fields)
        ):
            raise DraftError(
                "fields must list two to eight known field names in "
                "position order."
            )
        if len(set(fields)) != len(fields):
            raise DraftError("fields must not repeat a field.")
        scoped = op.get("lines")
        if scoped is None or scoped == []:
            scoped = None
        else:
            scoped = _line_numbers(scoped, line_count=line_count, name="lines")
        return {
            "kind": "field_split",
            "separator": separator,
            "fields": list(fields),
            "lines": scoped,
        }
    raise DraftError(f"Unsupported operation {kind!r}.")


def _terminal_covers(rule: dict, rules: list, record_id: str) -> bool:
    if rule.get("scope") == "document":
        return True
    header = (rule.get("scope") or {}).get("section_header_line")
    match = _RECORD_RE.match(record_id or "")
    if header is None or match is None:
        return False
    first = int(match.group(1))
    return _section_of(rules, first) == header


def _terminal_expected(texts: list, record_id: str, rules: list):
    match = _RECORD_RE.match(record_id or "")
    if match is None:
        return None
    last = int(match.group(2) or match.group(1))
    if not 1 <= last <= len(texts):
        return None
    amount = terminal_amount(texts[last - 1])
    return None if amount is None else str(amount)


def terminal_scope_lines(texts: list, rules: list) -> set:
    """First lines of candidate records a terminal_price rule covers."""
    records, _issues, _sections = derive_records(texts, rules or [])
    terminals = [rule for rule in (rules or []) if rule["kind"] == "terminal_price"]
    covered = set()
    for record in records:
        if any(
            _terminal_covers(rule, rules or [], record["id"]) for rule in terminals
        ):
            covered.add(record["lines"][0])
    return covered


def derive_record_cells(texts: list, rules: list) -> dict:
    """Canonical source cells for every candidate record.

    A projection of derive_records, not a second derivation: the
    preview, the worker import, and the exports read the same resolved
    fields the candidate table shows. Returns
    {record_id: {"lines", "class", "section", "cells", "terminal"}},
    where "terminal" carries the bound span, currency, and line when a
    covering rule resolved, and None otherwise.
    """
    records, _issues, _sections = derive_records(texts, rules or [])
    derived = {}
    for record in records:
        cells = {}
        for field, entry in record["fields"].items():
            if entry["state"] != "unknown" and entry["value"] is not None:
                cells[field] = entry["value"]
        terminal = None
        price = record["fields"].get("price") or {}
        if price.get("state") == "derived":
            currency = record["fields"].get("currency") or {}
            terminal = {
                "span": price.get("span"),
                "currency": currency.get("value"),
                "line": price.get("line"),
            }
        derived[record["id"]] = {
            "lines": record["lines"],
            "class": record["class"],
            "section": record["section"],
            "cells": cells,
            "terminal": terminal,
        }
    return derived


def _replaces(new: dict, old: dict) -> bool:
    """Whether a new rule supersedes an old one (same kind + scope)."""
    if new["kind"] != old["kind"]:
        return False
    if new["kind"] == "global":
        return new["field"] == old["field"]
    if new["kind"] == "section_value":
        return (
            new["header_line"] == old["header_line"]
            and new["field"] == old["field"]
        )
    if new["kind"] == "terminal_price":
        return new.get("scope") == old.get("scope")
    if new["kind"] == "correct":
        return new["record"] == old["record"] and new["field"] == old["field"]
    if new["kind"] == "field_split":
        return (new.get("lines") is None) == (old.get("lines") is None) and (
            new.get("lines") is None or new["lines"] == old["lines"]
        )
    return False


def _unsigned(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k != "id"}


def validate_operations(
    ops: list,
    *,
    lines: list,
    rules: list,
    policy: str = "stock",
) -> tuple[list, str, list]:
    """Validate ops without storing; return (pending, policy, per_op).

    per_op holds the normalized rule each op would store, or None for
    policy ops. Raises DraftError on any invalid op. Shared by proposal
    validation (the model suggests) and apply (the user confirms).
    """
    if not isinstance(ops, list) or not ops:
        raise DraftError("operations must be a non-empty list.")
    if len(ops) > MAX_OPERATIONS:
        raise DraftError(f"At most {MAX_OPERATIONS} operations per request.")
    if policy not in DOCUMENT_POLICIES:
        raise DraftError(f"Unknown policy {policy!r}.")
    pending = [dict(rule) for rule in rules]
    working_policy = policy
    per_op: list = []
    for op in ops:
        validated = _validate_one(op, texts=lines, rules=pending, policy=working_policy)
        if validated is None:
            assert op.get("op") == "policy"
            working_policy = op["policy"]
            per_op.append(None)
            continue
        if validated["kind"] == "drop_rule":
            pending = [
                old for old in pending if old.get("id") != validated["rule"]
            ]
            # Dropping a join or classify can orphan record
            # corrections; prune corrects that no longer address a
            # live candidate instead of keeping dead rules.
            alive = _record_ids(lines, pending)
            pending = [
                old
                for old in pending
                if old["kind"] != "correct" or old["record"] in alive
            ]
            per_op.append(validated)
            continue
        pending = [old for old in pending if not _replaces(validated, old)]
        # Stored rules carry server-assigned ids; a re-applied identical
        # op must dedupe against them instead of piling up copies.
        probe = _unsigned(validated)
        if not any(_unsigned(old) == probe for old in pending):
            pending.append(validated)
        per_op.append(validated)
    # The per-op gate keeps absent-quantity out of a stock draft at
    # creation, but a later policy switch could strand a retained
    # absent rule under a policy that requires quantities. Validate
    # the complete resulting set: switching back to stock must drop
    # the absent-quantity rule in the same batch.
    if working_policy != "price_only" and any(
        rule.get("kind") == "absent" and rule.get("field") == "quantity"
        for rule in pending
    ):
        raise DraftError(
            "The stock policy needs quantities, but this draft keeps "
            "a rule declaring quantity absent. Drop the "
            "absent-quantity rule first, or stay on price_only."
        )
    return pending, working_policy, per_op


def apply_operations(
    ops: list,
    *,
    lines: list,
    rules: list,
    revision_actual: int,
    revision_expected: int | None,
    policy: str = "stock",
) -> tuple[list, str]:
    """Validate ops against the current draft; return (rules, policy).

    Ops apply in order, so one batch can set the policy and then use
    it. Raises DraftConflict on a stale revision, DraftError on any
    invalid op — a failed batch stores nothing.
    """
    if revision_expected is not None and revision_expected != revision_actual:
        raise DraftConflict(
            f"Draft is at revision {revision_actual}; "
            f"request targeted {revision_expected}."
        )
    pending, working_policy, _per_op = validate_operations(
        ops, lines=lines, rules=rules, policy=policy
    )
    if len(pending) > MAX_RULES:
        raise DraftError(f"Drafts hold at most {MAX_RULES} rules.")
    return pending, working_policy
