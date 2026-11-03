"""Deterministic normalization (rules v1).

Every parser returns None for anything it cannot read exactly. Gaps become
review work or exclusions — the normalizer never guesses a capacity, grade,
currency, price, or lock state to make a row look complete.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

CAPACITY_RE = re.compile(
    r"^\s*(\d{1,4}(?:\.\d+)?)\s*(tb|t|gb|g|mb|m)?\s*$", re.IGNORECASE
)
# Gigabytes per unit. Binary units: 1 TB = 1024 GB.
CAPACITY_UNIT_GB = {"tb": 1024, "t": 1024, "gb": 1, "g": 1, "mb": 1 / 1024, "m": 1 / 1024}
MAX_CAPACITY_GB = 8192
CURRENCY_RE = re.compile(r"^\s*([A-Za-z]{3})\s*$")
INTEGER_RE = re.compile(r"^\d+$")

# Unlocked markers are checked before locked markers because "unlocked"
# contains "lock". "loked" covers a recurring vendor misspelling of locked.
UNLOCKED_MARKERS = ("unlocked", "factory unlock", "sim free", "sim-free", "open to all")
LOCKED_MARKERS = (
    "locked",
    "loked",
    "icloud",
    "carrier lock",
    "sim lock",
    "activation lock",
    "blacklist",
)

LOCKED = "locked"
UNLOCKED = "unlocked"
UNKNOWN = "unknown"

#: Product domains for required-field rules. Device files (phones and
#: similar) require capacity and grade per row; general files (office
#: supplies and the like) treat them as not applicable. Anything other
#: than DOMAIN_GENERAL behaves as device, so undecided files keep the
#: established phone checks.
DOMAIN_DEVICE = "device"
DOMAIN_GENERAL = "general"

#: Reviewer-facing domain answers, in choice order. The first keeps
#: per-row device questions; the second resolves them as not applicable.
DOMAIN_CHOICE_DEVICE = "Phones or devices — capacity and grade required"
DOMAIN_CHOICE_GENERAL = "Other products — capacity and grade do not apply"
DOMAIN_CHOICES = (DOMAIN_CHOICE_DEVICE, DOMAIN_CHOICE_GENERAL)
DOMAIN_BY_CHOICE = {
    DOMAIN_CHOICE_DEVICE: DOMAIN_DEVICE,
    DOMAIN_CHOICE_GENERAL: DOMAIN_GENERAL,
}

# Gap field -> canonical source cell. Only these cells may come from a
# model proposal; everything else stays exactly as the source read it.
GAP_CELL_FIELDS = {
    "supplier": "supplier",
    "model_name": "model",
    "capacity_gb": "capacity",
    "grade": "grade",
    "lock_state": "lock",
    "quantity": "quantity",
    "unit_price": "price",
    "currency": "currency",
}

# Offer fields a human clarification may authorize. Anything else
# (brand, color, conditions) always traces straight to source cells.
REVIEWABLE_FIELDS = frozenset(GAP_CELL_FIELDS)

# Proposed source cells whose numbers must occur in the message line
# before the merge adopts them. Other gap cells (supplier, model,
# grade, lock, currency) keep the existing merged-evidence treatment:
# the per-row gate authorizes them and release approval covers them.
_GROUNDED_NUMERIC_CELLS = frozenset({"quantity", "capacity", "price"})
_NUMBER_TOKEN_RE = re.compile(r"\d[\d.,]*")
_PLAIN_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")

# Optional cells the merge may adopt when the source left them empty.
# They never create gaps, so they are handled outside the gap mapping.
OPTIONAL_ADOPTABLE_CELLS = frozenset({"brand", "color", "conditions"})

# A number glued to a storage unit is capacity, never stock: neither a
# model proposal nor the quantity gate may read "256" from "256GB".
_CAPACITY_TOKEN_RE = re.compile(r"(\d[\d.,]*)\s*(?:gb|tb|mb)\b", re.IGNORECASE)

# Explicit stock markings. Under a confirmed terminal-price rule only a
# marked number may become stock quantity; anything else stays unknown.
_QUANTITY_MARK_RE = re.compile(
    r"(?:\b(?:qty|quantity|pcs|pieces|units?|stock)(?:\s*:)?\s*\d"
    r"|\b[x×]\s*\d"
    r"|\d+\s*(?:pcs|pieces|units?)\b)",
    re.IGNORECASE,
)

_TERMINAL_TRAILING_WORD_RE = re.compile(r"\s+[A-Za-z]{3}\s*$")
# A unit word after the currency ("289 EUR each") is explicit pricing
# prose, not a different number: strip one allowlisted word so the
# terminal amount still binds to its span. Non-numeric by
# construction, so model/capacity roles cannot leak through here.
_TERMINAL_TRAILING_UNIT_RE = re.compile(r"\s+(each|piece|unit)\s*$", re.IGNORECASE)
_TERMINAL_TRAILING_PUNCT_RE = re.compile(r"[^\w]+$")
_TERMINAL_NUMBER_RE = re.compile(r"(\d[\d.,]*)\s*$")
_TERMINAL_SEPARATORS = frozenset([" ", "\t", ":", ";", "|", "-", ",", "–", "—", ">", "(", ")"])


def has_quantity_marking(text: str) -> bool:
    """Whether a line carries an explicit stock-quantity marking."""
    return bool(_QUANTITY_MARK_RE.search(text or ""))


def _strip_trailing_word(text: str) -> str:
    """Drop one trailing 3-letter word such as a currency code."""
    return _TERMINAL_TRAILING_WORD_RE.sub("", text)


def terminal_amount(text: str) -> tuple[str, int, int] | None:
    """Return the terminal number token and its source span, if resolvable.

    The terminal token is the last number in the line once trailing
    punctuation, one trailing unit word ("each"), and one trailing
    3-letter word (a currency code such as "USD") are stripped. It
    must be separated from the text before it, so "128GB" never
    resolves and model/capacity digits keep their own roles. Returns
    (token, start, end) or None when no terminal number can be bound
    to a span.
    """
    if not text or not isinstance(text, str):
        return None
    candidate = text.strip()
    candidate = _TERMINAL_TRAILING_PUNCT_RE.sub("", candidate)
    candidate = _TERMINAL_TRAILING_UNIT_RE.sub("", candidate)
    candidate = _TERMINAL_TRAILING_PUNCT_RE.sub("", candidate)
    candidate = _strip_trailing_word(candidate)
    candidate = _TERMINAL_TRAILING_PUNCT_RE.sub("", candidate)
    match = _TERMINAL_NUMBER_RE.search(candidate)
    if not match:
        return None
    start = match.start(1)
    if start > 0 and candidate[start - 1] not in _TERMINAL_SEPARATORS:
        return None
    return match.group(1), start, match.end(1)


def parse_capacity(raw) -> int | None:
    """Read whole gigabytes from values like 128, "128GB", "1TB", "1024MB".

    Bare numbers mean gigabytes. Terabyte and megabyte values convert to
    gigabytes and must land on a whole gigabyte: "512MB" is refused
    rather than rounded, and anything outside 1 GB..8192 GB asks the
    reviewer instead of guessing.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if float(raw).is_integer() and 0 < raw <= MAX_CAPACITY_GB:
            return int(raw)
        return None
    if isinstance(raw, str):
        match = CAPACITY_RE.match(raw)
        if match:
            try:
                amount = Decimal(match.group(1))
            except InvalidOperation:
                return None
            unit = (match.group(2) or "gb").lower()
            gigabytes = amount * Decimal(str(CAPACITY_UNIT_GB[unit]))
            if gigabytes == gigabytes.to_integral_value() and 0 < gigabytes <= MAX_CAPACITY_GB:
                return int(gigabytes)
    return None


def parse_quantity(raw) -> int | None:
    """Read a non-negative whole quantity, tolerating thousands separators.

    Comma groups must be exact thousands ("1,000"); anything else with a
    comma ("1,5") is refused rather than misread.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float):
        if raw.is_integer() and raw >= 0:
            return int(raw)
        return None
    if isinstance(raw, str):
        text = raw.strip().replace(" ", "")
        if "," in text:
            groups = text.split(",")
            if (
                not groups[0].isdigit()
                or len(groups[0]) > 3
                or any(len(part) != 3 or not part.isdigit() for part in groups[1:])
            ):
                return None
            text = "".join(groups)
        if INTEGER_RE.match(text):
            return int(text)
    return None


def _valid_thousands(part: str, sep: str) -> str | None:
    """Join exact thousands groups ("1.234"), or None when malformed."""
    groups = part.split(sep)
    if (
        len(groups) < 2
        or not groups[0].isdigit()
        or not 1 <= len(groups[0]) <= 3
        or any(len(piece) != 3 or not piece.isdigit() for piece in groups[1:])
    ):
        return None
    return "".join(groups)


def parse_price(raw) -> Decimal | None:
    """Read a positive decimal price, preserving supplier precision.

    Explicit formats only. With both separators the last one wins as
    the decimal mark ("1,234.56", "1.234,56"); the other side must be
    exact thousands grouping. A lone comma keeps the documented US
    rule: a three-digit tail means thousands ("1,000"), a one- or
    two-digit tail means a European decimal ("10,5"). A lone dot with
    a three-digit tail and a short head ("1.234") is genuinely
    ambiguous — US decimal or EU thousands — so it is refused and the
    reviewer decides instead of the parser guessing.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        text = str(raw)
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        try:
            text = str(raw).strip()
        except Exception:
            return None
    sign = ""
    if text[:1] in ("+", "-"):
        sign, text = text[0], text[1:]
    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        if text.rfind(",") > text.rfind("."):
            integer, decimal = text.rsplit(",", 1)
            joined = _valid_thousands(integer, ".")
        else:
            integer, decimal = text.rsplit(".", 1)
            joined = _valid_thousands(integer, ",")
        if joined is None or not decimal or not decimal.isdigit():
            return None
        text = f"{joined}.{decimal}"
    elif has_comma:
        groups = text.split(",")
        if len(groups) > 2:
            joined = _valid_thousands(text, ",")
            if joined is None:
                return None
            text = joined
        else:
            head, tail = groups
            if len(tail) == 3 and tail.isdigit() and head.isdigit():
                text = head + tail
            elif head.isdigit() and tail.isdigit() and len(tail) in (1, 2):
                text = f"{head}.{tail}"
            else:
                return None
    elif has_dot:
        if text.count(".") > 1:
            joined = _valid_thousands(text, ".")
            if joined is None:
                return None
            text = joined
        else:
            head, tail = text.split(".")
            if (
                len(tail) == 3
                and tail.isdigit()
                and head.isdigit()
                and 1 <= len(head) <= 3
            ):
                return None
    try:
        value = Decimal(sign + text)
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


CURRENCY_ALIASES = {
    "$": "USD",
    "us$": "USD",
    "€": "EUR",
    "£": "GBP",
    "kč": "CZK",
    "czk": "CZK",
}


def parse_currency(raw) -> str | None:
    """Read a three-letter currency code, uppercased.

    A small alias table covers unambiguous symbols (¥ is deliberately
    absent: it means both JPY and CNY, so it stays a review question).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    alias = CURRENCY_ALIASES.get(text.lower())
    if alias is not None:
        return alias
    match = CURRENCY_RE.match(text)
    if match:
        return match.group(1).upper()
    return None


def classify_lock(raw) -> str:
    """Classify lock state without guessing unknown wording.

    SIM/carrier-unlocked wording plus a lock signal (notably an
    activation/iCloud lock) is a conflict, not an unlocked device:
    it stays unknown so a human confirms the real state.
    """
    if raw is None:
        return UNKNOWN
    text = str(raw).strip().lower()
    if not text:
        return UNKNOWN
    # "unlocked" contains "lock", so unlocked wording is lifted out
    # before looking for an independent lock signal.
    remainder = text
    saw_unlocked = False
    for marker in UNLOCKED_MARKERS:
        if marker in remainder:
            saw_unlocked = True
            remainder = remainder.replace(marker, " ")
    saw_locked = any(marker in remainder for marker in LOCKED_MARKERS)
    if saw_unlocked and saw_locked:
        return UNKNOWN
    if saw_unlocked:
        return UNLOCKED
    if saw_locked:
        return LOCKED
    return UNKNOWN


def _capacity_spans(message: str) -> list:
    """Source spans of numbers glued to a storage unit (capacity role)."""
    spans = []
    for match in _CAPACITY_TOKEN_RE.finditer(message):
        spans.append((match.start(1), match.end(1)))
    return spans


def _proposal_grounded(proposal, message: str, cell: str | None = None) -> bool:
    """Check that every number in a proposal occurs in the message line.

    Numbers compare digit-for-digit after stripping separators, so a
    transcribed "1.234,56" grounds "1234.56" without locale guessing.
    Unambiguous plain numbers ("450.00" vs "450") also match by exact
    decimal equality. A proposal with no digits at all passes here —
    the normalizer either reads it or leaves the gap for a human.

    Stock quantity proposals must additionally avoid the capacity role:
    a number glued to GB/TB/MB never grounds "quantity", so "256" from
    "256GB" cannot become stock.
    """
    tokens = _NUMBER_TOKEN_RE.findall("" if proposal is None else str(proposal))
    if not tokens:
        return True
    excluded = _capacity_spans(message) if cell == "quantity" else []
    line_tokens = []
    for match in _NUMBER_TOKEN_RE.finditer(message):
        if any(start <= match.start() < end for start, end in excluded):
            continue
        line_tokens.append(match.group(0))
    line_digits = {
        re.sub(r"\D", "", token)
        for token in line_tokens
        if re.sub(r"\D", "", token)
    }
    for token in tokens:
        stripped = re.sub(r"\D", "", token)
        if stripped and stripped in line_digits:
            continue
        if _PLAIN_NUMBER_RE.match(token) and any(
            _plain_equal(token, other) for other in line_tokens
        ):
            continue
        return False
    return True


def _plain_equal(left: str, right: str) -> bool:
    if not (_PLAIN_NUMBER_RE.match(left) and _PLAIN_NUMBER_RE.match(right)):
        return False
    try:
        return Decimal(left) == Decimal(right)
    except InvalidOperation:
        return False


def merge_interpreted_cells(
    local_cells: dict,
    interpreted_cells: dict,
    document_policy: str = "stock",
    domain: str = DOMAIN_DEVICE,
) -> dict:
    """Fold a model proposal into source cells without overwriting reads.

    Only cells for fields the local read could not fill come from the
    proposal; every readable value stays byte-identical to the source.
    Both the offer builder and the evidence gate use this merge, so a
    model can never widen its own mandate by rewriting other fields.

    On message rows (pasted chat-style lines), proposed numeric values
    must additionally be grounded: every number in the proposal has to
    occur in the source line. An ungrounded proposal is dropped, leaving
    the gap open for a human instead of silently adopting invention.
    Optional cells (brand, color, conditions) are adopted when the
    source left them empty, so a correct proposal is preserved rather
    than dropped for being optional. Under the general domain the
    device cells (capacity, grade) are never gaps, so proposals for
    them are not adopted either.
    """
    fields = normalize_cells(local_cells)
    _status, gaps, _reason = compute_offer_status(
        fields, document_policy=document_policy, domain=domain
    )
    wanted = {
        GAP_CELL_FIELDS[gap["field"]]
        for gap in gaps
        if gap["field"] in GAP_CELL_FIELDS
    }
    merged = dict(local_cells)
    message = local_cells.get("message")
    message = message if isinstance(message, str) and message.strip() else None
    for key in wanted:
        if key not in interpreted_cells:
            continue
        proposal = interpreted_cells[key]
        if (
            message is not None
            and key in _GROUNDED_NUMERIC_CELLS
            and not _proposal_grounded(proposal, message, key)
        ):
            continue
        merged[key] = proposal
    for key in OPTIONAL_ADOPTABLE_CELLS:
        if key not in interpreted_cells:
            continue
        if str(local_cells.get(key) or "").strip():
            continue
        proposal = interpreted_cells[key]
        if proposal is None or not str(proposal).strip():
            continue
        merged[key] = proposal
    return merged


def parse_review_value(field: str, value):
    """Parse a human-supplied VALUE answer for one offer field.

    Shared by answer recording and evidence validation so both agree
    on what a decision authorizes. Returns None when unparseable.
    """
    if field == "quantity":
        return parse_quantity(value)
    if field == "unit_price":
        return parse_price(value)
    if field == "capacity_gb":
        return parse_capacity(value)
    if field == "currency":
        return parse_currency(value)
    if field in ("supplier", "model_name", "grade"):
        if value is None:
            return None
        text = value.strip() if isinstance(value, str) else str(value).strip()
        return text or None
    return None


def grade_options(raw) -> list[str] | None:
    """Return confirmation options for ambiguous grades, else None.

    A grade containing "/" is never assumed to mean two acceptable grades:
    the reviewer picks one part or confirms the raw term as a single grade.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or "/" not in text:
        return None
    parts = [part.strip() for part in text.split("/") if part.strip()]
    options = parts + ([text] if text not in parts else [])
    if len(options) > 1:
        return options
    return None


def _short(raw) -> str:
    text = "" if raw is None else str(raw).strip()
    return text[:120]


def compute_offer_status(
    fields: dict, document_policy: str = "stock", domain: str = DOMAIN_DEVICE
) -> tuple[str, list, str]:
    """Decide ok/excluded/needs_input for normalized fields.

    Returns the status, the list of review gaps, and an exclusion reason.
    Locked offers are excluded without raising gaps for their other fields.
    Brand, color, and conditions are optional and never block release.
    Lock info that is simply absent also blocks nothing; only lock text
    that was provided but cannot be read asks for confirmation.
    Under the price_only document policy an absent quantity stays unknown
    instead of raising a gap; stock lists still require it.
    Under the general domain capacity and grade are not applicable, so
    they raise no gaps; device files keep the established per-row checks.
    """
    from pipeline.models import OfferStatus

    gaps: list = []
    supplier = (fields.get("supplier") or "").strip()
    model_name = (fields.get("model_name") or "").strip()
    if not supplier:
        gaps.append(
            {
                "field": "supplier",
                "kind": "value",
                "question": "Supplier is missing for this row. Supply the supplier name.",
            }
        )
    if not model_name:
        gaps.append(
            {
                "field": "model_name",
                "kind": "value",
                "question": "Model is missing for this row. Supply the model name.",
            }
        )
    device_fields_apply = domain != DOMAIN_GENERAL
    if device_fields_apply and fields.get("capacity_gb") is None:
        gaps.append(
            {
                "field": "capacity_gb",
                "kind": "value",
                "question": (
                    "Capacity could not be read for this row "
                    f"(got {_short(fields.get('capacity_raw'))!r}). "
                    "Supply gigabytes as a whole number."
                ),
            }
        )
    grade = (fields.get("grade") or "").strip()
    grade_raw = (fields.get("grade_raw") or "").strip()
    if device_fields_apply and not grade:
        options = grade_options(grade_raw)
        if options is not None:
            gaps.append(
                {
                    "field": "grade",
                    "kind": "choice",
                    "question": (
                        f"Grade {grade_raw!r} is ambiguous. Pick one part or "
                        "confirm the raw term as a single grade."
                    ),
                    "choices": options,
                }
            )
        else:
            gaps.append(
                {
                    "field": "grade",
                    "kind": "value",
                    "question": "Grade is missing for this row. Supply the grade term.",
                }
            )
    lock_state = fields.get("lock_state") or UNKNOWN
    lock_raw = fields.get("lock_raw")
    if lock_state == LOCKED:
        return OfferStatus.EXCLUDED, [], f"Locked device excluded ({_short(lock_raw)})."
    if lock_state == UNKNOWN and str(lock_raw or "").strip():
        gaps.append(
            {
                "field": "lock_state",
                "kind": "choice",
                "question": (
                    "Lock state could not be read for this row "
                    f"(got {_short(lock_raw)!r}). Confirm locked or unlocked."
                ),
                "choices": [LOCKED, UNLOCKED],
            }
        )
    if fields.get("quantity") is None and document_policy != "price_only":
        gaps.append(
            {
                "field": "quantity",
                "kind": "value",
                "question": (
                    "Quantity could not be read for this row "
                    f"(got {_short(fields.get('quantity_raw'))!r}). "
                    "Supply a whole number."
                ),
            }
        )
    if fields.get("unit_price") is None:
        gaps.append(
            {
                "field": "unit_price",
                "kind": "value",
                "question": (
                    "Unit price could not be read for this row "
                    f"(got {_short(fields.get('price_raw'))!r}). "
                    "Supply a positive price."
                ),
            }
        )
    if not (fields.get("currency") or "").strip():
        gaps.append(
            {
                "field": "currency",
                "kind": "value",
                "question": (
                    "Currency could not be read for this row "
                    f"(got {_short(fields.get('currency_raw'))!r}). "
                    "Supply a three-letter code."
                ),
            }
        )
    if gaps:
        return OfferStatus.NEEDS_INPUT, gaps, ""
    return OfferStatus.OK, [], ""


def normalize_cells(cells: dict) -> dict:
    """Convert canonical raw cells into offer fields plus raw evidence."""
    capacity_raw = cells.get("capacity")
    quantity_raw = cells.get("quantity")
    price_raw = cells.get("price")
    currency_raw = cells.get("currency")
    lock_raw = cells.get("lock")
    grade_raw = cells.get("grade")
    lock_state = classify_lock(lock_raw)
    options = grade_options(grade_raw)
    return {
        "supplier": (cells.get("supplier") or "").strip() if cells.get("supplier") is not None else "",
        "brand": (cells.get("brand") or "").strip() if cells.get("brand") is not None else "",
        "model_name": (cells.get("model") or "").strip() if cells.get("model") is not None else "",
        "capacity_gb": parse_capacity(capacity_raw),
        "capacity_raw": capacity_raw,
        "color": (cells.get("color") or "").strip() if cells.get("color") is not None else "",
        "grade": "" if options is not None or not (grade_raw or "").strip() else str(grade_raw).strip(),
        "grade_raw": "" if grade_raw is None else str(grade_raw).strip(),
        "lock_state": lock_state,
        "lock_raw": lock_raw,
        "quantity": parse_quantity(quantity_raw),
        "quantity_raw": quantity_raw,
        "unit_price": parse_price(price_raw),
        "price_raw": price_raw,
        "currency": parse_currency(currency_raw) or "",
        "currency_raw": currency_raw,
        "conditions": (cells.get("conditions") or "").strip() if cells.get("conditions") is not None else "",
    }
