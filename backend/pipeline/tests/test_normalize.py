"""Tests for deterministic parsers and offer status decisions."""

from decimal import Decimal

from django.test import SimpleTestCase

from pipeline.normalize import (
    classify_lock,
    compute_offer_status,
    grade_options,
    merge_interpreted_cells,
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

    def test_price_european_and_mixed_formats(self):
        self.assertEqual(parse_price("1.234,56"), Decimal("1234.56"))
        self.assertEqual(parse_price("1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_price("1.234.567,89"), Decimal("1234567.89"))
        self.assertEqual(parse_price("1,234,567.89"), Decimal("1234567.89"))
        self.assertEqual(parse_price("12.345.678"), Decimal("12345678"))
        self.assertEqual(parse_price("1234.567"), Decimal("1234.567"))

    def test_price_rejects_ambiguity(self):
        # A lone dot with a three-digit tail reads as US decimal or EU
        # thousands; refusing asks the reviewer instead of guessing.
        self.assertIsNone(parse_price("1.234"))
        self.assertIsNone(parse_price("12.345"))
        # Broken thousands grouping on either side is refused.
        self.assertIsNone(parse_price("1,23.45"))
        self.assertIsNone(parse_price("1.23,45"))
        self.assertIsNone(parse_price("1.234,56.78"))
        self.assertIsNone(parse_price("1,234.56,78"))

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

    def test_lock_conflict_needs_clarification(self):
        # SIM/carrier-unlocked wording plus an activation/iCloud lock
        # signal must never resolve to an unrestricted device.
        self.assertEqual(
            classify_lock("Factory unlocked; iCloud locked"), "unknown"
        )
        self.assertEqual(classify_lock("unlocked, blacklist"), "unknown")
        self.assertEqual(classify_lock("locked but sim free"), "unknown")

    def test_grade_options(self):
        self.assertEqual(grade_options("A/B"), ["A", "B", "A/B"])
        self.assertEqual(grade_options("A / B"), ["A", "B", "A / B"])
        self.assertIsNone(grade_options("A"))
        self.assertIsNone(grade_options(""))

    def test_merge_keeps_readable_values(self):
        local = {
            "supplier": "Acme",
            "brand": "Pear",
            "model": "P1",
            "capacity": "128",
            "color": "Red",
            "grade": "",
            "lock": "Unlocked",
            "quantity": "5",
            "price": "299.99",
            "currency": "USD",
            "conditions": "",
        }
        model = dict(local)
        model["grade"] = "A"
        model["price"] = "199.99"
        model["quantity"] = "9"
        merged = merge_interpreted_cells(local, model)
        # Only the missing grade comes from the model; readable
        # price and quantity stay exactly as the source read them.
        self.assertEqual(merged["grade"], "A")
        self.assertEqual(merged["price"], "299.99")
        self.assertEqual(merged["quantity"], "5")
        self.assertEqual(merged["supplier"], "Acme")
        self.assertIsNone(grade_options(None))

    def test_merge_drops_ungrounded_message_numbers(self):
        local = {
            "message": "iPhone 15 128GB Black x5 450",
            "supplier": "Acme",
        }
        model = {
            "model": "iPhone 15",
            "capacity": "128",
            "grade": "A",
            "quantity": "9",
            "price": "450",
            "currency": "USD",
        }
        merged = merge_interpreted_cells(local, model)
        # Grounded numbers come from the proposal; the invented
        # quantity stays out so the gap asks a human instead.
        self.assertEqual(merged["capacity"], "128")
        self.assertEqual(merged["price"], "450")
        self.assertNotIn("quantity", merged)

    def test_merge_accepts_eu_price_transcription(self):
        local = {"message": "P1 128GB a 1.234,56 EUR", "supplier": "Acme"}
        merged = merge_interpreted_cells(local, {"price": "1234.56"})
        self.assertEqual(merged["price"], "1234.56")

    def test_merge_skips_grounding_without_message(self):
        local = {
            "supplier": "Acme",
            "model": "P1",
            "capacity": "128",
            "grade": "A",
            "lock": "Unlocked",
            "quantity": "five",
            "price": "299.99",
            "currency": "USD",
        }
        merged = merge_interpreted_cells(local, {"quantity": "5"})
        # Spreadsheet gaps keep the existing merged-evidence
        # treatment: the unreadable cell is the evidence, not a line.
        self.assertEqual(merged["quantity"], "5")


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


class TerminalSpanTests(SimpleTestCase):
    def test_span_binds_token(self):
        from pipeline.normalize import terminal_amount

        text = "> Phone 18 Pro 256GB - 417"
        found = terminal_amount(text)
        self.assertIsNotNone(found)
        token, start, end = found
        self.assertEqual(token, "417")
        self.assertEqual(text[start:end], "417")

    def test_capacity_and_glued_numbers_never_terminal(self):
        from pipeline.normalize import terminal_amount

        self.assertIsNone(terminal_amount("iPhone 8 64GB"))
        self.assertIsNone(terminal_amount("SKU123"))
        self.assertIsNone(terminal_amount("P1+ - Like New"))

    def test_trailing_currency_word_is_kept(self):
        from pipeline.normalize import terminal_amount

        found = terminal_amount("Phone Z - 417 USD")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "417")

    def test_trailing_unit_word_after_currency_resolves(self):
        from pipeline.normalize import terminal_amount

        for text in (
            "Apple iPhone 13 with 128GB in Grade A: 12 pcs at 289 EUR each",
            "Phone Z - 289 each",
            "Phone Z - 289 EACH",
        ):
            found = terminal_amount(text)
            self.assertIsNotNone(found, text)
            token, start, end = found
            self.assertEqual(token, "289")
            self.assertEqual(text[start:end], "289")
        # Glued numbers and numberless lines still refuse.
        self.assertIsNone(terminal_amount("iPhone 8 64GB each"))
        self.assertIsNone(terminal_amount("warranty each"))
        self.assertIsNone(terminal_amount("P1+ - Like New each"))

    def test_markings_detected(self):
        from pipeline.normalize import has_quantity_marking

        for marked in ("Phone Z x5 450", "qty: 3", "10 pcs", "Stock 7"):
            self.assertTrue(has_quantity_marking(marked), marked)
        for plain in ("Phone Z 450", "P1+ - Like New", ""):
            self.assertFalse(has_quantity_marking(plain), plain)


class RoleGroundingTests(SimpleTestCase):
    def test_capacity_number_cannot_ground_quantity(self):
        local = {"message": "> Phone 18 Pro 256GB - 417"}
        merged = merge_interpreted_cells(local, {"quantity": "256"})
        self.assertNotIn("quantity", merged)

    def test_marked_quantity_grounds(self):
        local = {"message": "Phone Z x5 450"}
        merged = merge_interpreted_cells(local, {"quantity": "5"})
        self.assertEqual(merged["quantity"], "5")

    def test_optional_cells_adopted_when_source_empty(self):
        local = {"message": "Phone Z 450", "brand": ""}
        merged = merge_interpreted_cells(
            local, {"brand": "Apple", "conditions": "Ready in DXB"}
        )
        self.assertEqual(merged["brand"], "Apple")
        self.assertEqual(merged["conditions"], "Ready in DXB")

    def test_source_optional_cells_win(self):
        local = {"message": "Phone Z 450", "brand": "Pear"}
        merged = merge_interpreted_cells(local, {"brand": "Apple"})
        self.assertEqual(merged["brand"], "Pear")


class PriceOnlyPolicyTests(SimpleTestCase):
    def test_absent_quantity_gaps_under_stock(self):
        fields = full_fields(quantity=None, quantity_raw="")
        status, gaps, _reason = compute_offer_status(fields)
        self.assertEqual(status, "needs_input")
        self.assertIn("quantity", {gap["field"] for gap in gaps})

    def test_absent_quantity_ok_under_price_only(self):
        fields = full_fields(quantity=None, quantity_raw="")
        status, gaps, _reason = compute_offer_status(
            fields, document_policy="price_only"
        )
        self.assertEqual(status, "ok")
        self.assertNotIn("quantity", {gap["field"] for gap in gaps})

    def test_merge_respects_policy(self):
        local = {"message": "Phone Z x3 450", "price": "450",
                 "currency": "USD", "supplier": "Acme", "model": "Z"}
        stock = merge_interpreted_cells(local, {"quantity": "3"})
        self.assertEqual(stock["quantity"], "3")
        priced = merge_interpreted_cells(
            local, {"quantity": "3"}, document_policy="price_only"
        )
        self.assertNotIn("quantity", priced)
