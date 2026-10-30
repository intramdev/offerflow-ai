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


def parse_price(raw) -> Decimal | None:
    """Read a positive decimal price, preserving supplier precision.

    A lone comma acts as the European decimal separator ("10,5" -> 10.5);
    with a dot present, commas are thousands separators ("1,000.50").
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
    if "," in text and "." not in text:
        groups = text.split(",")
        if len(groups) > 2:
            if (
                not groups[0].isdigit()
                or len(groups[0]) > 3
                or any(len(part) != 3 or not part.isdigit() for part in groups[1:])
            ):
                return None
            text = "".join(groups)
        else:
            head, tail = groups
            if len(tail) == 3 and tail.isdigit() and head.isdigit():
                text = head + tail
            elif head.isdigit() and tail.isdigit() and len(tail) in (1, 2):
                text = f"{head}.{tail}"
            else:
                return None
    else:
        text = text.replace(",", "")
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
    """Classify lock state without guessing unknown wording."""
    if raw is None:
        return UNKNOWN
    text = str(raw).strip().lower()
    if not text:
        return UNKNOWN
    if any(marker in text for marker in UNLOCKED_MARKERS):
        return UNLOCKED
    if any(marker in text for marker in LOCKED_MARKERS):
        return LOCKED
    return UNKNOWN


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


def compute_offer_status(fields: dict) -> tuple[str, list, str]:
    """Decide ok/excluded/needs_input for normalized fields.

    Returns the status, the list of review gaps, and an exclusion reason.
    Locked offers are excluded without raising gaps for their other fields.
    Brand, color, and conditions are optional and never block release.
    Lock info that is simply absent also blocks nothing; only lock text
    that was provided but cannot be read asks for confirmation.
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
    if fields.get("capacity_gb") is None:
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
    if not grade:
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
    if fields.get("quantity") is None:
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
