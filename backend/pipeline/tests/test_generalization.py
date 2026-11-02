"""Held-out layout generalization: unfamiliar formats, no tailored parser.

Every fixture below is deliberately unlike the phone-chat samples
used during development: inline labeled fields, semicolon
separators, multi-line products, non-phone categories, row-ten
headers. None of these texts appear in example prompts or supplier
templates — the generic typed operations must carry them.
"""

import os
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from common.meta import MetaProvider
from organizations.models import AIProviderConfig, Membership, Organization
from pipeline.models import ImportRun, ReviewStatus
from pipeline.review import RunStatus, answer_request
from pipeline.tests.test_parsing import (
    HEADERS,
    force_fixture_provider,
    make_multi_workbook,
)

User = get_user_model()

# Inline labeled fields with pipe/paren separators, tablets and an
# accessory — not a phone chat export. Pipes and parens are used on
# purpose: commas and uniformly repeated semicolons route to the
# table lane by design, while chat pastes mix other separators.
INLINE_TEXT = (
    "TechBase BV weekly deals\n"
    "Prices in EUR updated 03 Nov\n"
    "Model: Tab A9 64GB Graphite | qty 12 | 189\n"
    "Tab S9 256GB Beige | qty 4 | 799\n"
    "USB-C Charger 65W (qty 40) - 19\n"
)

# One product spread over three lines; the price sits on the last.
MULTILINE_TEXT = (
    "Northstar Devices\n"
    "Phone 18 Pro\n"
    "256GB Graphite\n"
    "417 USD\n"
    "Phone 15\n"
    "128GB Black\n"
    "505 USD\n"
)

# Two grade sections sharing a model name; corrections must not leak
# across the section boundary.
SECTIONED_TEXT = (
    "Supplier Z lists\n"
    "P1+ - Like New\n"
    "Phone A 128GB - 300\n"
    "Phone B 128GB - 320\n"
    "P2+ - Grade B\n"
    "Phone A 128GB - 250\n"
    "Phone C 64GB - 180\n"
)

# "1.234" is ambiguous (US twelve-hundred vs EU one-point-two): the
# draft must leave the price unknown, never guess it.
AMBIGUOUS_TEXT = "Acme lists\nPhone Z 128GB - 1.234\n"

EQUIVALENCE_TEXT = "Acme lines\nPhone 15 128GB - 450\nPhone 14 64GB - 320\n"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class GeneralizationTests(TestCase):
    def setUp(self):
        force_fixture_provider(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.alice = User.objects.create_user(
            username="alice", email="alice@example.test", password="x"
        )
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.alice, organization=self.org, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def _paste(self, text):
        return self.client.post(
            "/api/pipeline/previews/from-text/",
            {"organization_id": self.org.id, "text": text},
            format="json",
        )

    def _apply(self, key, operations, revision=None):
        body = {"operations": operations}
        if revision is not None:
            body["revision"] = revision
        return self.client.post(
            f"/api/pipeline/previews/{key}/assist/apply/",
            body,
            format="json",
        )

    def _confirm(self, key, revision=None):
        body = {"mapping": {}}
        if revision is not None:
            body["revision"] = revision
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/pipeline/previews/{key}/confirm/",
                body,
                format="json",
            )

    def _records(self, preview):
        return {
            record["id"]: record
            for record in preview["draft"]["records"]
        }

    def test_inline_labeled_semicolon_layout(self):
        """Labeled fields and semicolons need no special parser."""
        key = self._paste(INLINE_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "TechBase BV"},
                {"op": "exclude_lines", "lines": [1, 2], "reason": "title and date"},
                {"op": "terminal_price", "currency": "EUR"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        records = self._records(applied.json()["preview"])
        self.assertEqual(set(records), {"L3", "L4", "L5"})
        prices = {
            record_id: record["lines"] for record_id, record in records.items()
        }
        self.assertEqual(prices, {"L3": [3], "L4": [4], "L5": [5]})

        confirm = self._confirm(key)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        offers = {
            (offer.source_row.data.get("message") or "")[:24]: offer
            for offer in run.offers.all()
        }
        self.assertEqual(len(offers), 3)
        by_price = sorted(offer.unit_price for offer in offers.values())
        self.assertEqual(
            by_price, [Decimal("19"), Decimal("189"), Decimal("799")]
        )
        for offer in offers.values():
            self.assertEqual(offer.currency, "EUR")
            self.assertEqual(offer.supplier, "TechBase BV")
            # Marked quantities ("qty 12") are text, not cells: with no
            # model reading them, they stay unknown instead of guessed.
            self.assertIsNone(offer.quantity)

    def test_multiline_join_reads_terminal_on_last_line(self):
        """Joined lines form one product; the price binds to its end."""
        key = self._paste(MULTILINE_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Northstar Devices"},
                {"op": "exclude_lines", "lines": [1], "reason": "title"},
                {"op": "join_lines", "lines": [2, 3, 4]},
                {"op": "join_lines", "lines": [5, 6, 7]},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        records = self._records(applied.json()["preview"])
        self.assertEqual(set(records), {"L2-L4", "L5-L7"})

        confirm = self._confirm(key)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        prices = sorted(
            offer.unit_price for offer in run.offers.all()
        )
        self.assertEqual(prices, [Decimal("417"), Decimal("505")])
        for offer in run.offers.all():
            self.assertEqual(offer.currency, "USD")

    def test_section_correction_stays_scoped(self):
        """A fix in one section leaves the other section untouched."""
        key = self._paste(SECTIONED_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Supplier Z"},
                {"op": "exclude_lines", "lines": [1], "reason": "title"},
                {"op": "section_value", "header_line": 2, "field": "grade", "value": "P1+"},
                {"op": "section_value", "header_line": 5, "field": "grade", "value": "P2+"},
                {"op": "correct", "record": "L3", "field": "color", "value": "Black"},
                {"op": "correct", "record": "L7", "field": "model", "value": "Phone C Mini"},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        records = self._records(applied.json()["preview"])
        self.assertEqual(set(records), {"L3", "L4", "L6", "L7"})
        grades = {
            record_id: record["fields"]["grade"]["value"]
            for record_id, record in records.items()
        }
        self.assertEqual(
            grades, {"L3": "P1+", "L4": "P1+", "L6": "P2+", "L7": "P2+"}
        )
        # The L3 correction did not leak into its section sibling.
        self.assertEqual(records["L3"]["fields"]["color"]["value"], "Black")
        self.assertEqual(records["L4"]["fields"]["color"]["state"], "unknown")
        self.assertEqual(records["L7"]["fields"]["model"]["value"], "Phone C Mini")
        self.assertEqual(records["L6"]["fields"]["model"]["state"], "unknown")

        # A later batch preserves the earlier manual edits.
        revision = applied.json()["preview"]["draft"]["revision"]
        second = self._apply(
            key,
            [{"op": "global", "field": "brand", "value": "Pear"}],
            revision=revision,
        )
        self.assertEqual(second.status_code, 200, second.content)
        records = self._records(second.json()["preview"])
        self.assertEqual(records["L3"]["fields"]["color"]["value"], "Black")
        self.assertEqual(records["L7"]["fields"]["model"]["value"], "Phone C Mini")
        self.assertEqual(records["L6"]["fields"]["brand"]["value"], "Pear")

    def test_ambiguous_terminal_stays_unknown_until_answered(self):
        """An ambiguous final number asks; it never becomes a guess."""
        key = self._paste(AMBIGUOUS_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Acme"},
                {"op": "exclude_lines", "lines": [1], "reason": "title"},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        sample = applied.json()["preview"]["samples"]["rows"][0]
        self.assertNotIn("price", sample["cells"])

        confirm = self._confirm(key)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        self.assertEqual(run.status, RunStatus.NEEDS_INPUT)
        offer = run.offers.get()
        self.assertIsNone(offer.unit_price)
        fields = set(
            run.reviews.filter(status=ReviewStatus.OPEN).values_list(
                "field", flat=True
            )
        )
        self.assertIn("unit_price", fields)

        answers = {
            "model_name": "Phone Z", "capacity_gb": "128", "grade": "A",
            "quantity": "2", "unit_price": "1234.56",
        }
        for review in run.reviews.filter(status=ReviewStatus.OPEN):
            answer_request(
                review=review, user=self.alice, value=answers[review.field]
            )
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.READY_FOR_REVIEW)
        self.assertTrue(all(item.passed for item in run.validations.all()))
        offer.refresh_from_db()
        self.assertEqual(offer.unit_price, Decimal("1234.56"))

    def test_txt_upload_matches_paste(self):
        """Uploading chat text equals pasting it: same candidates."""
        upload = self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org.id,
                "file": SimpleUploadedFile(
                    "chat.txt", EQUIVALENCE_TEXT.encode(), "text/plain"
                ),
            },
            format="multipart",
        )
        self.assertEqual(upload.status_code, 200, upload.content)
        self.assertTrue(upload.data["message_candidate"])
        via_upload = self.client.post(
            "/api/pipeline/previews/from-text/",
            {"organization_id": self.org.id, "text": upload.data["text"]},
            format="json",
        )
        via_paste = self._paste(EQUIVALENCE_TEXT)
        self.assertEqual(via_upload.status_code, 201, via_upload.content)
        self.assertEqual(via_paste.status_code, 201, via_paste.content)
        uploaded = [
            (record["id"], record["lines"], record["class"])
            for record in via_upload.data["draft"]["records"]
        ]
        pasted = [
            (record["id"], record["lines"], record["class"])
            for record in via_paste.data["draft"]["records"]
        ]
        self.assertEqual(uploaded, pasted)
        self.assertEqual(len(uploaded), 3)

    def test_row_ten_headers_and_second_sheet_survive(self):
        """Headers at row 10 read; the unselected sheet is disclosed."""
        junk = [[f"banner line {n}"] for n in range(1, 10)]
        gadgets = junk + [
            HEADERS,
            ["Acme", "Pear", "P1", "128GB", "Black", "A", "Unlocked", 5, 299.99, "USD", "ok"],
            ["Acme", "Pear", "P2", "64GB", "White", "B", "Unlocked", 2, 149.50, "USD", ""],
        ]
        extras = [
            HEADERS,
            ["TabCo", "Tab", "T1", "256GB", "Gray", "A", "Unlocked", 1, 799, "EUR", ""],
        ]
        content = make_multi_workbook(
            [("Gadgets", gadgets, True), ("Extras", extras, True)]
        )
        upload = self.client.post(
            "/api/pipeline/previews/",
            {
                "organization_id": self.org.id,
                "file": SimpleUploadedFile(
                    "row10.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201, upload.content)
        self.assertEqual(upload.data["sheets"]["selected"], "Gadgets")
        self.assertEqual(upload.data["sheets"]["omitted"], ["Extras"])
        rows = upload.data["samples"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["cells"]["model"], "P1")
        self.assertEqual(rows[1]["cells"]["model"], "P2")

        switched = self.client.post(
            f"/api/pipeline/previews/{upload.data['key']}/render/",
            {"mapping": {}, "sheet": "Extras"},
            format="json",
        )
        self.assertEqual(switched.status_code, 200, switched.content)
        self.assertEqual(switched.data["sheets"]["omitted"], ["Gadgets"])
        self.assertEqual(
            switched.data["samples"]["rows"][0]["cells"]["model"], "T1"
        )

    def test_reads_never_mutate_confirmed_rules(self):
        """Eight chat turns after applying leave the draft untouched."""
        AIProviderConfig.objects.create(model="admin-model")
        key = self._paste(SECTIONED_TEXT).data["key"]
        applied = self._apply(
            key,
            [
                {"op": "global", "field": "supplier", "value": "Supplier Z"},
                {"op": "exclude_lines", "lines": [1], "reason": "title"},
                {"op": "classify", "lines": [2, 5], "class": "header"},
                {"op": "terminal_price", "currency": "USD"},
            ],
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        before = applied.json()["preview"]["draft"]
        self.assertEqual(before["revision"], 2)

        def _quiet(payload, on_chunk=None):
            on_chunk(1, 1, 20, 8)
            return {
                "message": "Noted.",
                "set_constants": {},
                "drop_lines": [],
                "operations": [],
                "usage": {"total_tokens": 28},
            }

        paid = mock.patch.dict(
            os.environ,
            {
                "OFFERFLOW_PROVIDER": "meta",
                "OFFERFLOW_ALLOW_PAID": "true",
                "OFFERFLOW_META_API_KEY": "LLM_9_envkey",
                "OFFERFLOW_META_MODEL": "env-model",
            },
            clear=False,
        )
        with paid, mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_quiet
        ):
            for turn in range(8):
                response = self.client.post(
                    f"/api/pipeline/previews/{key}/assist/",
                    {"messages": [{"role": "user", "content": f"note {turn}"}]},
                    format="json",
                )
                self.assertEqual(response.status_code, 200, response.content)
        render = self.client.post(
            f"/api/pipeline/previews/{key}/render/",
            {"mapping": {}},
            format="json",
        )
        self.assertEqual(render.status_code, 200, render.content)
        # Render echoes no draft, so confirm the rules survived by
        # importing: the terminal price must still bind.
        confirm = self._confirm(key, revision=2)
        self.assertEqual(confirm.status_code, 201, confirm.content)
        run = ImportRun.objects.get(pk=confirm.data["id"])
        prices = sorted(offer.unit_price for offer in run.offers.all())
        self.assertEqual(
            prices,
            [Decimal("180"), Decimal("250"), Decimal("300"), Decimal("320")],
            "confirmed rules must survive long conversations",
        )
