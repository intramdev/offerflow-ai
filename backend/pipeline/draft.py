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
    }
)

MAX_OPERATIONS = 50
MAX_JOIN_LINES = 20
MAX_RULES = 200

_NUMBER_TOKEN_RE = re.compile(r"\d[\d.,]*")
_RECORD_RE = re.compile(r"^L(\d+)(?:-L(\d+))?$")


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
            override = corrects.get(record_id, {}).get(field)
            if override is not None:
                entry = {
                    "value": override["value"],
                    "state": "user_confirmed",
                    "by": "correction",
                    "rule": override.get("id"),
                }
            fields[field] = entry
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
    if "supplier" not in known and not any(
        rule["kind"] == "section_value" and rule["field"] == "supplier"
        for rule in rules
    ):
        issues.append(
            {
                "scope": "document",
                "field": "supplier",
                "reason": "No supplier confirmed for this document.",
            }
        )
    if "currency" not in known and not any(
        rule["kind"] in ("section_value", "terminal_price")
        and rule.get("currency", rule.get("value"))
        for rule in rules
    ):
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

    Merges confirmed global, section, and correction values with
    terminal-price derivation, so the preview, the worker import, and
    the exports compute from one implementation. Returns
    {record_id: {"lines", "class", "section", "cells", "terminal"}},
    where "terminal" carries the bound span, currency, and line when a
    covering rule resolved, and None otherwise.
    """
    records, _issues, _sections = derive_records(texts, rules or [])
    terminals = [rule for rule in (rules or []) if rule["kind"] == "terminal_price"]
    derived = {}
    for record in records:
        cells = {}
        for field, entry in record["fields"].items():
            if entry["state"] != "unknown" and entry["value"] is not None:
                cells[field] = entry["value"]
        terminal = None
        for rule in terminals:
            if not _terminal_covers(rule, rules or [], record["id"]):
                continue
            last = record["lines"][-1]
            found = _terminal_span(texts[last - 1])
            if found is None:
                continue
            amount = parse_price(found[0])
            if amount is None:
                continue
            cells["price"] = str(amount)
            cells["currency"] = rule["currency"]
            terminal = {
                "span": [found[1], found[2]],
                "currency": rule["currency"],
                "line": last,
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
