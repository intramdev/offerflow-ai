"""Tests for deterministic parsers and offer status decisions."""

from decimal import Decimal

from django.test import SimpleTestCase

from pipeline.normalize import (
    classify_lock,
    compute_offer_status,
    grade_options,
    parse_capacity,
    parse_currency,
    parse_price,
    parse_quantity,
)


def full_fields(**overrides):
    fields = {
        "supplier": "Acme",
        "brand": "Pear",
        "model_name": "P1",
        "capacity_gb": 128,
        "capacity_raw": "128GB",
        "color": "Black",
        "grade": "A",
        "grade_raw": "A",
        "lock_state": "unlocked",
        "lock_raw": "Unlocked",
        "quantity": 5,
        "quantity_raw": 5,
        "unit_price": Decimal("299.99"),
        "price_raw": 299.99,
        "currency": "USD",
        "currency_raw": "usd",
        "conditions": "",
    }
    fields.update(overrides)
    return fields


class ParserTests(SimpleTestCase):
    def test_capacity(self):
        self.assertEqual(parse_capacity("128GB"), 128)
        self.assertEqual(parse_capacity("128 GB"), 128)
        self.assertEqual(parse_capacity("128"), 128)
        self.assertEqual(parse_capacity(128), 128)
        self.assertEqual(parse_capacity(128.0), 128)
        self.assertIsNone(parse_capacity("128/256"))
        self.assertIsNone(parse_capacity("N/A"))
        self.assertIsNone(parse_capacity(None))
        self.assertIsNone(parse_capacity(True))
        self.assertIsNone(parse_capacity(0))
        self.assertIsNone(parse_capacity(-5))
        self.assertIsNone(parse_capacity(99999))

    def test_capacity_units(self):
        self.assertEqual(parse_capacity("1TB"), 1024)
        self.assertEqual(parse_capacity("1 TB"), 1024)
        self.assertEqual(parse_capacity("2tb"), 2048)
        self.assertEqual(parse_capacity("0.5TB"), 512)
        self.assertEqual(parse_capacity("1T"), 1024)
        self.assertEqual(parse_capacity("1024MB"), 1)
        self.assertEqual(parse_capacity("2048 MB"), 2)
        self.assertEqual(parse_capacity("128G"), 128)
        self.assertEqual(parse_capacity("500G"), 500)
        # Fractional gigabytes and sub-gigabyte values refuse, never round.
        self.assertIsNone(parse_capacity("1.5GB"))
        self.assertIsNone(parse_capacity("512MB"))
        self.assertIsNone(parse_capacity("16TB"))
        self.assertIsNone(parse_capacity("1PB"))

    def test_quantity(self):
        self.assertEqual(parse_quantity(5), 5)
        self.assertEqual(parse_quantity("5"), 5)
        self.assertEqual(parse_quantity("1,000"), 1000)
        self.assertEqual(parse_quantity("1,000,000"), 1000000)
        self.assertEqual(parse_quantity(5.0), 5)
        self.assertEqual(parse_quantity(0), 0)
        self.assertIsNone(parse_quantity(5.5))
        self.assertIsNone(parse_quantity(-1))
        self.assertIsNone(parse_quantity("five"))
        self.assertIsNone(parse_quantity("1,5"))
        self.assertIsNone(parse_quantity("10,50"))
        self.assertIsNone(parse_quantity(None))

    def test_price(self):
        self.assertEqual(parse_price("12.50"), Decimal("12.50"))
        self.assertEqual(parse_price(12.5), Decimal("12.5"))
        self.assertEqual(parse_price("1,299.99"), Decimal("1299.99"))
        self.assertEqual(parse_price("10,5"), Decimal("10.5"))
        self.assertEqual(parse_price("10,50"), Decimal("10.50"))
        self.assertEqual(parse_price("1,000"), Decimal("1000"))
        self.assertEqual(parse_price("1,000,000"), Decimal("1000000"))
        self.assertIsNone(parse_price("1,000,00"))
        self.assertIsNone(parse_price("1,0000"))
        self.assertIsNone(parse_price(0))
        self.assertIsNone(parse_price(-3))
        self.assertIsNone(parse_price("free"))
        self.assertIsNone(parse_price("=1+1"))
        self.assertIsNone(parse_price(None))

    def test_currency(self):
        self.assertEqual(parse_currency("usd"), "USD")
        self.assertEqual(parse_currency(" USD "), "USD")
        self.assertIsNone(parse_currency("US"))
        self.assertIsNone(parse_currency("EURO"))
        self.assertIsNone(parse_currency(123))
        self.assertIsNone(parse_currency(None))

    def test_lock_classification(self):
        self.assertEqual(classify_lock("Unlocked"), "unlocked")
        self.assertEqual(classify_lock("Factory unlocked"), "unlocked")
        self.assertEqual(classify_lock("Locked"), "locked")
        self.assertEqual(classify_lock("iCloud locked"), "locked")
        self.assertEqual(classify_lock("loked"), "locked")
        self.assertEqual(classify_lock(""), "unknown")
        self.assertEqual(classify_lock(None), "unknown")
        self.assertEqual(classify_lock("maybe"), "unknown")

    def test_grade_options(self):
        self.assertEqual(grade_options("A/B"), ["A", "B", "A/B"])
        self.assertEqual(grade_options("A / B"), ["A", "B", "A / B"])
        self.assertIsNone(grade_options("A"))
        self.assertIsNone(grade_options(""))
        self.assertIsNone(grade_options(None))


class StatusTests(SimpleTestCase):
    def test_complete_row_is_ok(self):
        status, gaps, reason = compute_offer_status(full_fields())
        self.assertEqual(status, "ok")
        self.assertEqual(gaps, [])
        self.assertEqual(reason, "")

    def test_optional_fields_blank_still_ok(self):
        status, gaps, _ = compute_offer_status(
            full_fields(brand="", color="", conditions="")
        )
        self.assertEqual(status, "ok")

    def test_locked_excluded_without_gaps(self):
        status, gaps, reason = compute_offer_status(
            full_fields(lock_state="locked", unit_price=None, currency="")
        )
        self.assertEqual(status, "excluded")
        self.assertEqual(gaps, [])
        self.assertIn("Locked", reason)

    def test_missing_values_raise_gaps(self):
        status, gaps, _ = compute_offer_status(
            full_fields(unit_price=None, currency="", capacity_gb=None)
        )
        self.assertEqual(status, "needs_input")
        self.assertEqual(
            {gap["field"] for gap in gaps}, {"unit_price", "currency", "capacity_gb"}
        )
        self.assertTrue(all(gap["kind"] == "value" for gap in gaps))

    def test_ambiguous_grade_raises_choice(self):
        status, gaps, _ = compute_offer_status(
            full_fields(grade="", grade_raw="A/B")
        )
        self.assertEqual(status, "needs_input")
        grade_gaps = [gap for gap in gaps if gap["field"] == "grade"]
        self.assertEqual(len(grade_gaps), 1)
        self.assertEqual(grade_gaps[0]["kind"], "choice")
        self.assertEqual(grade_gaps[0]["choices"], ["A", "B", "A/B"])

    def test_decided_grade_not_regapped(self):
        status, gaps, _ = compute_offer_status(
            full_fields(grade="A", grade_raw="A/B")
        )
        self.assertEqual(status, "ok")

    def test_unknown_lock_raises_choice(self):
        status, gaps, _ = compute_offer_status(
            full_fields(lock_state="unknown", lock_raw="maybe")
        )
        self.assertEqual(status, "needs_input")
        lock_gaps = [gap for gap in gaps if gap["field"] == "lock_state"]
        self.assertEqual(lock_gaps[0]["choices"], ["locked", "unlocked"])

    def test_absent_lock_raises_no_gap(self):
        for raw in (None, "", "  "):
            status, gaps, _ = compute_offer_status(
                full_fields(lock_state="unknown", lock_raw=raw)
            )
            self.assertEqual(status, "ok", f"lock_raw={raw!r}")
            self.assertEqual(gaps, [], f"lock_raw={raw!r}")
