"""Tests for token budgets and the paid provider (mocked transport only).

No test here touches the network: the model client is always a fake. Live
calls require explicit owner activation with an approved allowance.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from openai import RateLimitError

from common.meta import MetaConfig, MetaProvider, ProposalUnusable, ProviderError
from organizations.models import Membership, Organization
from pipeline.models import OperatorSpend, SpendAllowance
from pipeline.spending import (
    BudgetRefused,
    get_allowance,
    get_operator_spend,
    reconcile,
    release,
    reserve,
)

User = get_user_model()

FIELDS = {
    "supplier": "Acme",
    "brand": "Pear",
    "model": "P1",
    "capacity": "128GB",
    "color": "Black",
    "grade": "A",
    "lock": "Unlocked",
    "quantity": "5",
    "price": "299.99",
    "currency": "USD",
    "conditions": "",
}


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content, usage):
        message = mock.Mock()
        message.content = content
        choice = mock.Mock()
        choice.message = message
        self.choices = [choice]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.script, Exception):
            raise self.script
        return self.script


class _FakeClient:
    def __init__(self, script):
        self.chat = mock.Mock()
        self.chat.completions = _FakeCompletions(script)


class _EchoCompletions:
    """Answer every call by echoing its row indexes with canned fields."""

    def __init__(self, prompt_tokens=10, completion_tokens=5, fail_on=()):
        self.calls = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.fail_on = set(fail_on)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) in self.fail_on:
            raise _sdk_error(RateLimitError, "slow down")
        user_content = kwargs["messages"][1]["content"]
        indexes = [row["index"] for row in json.loads(user_content)["rows"]]
        body = json.dumps(
            {
                "rows": [
                    {"index": index, "fields": dict(FIELDS)} for index in indexes
                ]
            }
        )
        return _FakeResponse(
            body, _FakeUsage(self.prompt_tokens, self.completion_tokens)
        )


def _sdk_error(cls, message):
    """Build an SDK error across constructor spellings (pinned SDK)."""
    try:
        return cls(message, response=mock.Mock(), body=None)
    except TypeError:
        pass
    try:
        return cls(message, request=mock.Mock())
    except TypeError:
        return cls(message)


def _one_row_cap(rows) -> int:
    """Input cap that fits system prompt, schema, plus exactly one row.

    Derived from the live prompt and schema so wording can evolve
    without silently changing what the chunking tests exercise: every
    row still travels alone, and any second row still forces a new
    chunk.
    """
    from common.meta import RESPONSE_SCHEMA, SYSTEM_PROMPT
    from pipeline.spending import estimate_schema_tokens, estimate_text_tokens

    system = estimate_text_tokens(SYSTEM_PROMPT)
    schema = estimate_schema_tokens(RESPONSE_SCHEMA)
    widest = max(
        estimate_text_tokens(json.dumps(row, separators=(",", ":"))) + 1
        for row in rows
    )
    cap = system + schema + widest + 5
    assert cap < system + schema + 2 * widest, "cap must force one row per chunk"
    return cap


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SpendingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")

    def allow(self, budget, **overrides):
        allowance = get_allowance(self.org.id)
        allowance.token_budget = budget
        allowance.monthly_credits = budget
        for key, value in overrides.items():
            setattr(allowance, key, value)
        allowance.save()
        operator = get_operator_spend()
        if budget > operator.monthly_ceiling:
            operator.monthly_ceiling = budget
            operator.save(update_fields=["monthly_ceiling"])
        return allowance

    def operator(self):
        return OperatorSpend.objects.get()

    def config(self, **overrides):
        options = {
            "organization_id": self.org.id,
            "api_key": "LLM_9_testsecret",
            "model": "test-model",
        }
        options.update(overrides)
        return MetaConfig(**options)

    def provider(self, script, **config_overrides):
        holder = {}

        def factory(config):
            holder["client"] = _FakeClient(script)
            return holder["client"]

        provider = MetaProvider(self.config(**config_overrides), client_factory=factory)
        return provider, holder

    def test_zero_budget_refuses(self):
        allowance = get_allowance(self.org.id)
        allowance.monthly_credits = 0
        allowance.save(update_fields=["monthly_credits"])
        self.assertEqual(allowance.token_budget, 0)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)

    def test_paused_refuses(self):
        self.allow(100000, paused=True)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)

    def test_over_budget_refuses(self):
        self.allow(50)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)

    def test_in_flight_limit(self):
        self.allow(100000)
        first = reserve(self.org.id, 100, 1)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)
        release(first)
        second = reserve(self.org.id, 100, 1)
        self.assertEqual(second.estimate_tokens, 100)

    def test_reconcile_math(self):
        self.allow(100000)
        reservation = reserve(self.org.id, 100, 1)
        allowance = reconcile(reservation, 60)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.tokens_spent, 60)
        self.assertEqual(allowance.in_flight, 0)

    def test_meta_success_path(self):
        self.allow(100000)
        body = json.dumps({"rows": [{"index": 2, "fields": dict(FIELDS)}]})
        script = _FakeResponse(body, _FakeUsage(120, 80))
        provider, holder = self.provider(script)
        result = provider.interpret_table({"rows": [{"index": 2, "data": {"a": 1}}]})
        self.assertFalse(result["simulated"])
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["usage"]["total_tokens"], 200)
        self.assertEqual(result["rows"][0]["fields"]["grade"], "A")
        call = holder["client"].chat.completions.calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["response_format"]["type"], "json_schema")
        self.assertEqual(call["max_completion_tokens"], 2000)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 200)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def test_meta_auth_error_releases_without_spend(self):
        from openai import AuthenticationError

        self.allow(100000)
        provider, _holder = self.provider(_sdk_error(AuthenticationError, "bad key"))
        with self.assertRaises(ProviderError):
            provider.interpret_table({"rows": [{"index": 0, "data": {"a": 1}}]})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 0)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def test_meta_rate_limit_not_retried(self):
        self.allow(100000)
        provider, holder = self.provider(_sdk_error(RateLimitError, "slow down"))
        with self.assertRaises(ProviderError):
            provider.interpret_table({"rows": [{"index": 0, "data": {"a": 1}}]})
        self.assertEqual(len(holder["client"].chat.completions.calls), 1)

    def test_input_cap_refuses_before_dispatch(self):
        self.allow(100000)
        provider, holder = self.provider(
            _FakeResponse("{}", _FakeUsage(1, 1)), max_input_tokens=10
        )
        big = {"rows": [{"index": 1, "data": {"blob": "x" * 10000}}]}
        with self.assertRaises(ProviderError):
            provider.interpret_table(big)
        self.assertEqual(holder, {})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_reserved, 0)

    def test_malformed_model_output_fails_but_pays(self):
        self.allow(100000)
        script = _FakeResponse("not json", _FakeUsage(10, 5))
        provider, _holder = self.provider(script)
        with self.assertRaises(ProviderError):
            provider.interpret_table({"rows": [{"index": 0, "data": {"a": 1}}]})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 15)

    def test_missing_usage_reconciles_estimate(self):
        self.allow(100000)
        body = json.dumps({"rows": []})
        script = _FakeResponse(body, None)
        provider, _holder = self.provider(script)
        result = provider.interpret_table({"rows": [{"index": 0, "data": {"a": 1}}]})
        self.assertEqual(result["rows"], [])
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertGreater(allowance.tokens_spent, 0)

    def test_empty_rows_skip_dispatch(self):
        self.allow(100000)
        body = json.dumps({"rows": [{"index": 0, "fields": dict(FIELDS)}]})
        provider, holder = self.provider(_FakeResponse(body, _FakeUsage(1, 1)))
        result = provider.interpret_table({"rows": []})
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["chunk_usages"], [])
        self.assertEqual(holder, {})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 0)
        self.assertEqual(allowance.tokens_reserved, 0)

    def test_rows_split_into_bounded_calls(self):
        from common.meta import MAX_ROWS_PER_CALL

        self.allow(100000)
        completions = _EchoCompletions(prompt_tokens=10, completion_tokens=5)
        client = mock.Mock()
        client.chat.completions = completions
        provider = MetaProvider(
            self.config(), client_factory=lambda config: client
        )
        rows = [{"index": i, "data": {"a": i}} for i in range(MAX_ROWS_PER_CALL + 8)]
        result = provider.interpret_table({"rows": rows})
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(len(result["rows"]), MAX_ROWS_PER_CALL + 8)
        self.assertEqual(
            [item["index"] for item in result["rows"]], list(range(MAX_ROWS_PER_CALL + 8))
        )
        self.assertEqual(len(result["chunk_usages"]), 2)
        self.assertEqual(result["usage"]["total_tokens"], 30)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 30)
        self.assertEqual(allowance.period_spent, 30)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def test_chunk_callback_reports_progress(self):
        self.allow(100000)
        completions = _EchoCompletions(prompt_tokens=10, completion_tokens=5)
        client = mock.Mock()
        client.chat.completions = completions
        rows = [{"index": i, "data": {"blob": "x" * 250}} for i in range(3)]
        provider = MetaProvider(
            self.config(max_input_tokens=_one_row_cap(rows)),
            client_factory=lambda config: client,
        )
        seen = []
        provider.interpret_table({"rows": rows}, on_chunk=lambda *a: seen.append(a))
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0], (1, 3, 10, 5))
        self.assertEqual(seen[2], (3, 3, 10, 5))

    def test_input_cap_splits_calls(self):
        self.allow(100000)
        completions = _EchoCompletions()
        client = mock.Mock()
        client.chat.completions = completions
        rows = [
            {"index": i, "data": {"blob": "x" * 250}} for i in range(3)
        ]
        provider = MetaProvider(
            self.config(max_input_tokens=_one_row_cap(rows)),
            client_factory=lambda config: client,
        )
        result = provider.interpret_table({"rows": rows})
        self.assertGreater(len(completions.calls), 1)
        self.assertEqual(len(result["rows"]), 3)

    def test_message_context_travels_and_counts(self):
        self.allow(100000)
        completions = _EchoCompletions()
        client = mock.Mock()
        client.chat.completions = completions
        rows = [{"index": 0, "data": {"message": "P1 x2 10"}}]
        provider = MetaProvider(
            self.config(max_input_tokens=_one_row_cap(rows) + 500),
            client_factory=lambda config: client,
        )
        provider.interpret_table({"rows": rows, "context": "Mercantile Pacific"})
        sent = json.loads(completions.calls[0]["messages"][1]["content"])
        self.assertEqual(sent["context"], "Mercantile Pacific")
        self.assertEqual(len(sent["rows"]), 1)
        # Context consumes the same budget: an over-cap context refuses
        # before any dispatch.
        with self.assertRaisesRegex(ProviderError, "exceeds the input cap"):
            provider.interpret_table({"rows": rows, "context": "x" * 100000})
        self.assertEqual(len(completions.calls), 1)
        with self.assertRaisesRegex(ProviderError, "Unusable payload"):
            provider.interpret_table({"rows": rows, "context": 123})

    def test_single_row_over_cap_refuses(self):
        self.allow(100000)
        completions = _EchoCompletions()
        client = mock.Mock()
        client.chat.completions = completions
        provider = MetaProvider(
            self.config(max_input_tokens=50),
            client_factory=lambda config: client,
        )
        with self.assertRaisesRegex(ProviderError, "single row"):
            provider.interpret_table(
                {"rows": [{"index": 0, "data": {"blob": "x" * 10000}}]}
            )
        self.assertEqual(completions.calls, [])
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 0)

    def test_mid_batch_failure_keeps_prior_spend(self):
        self.allow(100000)
        completions = _EchoCompletions(
            prompt_tokens=10, completion_tokens=5, fail_on=(2,)
        )
        client = mock.Mock()
        client.chat.completions = completions
        rows = [
            {"index": i, "data": {"blob": "x" * 250}} for i in range(3)
        ]
        provider = MetaProvider(
            self.config(max_input_tokens=_one_row_cap(rows)),
            client_factory=lambda config: client,
        )
        with self.assertRaisesRegex(ProviderError, "chunk 2/"):
            provider.interpret_table({"rows": rows})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 15)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def _status_error(self, body):
        from openai import APIStatusError

        response = mock.Mock()
        response.status_code = 400
        response.text = body
        response.headers = {}
        return APIStatusError("400 bad request", response=response, body=None)

    def test_status_error_keeps_provider_message_only(self):
        self.allow(100000)
        provider, _holder = self.provider(
            self._status_error(
                json.dumps(
                    {
                        "error": {
                            "message": "strict schema: rows.0 missing required key",
                            "code": "bad_schema",
                            "echo": "x" * 500,
                        }
                    }
                )
            )
        )
        with self.assertRaises(ProviderError) as raised:
            provider.interpret_table(
                {"rows": [{"index": 1, "data": {"message": "Phone 1 - 5"}}]}
            )
        message = str(raised.exception)
        self.assertIn("(400)", message)
        self.assertIn("strict schema: rows.0 missing required key", message)
        self.assertNotIn("bad_schema", message)
        self.assertNotIn("x" * 100, message)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 0)
        self.assertEqual(allowance.tokens_reserved, 0)

    def test_status_error_truncates_plain_text_body(self):
        from common.meta import ERROR_BODY_CHARS

        self.allow(100000)
        provider, _holder = self.provider(
            self._status_error("plain failure: " + "y" * 500)
        )
        with self.assertRaises(ProviderError) as raised:
            provider.interpret_table(
                {"rows": [{"index": 1, "data": {"message": "Phone 1 - 5"}}]}
            )
        message = str(raised.exception)
        detail = message.split("Detail: ", 1)[1]
        self.assertLessEqual(len(detail), ERROR_BODY_CHARS + 1)
        self.assertTrue(detail.endswith("…"))
        self.assertNotIn("y" * 400, message)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReservationLifecycleTests(SpendingTests):
    """R1: identity, idempotent settlement, uncertain spend, estimates."""

    def test_double_reconcile_books_once(self):
        from pipeline.models import SpendReservation

        self.allow(100000)
        reservation = reserve(self.org.id, 100, 1)
        reconcile(reservation, 60)
        reconcile(reservation, 60)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 60)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)
        row = SpendReservation.objects.get(pk=reservation.id)
        self.assertEqual(row.state, SpendReservation.State.RECONCILED)
        self.assertEqual(row.actual_tokens, 60)

    def test_double_release_frees_once(self):
        self.allow(100000)
        reservation = reserve(self.org.id, 100, 2)
        release(reservation)
        release(reservation)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def test_release_after_reconcile_is_noop(self):
        self.allow(100000)
        reservation = reserve(self.org.id, 100, 1)
        reconcile(reservation, 60)
        release(reservation)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 60)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def test_timeout_holds_uncertain_not_free(self):
        from openai import APITimeoutError

        from pipeline.models import SpendReservation

        self.allow(100000)
        provider, _holder = self.provider(_sdk_error(APITimeoutError, "slow"))
        with self.assertRaises(ProviderError):
            provider.interpret_table({"rows": [{"index": 0, "data": {"a": 1}}]})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 0)
        self.assertGreater(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 1)
        row = SpendReservation.objects.get(organization=self.org)
        self.assertEqual(row.state, SpendReservation.State.UNCERTAIN)
        # A second dispatch cannot take the held slot: no extra spend.
        with self.assertRaises(ProviderError):
            provider.interpret_table({"rows": [{"index": 1, "data": {"a": 1}}]})

    def test_late_usage_reconciles_uncertain(self):
        from pipeline.spending import mark_uncertain

        self.allow(100000)
        reservation = reserve(self.org.id, 100, 2)
        mark_uncertain(reservation, "timeout")
        mark_uncertain(reservation, "timeout")
        reconcile(reservation, 40)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 40)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)

    def test_sweep_books_expired_uncertain_as_spend(self):
        from datetime import timedelta

        from django.utils import timezone

        from pipeline.models import SpendReservation, UsageEvent
        from pipeline.spending import mark_uncertain, sweep_expired

        self.allow(100000)
        reservation = reserve(self.org.id, 100, 2)
        mark_uncertain(reservation, "timeout")
        SpendReservation.objects.filter(pk=reservation.id).update(
            expires_at=timezone.now() - timedelta(days=2)
        )
        swept = sweep_expired(organization_id=self.org.id)
        self.assertEqual(swept["expired"], 1)
        self.assertEqual(swept["tokens_booked"], 100)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 100)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)
        event = UsageEvent.objects.get(
            organization=self.org, kind="provider_uncertain"
        )
        self.assertEqual(event.quantity, 100)
        self.assertEqual(event.detail["reservation_id"], reservation.id)
        # Re-sweeping settles nothing twice.
        swept = sweep_expired(organization_id=self.org.id)
        self.assertEqual(swept["expired"], 0)
        allowance.refresh_from_db()
        self.assertEqual(allowance.tokens_spent, 100)

    def test_sweep_converts_dead_hold_to_uncertain(self):
        from datetime import timedelta

        from django.utils import timezone

        from pipeline.models import SpendReservation
        from pipeline.spending import sweep_expired

        self.allow(100000)
        reservation = reserve(self.org.id, 100, 2)
        SpendReservation.objects.filter(pk=reservation.id).update(
            expires_at=timezone.now() - timedelta(seconds=5)
        )
        swept = sweep_expired(organization_id=self.org.id)
        self.assertEqual(swept["uncertain"], 1)
        row = SpendReservation.objects.get(pk=reservation.id)
        self.assertEqual(row.state, SpendReservation.State.UNCERTAIN)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 0)
        self.assertEqual(allowance.tokens_reserved, 100)
        self.assertEqual(allowance.in_flight, 1)

    def test_sweep_heals_phantom_counters_only(self):
        from pipeline.spending import sweep_expired

        self.allow(100000)
        reservation = reserve(self.org.id, 100, 2)
        SpendAllowance.objects.filter(organization_id=self.org.id).update(
            tokens_reserved=9999, in_flight=9
        )
        sweep_expired(organization_id=self.org.id)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_reserved, 100)
        self.assertEqual(allowance.in_flight, 1)
        release(reservation)

    def test_estimates_bound_and_cover_output(self):
        from pipeline.spending import (
            REQUEST_OVERHEAD_TOKENS,
            estimate_request_tokens,
            estimate_text_tokens,
        )

        self.assertEqual(estimate_text_tokens(""), 0)
        # ceil(chars/3): strictly above the old chars//4 rule.
        self.assertEqual(estimate_text_tokens("x" * 12), 4)
        self.assertEqual(estimate_text_tokens("x" * 13), 5)
        total = estimate_request_tokens(
            texts=["system", "user payload"], max_output_tokens=2000
        )
        self.assertEqual(
            total,
            estimate_text_tokens("system")
            + estimate_text_tokens("user payload")
            + 2000
            + REQUEST_OVERHEAD_TOKENS,
        )
        with self.assertRaises(ValueError):
            estimate_request_tokens(texts=["x"], max_output_tokens=-1)

    def test_estimate_bounds_non_latin_conservatively(self):
        from pipeline.spending import estimate_text_tokens

        # ASCII keeps the documented ceil(n/3) bound.
        self.assertEqual(estimate_text_tokens("x" * 12), 4)
        # Non-ASCII scripts (CJK, emoji, accented) commonly cost at
        # least one token per character under BPE, so each counts two.
        self.assertEqual(estimate_text_tokens("日本語テスト"), 12)
        self.assertEqual(estimate_text_tokens("📱" * 4), 8)
        self.assertEqual(estimate_text_tokens("abc日本語"), 7)
        self.assertEqual(estimate_text_tokens("café ¥42000"), 7)

    def test_request_estimate_accounts_for_response_schema(self):
        from pipeline.spending import (
            REQUEST_OVERHEAD_TOKENS,
            estimate_request_tokens,
            estimate_text_tokens,
        )

        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        total = estimate_request_tokens(
            texts=["system", "user payload"],
            max_output_tokens=2000,
            schema=schema,
        )
        self.assertEqual(
            total,
            estimate_text_tokens("system")
            + estimate_text_tokens("user payload")
            + 2000
            + REQUEST_OVERHEAD_TOKENS
            + estimate_text_tokens(json.dumps(schema, sort_keys=True)),
        )

    def test_dispatch_reservation_includes_schema(self):
        from common.meta import RESPONSE_SCHEMA, SYSTEM_PROMPT
        from pipeline.spending import estimate_text_tokens

        max_output = 2000
        body = {"rows": [{"index": 0, "data": {"a": 1}}]}
        user_content = json.dumps(body, separators=(",", ":"))
        # A budget covering the old schema-blind math must now refuse:
        # the serialized response schema also travels on the request.
        old_math = (
            estimate_text_tokens(SYSTEM_PROMPT)
            + estimate_text_tokens(user_content)
            + max_output
            + 64
        )
        schema_tokens = estimate_text_tokens(
            json.dumps(RESPONSE_SCHEMA, sort_keys=True)
        )
        self.assertGreater(schema_tokens, 0)
        self.allow(old_math)
        script = _FakeResponse(
            json.dumps({"rows": []}), _FakeUsage(10, 5)
        )
        provider, _holder = self.provider(script, max_output_tokens=max_output)
        with self.assertRaises(ProviderError):
            provider.interpret_table(
                {"rows": [{"index": 0, "data": {"a": 1}}]}
            )

    def test_reservation_covers_full_permitted_call(self):
        self.allow(100000)
        body = json.dumps({"rows": []})
        script = _FakeResponse(body, _FakeUsage(10, 5))
        provider, _holder = self.provider(script, max_output_tokens=2000)
        result = provider.interpret_table({"rows": [{"index": 0, "data": {"a": 1}}]})
        self.assertEqual(result["usage"]["total_tokens"], 15)
        # Observed usage reconciles exactly even though the hold covered
        # the full permitted output.
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 15)
        self.assertEqual(allowance.tokens_reserved, 0)

    def test_reconcile_works_while_paused(self):
        self.allow(100000)
        reservation = reserve(self.org.id, 100, 1)
        SpendAllowance.objects.filter(organization_id=self.org.id).update(
            paused=True
        )
        reconcile(reservation, 60)
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 60)
        self.assertEqual(allowance.tokens_reserved, 0)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)


class OperatorCeilingTests(SpendingTests):
    def test_zero_operator_refuses_approved_org(self):
        self.allow(100000)
        OperatorSpend.objects.update(monthly_ceiling=0)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)

    def test_operator_ceiling_binds_before_org_budget(self):
        self.allow(100000)
        OperatorSpend.objects.update(monthly_ceiling=150)
        reservation = reserve(self.org.id, 100, 2)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 2)
        release(reservation)

    def test_operator_ceiling_is_shared_across_orgs(self):
        self.allow(100000)
        org2 = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=self.user, organization=org2, role="owner"
        )
        other = get_allowance(org2.id)
        other.token_budget = 100000
        other.monthly_credits = 100000
        other.save()
        OperatorSpend.objects.update(monthly_ceiling=150)
        reservation = reserve(self.org.id, 100, 2)
        with self.assertRaises(BudgetRefused):
            reserve(org2.id, 100, 2)
        release(reservation)
        reserve(org2.id, 100, 2)

    def test_reconcile_updates_operator_counters(self):
        self.allow(100000)
        reservation = reserve(self.org.id, 100, 1)
        self.assertEqual(self.operator().tokens_reserved, 100)
        reconcile(reservation, 60)
        operator = self.operator()
        self.assertEqual(operator.tokens_reserved, 0)
        self.assertEqual(operator.period_spent, 60)
        self.assertEqual(operator.lifetime_spent, 60)

    def test_operator_kill_switch_blocks_new_but_settles_accepted(self):
        self.allow(100000)
        reservation = reserve(self.org.id, 100, 1)
        OperatorSpend.objects.update(paused=True)
        with self.assertRaises(BudgetRefused):
            reserve(self.org.id, 100, 1)
        reconcile(reservation, 60)
        operator = self.operator()
        self.assertEqual(operator.period_spent, 60)
        self.assertEqual(operator.lifetime_spent, 60)

    def test_operator_month_resets_without_touching_lifetime(self):
        from datetime import timedelta as _td

        from django.utils import timezone as _tz

        from pipeline.spending import _snap_operator_month

        self.allow(100000)
        operator = self.operator()
        operator.period_spent = 500
        operator.lifetime_spent = 500
        operator.period_start = _tz.localdate() - _td(days=40)
        operator.save()
        _snap_operator_month(operator)
        operator.save()
        operator.refresh_from_db()
        self.assertEqual(operator.period_spent, 0)
        self.assertEqual(operator.lifetime_spent, 500)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OperatorSpendAdminTests(TestCase):
    """The operator singleton must be creatable exactly once via admin.

    An empty table with a blanket add refusal leaves paid dispatch
    permanently unapproved: every reserve rolls its auto-created row
    back, and the admin offers no row to approve.
    """

    def setUp(self):
        from django.contrib import admin as djadmin
        from django.test import RequestFactory

        from pipeline.admin import OperatorSpendAdmin

        request = RequestFactory().get("/admin/pipeline/operatorspend/add/")
        request.user = User.objects.create_superuser(
            username="admin", email="admin@example.test", password="x"
        )
        self.request = request
        self.model_admin = OperatorSpendAdmin(OperatorSpend, djadmin.AdminSite())

    def test_add_allowed_when_no_row_exists(self):
        self.assertFalse(OperatorSpend.objects.exists())
        self.assertTrue(self.model_admin.has_add_permission(self.request))

    def test_add_refused_once_singleton_exists(self):
        OperatorSpend.objects.create(monthly_ceiling=100)
        self.assertFalse(self.model_admin.has_add_permission(self.request))


class AssistEnvelopeTests(SpendingTests):
    """The assist wire format must satisfy strict structured output.

    Strict mode rejects any object without additionalProperties false
    (the API answers 400), so free-form maps are not expressible:
    constants travel as {name, value} pairs and fold to a dict here.
    """

    def _assist(self, body):
        script = _FakeResponse(json.dumps(body), _FakeUsage(30, 12))
        provider, _ = self.provider(script)
        return provider.assist_preview(
            {
                "preview": {
                    "message_lines": True,
                    "lines": [{"line": 1, "text": "ACME pricelist"}],
                },
                "conversation": [
                    {"role": "user", "content": "Set the supplier."}
                ],
            }
        )

    def _assert_strict(self, node):
        if isinstance(node, dict) and node.get("type") == "object":
            properties = node.get("properties")
            self.assertIsInstance(properties, dict)
            self.assertEqual(node.get("additionalProperties"), False)
            self.assertEqual(set(node.get("required", [])), set(properties))
            for child in properties.values():
                self._assert_strict(child)
        elif isinstance(node, dict) and node.get("type") == "array":
            self._assert_strict(node.get("items", {}))
        elif isinstance(node, dict):
            for child in node.values():
                self._assert_strict(child)
        elif isinstance(node, list):
            for child in node:
                self._assert_strict(child)

    def test_assist_schema_is_strict_compatible(self):
        from common.meta import ASSIST_PREVIEW_RESPONSE_SCHEMA

        self._assert_strict(ASSIST_PREVIEW_RESPONSE_SCHEMA)

    def test_assist_prompt_preserves_section_headers(self):
        from common.meta import ASSIST_PREVIEW_SYSTEM_PROMPT

        # Dropping a grade banner would destroy the grades the
        # importer inherits from it; pin the preservation rule.
        self.assertIn("Never propose dropping section headers", ASSIST_PREVIEW_SYSTEM_PROMPT)
        self.assertIn("confirm them with section_value instead", ASSIST_PREVIEW_SYSTEM_PROMPT)

    def test_assist_parses_constant_pairs(self):
        self.allow(100000)
        result = self._assist(
            {
                "message": "Done.",
                "set_constants": [{"name": "supplier", "value": "ACME"}],
                "drop_lines": [1],
            }
        )
        self.assertEqual(result["set_constants"], {"supplier": "ACME"})
        self.assertEqual(result["drop_lines"], [1])
        self.assertEqual(result["usage"]["total_tokens"], 42)

    def test_assist_rejects_malformed_pairs(self):
        self.allow(100000)
        with self.assertRaises(ProposalUnusable):
            self._assist(
                {
                    "message": "Done.",
                    "set_constants": [{"name": "supplier"}],
                    "drop_lines": [],
                }
            )
