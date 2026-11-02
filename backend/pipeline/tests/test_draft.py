"""Draft engine: typed rules over immutable source lines."""

from decimal import Decimal

from django.test import SimpleTestCase

from pipeline.draft import (
    DOCUMENT_POLICIES,
    DraftConflict,
    DraftError,
    apply_operations,
    derive_records,
    terminal_amount,
)

LINES = [
    "Northstar Devices",
    "Available from 02 Nov",
    "",
    "P1+ - Like New",
    "> Phone 18 Pro 256GB - 417",
    "> Phone 15 128GB - 505",
    "",
    "P2+ - Grade B",
    "> Phone 14 128GB - 299",
]


class TerminalAmountTests(SimpleTestCase):
    def test_takes_final_number(self):
        self.assertEqual(terminal_amount("> Phone 18 Pro 256GB - 417"), Decimal("417"))

    def test_european_format(self):
        self.assertEqual(terminal_amount("Phone Z - 1.234,56"), Decimal("1234.56"))

    def test_us_format(self):
        self.assertEqual(terminal_amount("Phone Z - 1,234.56"), Decimal("1234.56"))

    def test_ambiguous_refuses(self):
        self.assertIsNone(terminal_amount("Phone Z - 1.234"))

    def test_us_thousands_reads(self):
        self.assertEqual(terminal_amount("Phone Z - 1,234"), Decimal("1234"))

    def test_no_number_refuses(self):
        self.assertIsNone(terminal_amount("P1+ - Like New"))


class DeriveRecordsTests(SimpleTestCase):
    def test_plain_lines_become_unknown_candidates(self):
        records, unresolved, sections = derive_records(LINES, [])
        self.assertEqual(len(records), 7)
        self.assertEqual(records[0]["id"], "L1")
        self.assertEqual(records[0]["lines"], [1])
        self.assertEqual(records[0]["fields"]["supplier"]["state"], "unknown")
        self.assertTrue(
            any(
                issue["scope"] == "document" and issue["field"] == "supplier"
                for issue in unresolved
            )
        )
        self.assertEqual(sections, [])

    def test_section_value_scopes_to_covered_lines(self):
        rules = [
            {
                "id": "r1",
                "kind": "section_value",
                "header_line": 4,
                "header_text": "P1+ - Like New",
                "field": "grade",
                "value": "P1+",
            },
            {
                "id": "r2",
                "kind": "section_value",
                "header_line": 8,
                "header_text": "P2+ - Grade B",
                "field": "grade",
                "value": "B",
            },
        ]
        records, _unresolved, sections = derive_records(LINES, rules)
        by_id = {record["id"]: record for record in records}
        # Section headers are not products.
        self.assertNotIn("L4", by_id)
        self.assertNotIn("L8", by_id)
        self.assertEqual(by_id["L5"]["fields"]["grade"]["value"], "P1+")
        self.assertEqual(
            by_id["L5"]["fields"]["grade"]["state"], "user_confirmed"
        )
        self.assertEqual(by_id["L9"]["fields"]["grade"]["value"], "B")
        # Outside any section: still unknown.
        self.assertEqual(by_id["L1"]["fields"]["grade"]["state"], "unknown")
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["records"], ["L5", "L6"])

    def test_join_builds_multiline_record(self):
        multi = ["Supplier: Bluebird", "Device: Phone 15 Pro", "Storage: 256GB"]
        rules = [
            {"id": "r1", "kind": "join_lines", "lines": [2, 3]},
            {"id": "r2", "kind": "classify", "lines": [1], "class": "header"},
        ]
        records, _unresolved, _sections = derive_records(multi, rules)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "L2-L3")
        self.assertEqual(records[0]["lines"], [2, 3])

    def test_excluded_lines_leave_no_records(self):
        rules = [{"id": "r1", "kind": "exclude_lines", "lines": [1, 2]}]
        records, _unresolved, _sections = derive_records(LINES, rules)
        ids = [record["id"] for record in records]
        self.assertNotIn("L1", ids)
        self.assertNotIn("L2", ids)
        self.assertIn("L5", ids)

    def test_correct_beats_global(self):
        rules = [
            {"id": "r1", "kind": "global", "field": "grade", "value": "A"},
            {
                "id": "r2",
                "kind": "correct",
                "record": "L5",
                "field": "grade",
                "value": "P1+",
            },
        ]
        records, _unresolved, _sections = derive_records(LINES, rules)
        by_id = {record["id"]: record for record in records}
        self.assertEqual(by_id["L5"]["fields"]["grade"]["value"], "P1+")
        self.assertEqual(by_id["L6"]["fields"]["grade"]["value"], "A")


class ApplyOperationsTests(SimpleTestCase):
    def test_unknown_op_rejected(self):
        with self.assertRaises(DraftError) as raised:
            apply_operations(
                [{"op": "teleport", "lines": [1]}],
                lines=LINES,
                rules=[],
                revision_actual=1,
                revision_expected=1,
            )
        self.assertIn("teleport", str(raised.exception))

    def test_unknown_op_field_rejected(self):
        with self.assertRaises(DraftError) as raised:
            apply_operations(
                [
                    {
                        "op": "global",
                        "field": "supplier",
                        "value": "Acme",
                        "interpretation_instructions": "be nice",
                    }
                ],
                lines=LINES,
                rules=[],
                revision_actual=1,
                revision_expected=1,
            )
        self.assertIn("interpretation_instructions", str(raised.exception))

    def test_stale_revision_conflicts(self):
        with self.assertRaises(DraftConflict):
            apply_operations(
                [{"op": "global", "field": "supplier", "value": "Acme"}],
                lines=LINES,
                rules=[],
                revision_actual=3,
                revision_expected=1,
            )

    def test_line_out_of_range_rejected(self):
        with self.assertRaises(DraftError):
            apply_operations(
                [{"op": "exclude_lines", "lines": [99]}],
                lines=LINES,
                rules=[],
                revision_actual=1,
                revision_expected=1,
            )

    def test_noncontiguous_join_rejected(self):
        with self.assertRaises(DraftError):
            apply_operations(
                [{"op": "join_lines", "lines": [5, 9]}],
                lines=LINES,
                rules=[],
                revision_actual=1,
                revision_expected=1,
            )

    def test_absent_quantity_needs_price_only(self):
        with self.assertRaises(DraftError) as raised:
            apply_operations(
                [{"op": "absent", "field": "quantity", "scope": "document"}],
                lines=LINES,
                rules=[],
                revision_actual=1,
                revision_expected=1,
                policy="stock",
            )
        self.assertIn("price_only", str(raised.exception))

    def test_policy_then_absent_applies_in_order(self):
        rules, policy = apply_operations(
            [
                {"op": "policy", "policy": "price_only"},
                {"op": "absent", "field": "quantity", "scope": "document"},
            ],
            lines=LINES,
            rules=[],
            revision_actual=1,
            revision_expected=1,
            policy="stock",
        )
        self.assertEqual(policy, "price_only")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["kind"], "absent")

    def test_same_scope_global_replaces(self):
        rules, _policy = apply_operations(
            [
                {"op": "global", "field": "supplier", "value": "Acme"},
                {"op": "global", "field": "supplier", "value": "Beta"},
            ],
            lines=LINES,
            rules=[],
            revision_actual=1,
            revision_expected=1,
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["value"], "Beta")

    def test_bad_currency_rejected(self):
        with self.assertRaises(DraftError):
            apply_operations(
                [{"op": "global", "field": "currency", "value": "US"}],
                lines=LINES,
                rules=[],
                revision_actual=1,
                revision_expected=1,
            )

    def test_policies_known(self):
        self.assertEqual(tuple(DOCUMENT_POLICIES), ("stock", "price_only"))


class TerminalDelegationTests(SimpleTestCase):
    def test_currency_suffix_is_evidence(self):
        self.assertEqual(terminal_amount("Phone Z - 417 USD"), Decimal("417"))

    def test_glued_letters_never_terminal(self):
        self.assertIsNone(terminal_amount("SKU123"))
        self.assertIsNone(terminal_amount("iPhone 8 64GB"))

    def test_capacity_token_keeps_its_role(self):
        self.assertEqual(
            terminal_amount("> Phone 16 Pro 128GB - 349"), Decimal("349")
        )


class IdBlindDedupeTests(SimpleTestCase):
    def test_reapplied_identical_op_stores_once(self):
        op = {"op": "exclude_lines", "lines": [1]}
        rules, _policy = apply_operations(
            [op], lines=LINES, rules=[], revision_actual=1,
            revision_expected=1,
        )
        rules[0]["id"] = "r1"
        again, _policy = apply_operations(
            [op], lines=LINES, rules=rules, revision_actual=2,
            revision_expected=2,
        )
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["id"], "r1")


class ValidateOperationsTests(SimpleTestCase):
    def test_per_op_echo_and_policy_sequence(self):
        from pipeline.draft import validate_operations

        pending, policy, per_op = validate_operations(
            [
                {"op": "policy", "policy": "price_only"},
                {"op": "absent", "field": "quantity"},
                {
                    "op": "global", "field": "supplier",
                    "value": "Northstar",
                },
            ],
            lines=LINES,
            rules=[],
            policy="stock",
        )
        self.assertEqual(policy, "price_only")
        self.assertEqual(len(pending), 2)
        self.assertIsNone(per_op[0])
        self.assertEqual(per_op[1]["kind"], "absent")
        self.assertEqual(per_op[2]["value"], "Northstar")

    def test_failed_batch_validates_nothing(self):
        from pipeline.draft import validate_operations

        with self.assertRaises(DraftError):
            validate_operations(
                [
                    {"op": "global", "field": "supplier", "value": "Ok"},
                    {"op": "explode"},
                ],
                lines=LINES,
                rules=[],
                policy="stock",
            )


class DeriveRecordCellsTests(SimpleTestCase):
    def test_section_terminal_join_merge(self):
        from pipeline.draft import derive_record_cells

        rules, _policy = apply_operations(
            [
                {"op": "exclude_lines", "lines": [1, 2]},
                {"op": "classify", "lines": [4], "class": "header"},
                {"op": "classify", "lines": [8], "class": "header"},
                {
                    "op": "section_value", "header_line": 4,
                    "field": "grade", "value": "P1+ - Like New",
                },
                {
                    "op": "section_value", "header_line": 8,
                    "field": "grade", "value": "P2+ - Grade B",
                },
                {"op": "terminal_price", "currency": "USD"},
                {"op": "join_lines", "lines": [5, 6]},
            ],
            lines=LINES,
            rules=[],
            revision_actual=1,
            revision_expected=1,
        )
        derived = derive_record_cells(LINES, rules)
        self.assertEqual(
            [info["lines"] for info in derived.values()], [[5, 6], [9]]
        )
        joined = derived["L5-L6"]
        self.assertEqual(joined["cells"]["grade"], "P1+ - Like New")
        # The terminal of the joined record is line 6's final number.
        self.assertEqual(joined["cells"]["price"], "505")
        self.assertEqual(joined["cells"]["currency"], "USD")
        self.assertEqual(joined["terminal"]["line"], 6)
        token = LINES[5][
            joined["terminal"]["span"][0]:joined["terminal"]["span"][1]
        ]
        self.assertEqual(token, "505")
        single = derived["L9"]
        self.assertEqual(single["cells"]["grade"], "P2+ - Grade B")
        self.assertEqual(single["cells"]["price"], "299")

    def test_terminal_scope_lines(self):
        from pipeline.draft import terminal_scope_lines

        rules, _policy = apply_operations(
            [
                {
                    "op": "section_value", "header_line": 4,
                    "field": "grade", "value": "P1+",
                },
                {
                    "op": "section_value", "header_line": 8,
                    "field": "grade", "value": "P2+",
                },
                {
                    "op": "terminal_price", "currency": "USD",
                    "section_header_line": 4,
                },
            ],
            lines=LINES,
            rules=[],
            revision_actual=1,
            revision_expected=1,
        )
        self.assertEqual(terminal_scope_lines(LINES, rules), {5, 6})


class DropRuleTests(SimpleTestCase):
    RULES = [
        {"id": "r1", "kind": "global", "field": "supplier", "value": "Acme"},
        {
            "id": "r2", "kind": "section_value", "header_line": 4,
            "header_text": "P1+ - Like New", "field": "grade", "value": "P1+",
        },
    ]

    def test_drop_removes_only_that_rule(self):
        rules, _policy = apply_operations(
            [{"op": "drop_rule", "rule": "r2"}],
            lines=LINES,
            rules=[dict(rule) for rule in self.RULES],
            revision_actual=1,
            revision_expected=1,
        )
        self.assertEqual([rule["id"] for rule in rules], ["r1"])
        records, _unresolved, _sections = derive_records(LINES, rules)
        by_id = {record["id"]: record for record in records}
        self.assertEqual(by_id["L5"]["fields"]["grade"]["state"], "unknown")

    def test_unknown_rule_id_rejected(self):
        with self.assertRaises(DraftError) as raised:
            apply_operations(
                [{"op": "drop_rule", "rule": "r9"}],
                lines=LINES,
                rules=[dict(rule) for rule in self.RULES],
                revision_actual=1,
                revision_expected=1,
            )
        self.assertIn("r9", str(raised.exception))

    def test_drop_twice_in_one_batch_rejected(self):
        with self.assertRaises(DraftError):
            apply_operations(
                [
                    {"op": "drop_rule", "rule": "r1"},
                    {"op": "drop_rule", "rule": "r1"},
                ],
                lines=LINES,
                rules=[dict(rule) for rule in self.RULES],
                revision_actual=1,
                revision_expected=1,
            )

    def test_drop_join_prunes_orphaned_corrections(self):
        rules, _policy = apply_operations(
            [
                {"op": "join_lines", "lines": [5, 6]},
                {"op": "correct", "record": "L5-L6", "field": "model", "value": "Phone 18 Pro"},
            ],
            lines=LINES,
            rules=[],
            revision_actual=1,
            revision_expected=1,
        )
        # The pure engine assigns no ids; tag them as apply would.
        for pos, rule in enumerate(rules, start=1):
            rule["id"] = f"r{pos}"
        join_id = next(
            rule["id"] for rule in rules if rule["kind"] == "join_lines"
        )
        pruned, _policy = apply_operations(
            [{"op": "drop_rule", "rule": join_id}],
            lines=LINES,
            rules=rules,
            revision_actual=2,
            revision_expected=2,
        )
        self.assertFalse([rule for rule in pruned if rule["kind"] == "correct"])
