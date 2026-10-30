"""Tests for token budgets and the paid provider (mocked transport only).

No test here touches the network: the model client is always a fake. Live
calls require explicit owner activation with an approved allowance.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from openai import RateLimitError

from common.meta import MetaConfig, MetaProvider, ProviderError
from organizations.models import Membership, Organization
from pipeline.models import SpendAllowance
from pipeline.spending import (
    BudgetRefused,
    get_allowance,
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
        return allowance

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

    def test_default_budget_zero_refuses(self):
        allowance = get_allowance(self.org.id)
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
        provider = MetaProvider(
            self.config(max_input_tokens=200),
            client_factory=lambda config: client,
        )
        seen = []
        rows = [{"index": i, "data": {"blob": "x" * 400}} for i in range(3)]
        provider.interpret_table({"rows": rows}, on_chunk=lambda *a: seen.append(a))
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0], (1, 3, 10, 5))
        self.assertEqual(seen[2], (3, 3, 10, 5))

    def test_input_cap_splits_calls(self):
        self.allow(100000)
        completions = _EchoCompletions()
        client = mock.Mock()
        client.chat.completions = completions
        provider = MetaProvider(
            self.config(max_input_tokens=200),
            client_factory=lambda config: client,
        )
        rows = [
            {"index": i, "data": {"blob": "x" * 400}} for i in range(3)
        ]
        result = provider.interpret_table({"rows": rows})
        self.assertGreater(len(completions.calls), 1)
        self.assertEqual(len(result["rows"]), 3)

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
        provider = MetaProvider(
            self.config(max_input_tokens=200),
            client_factory=lambda config: client,
        )
        rows = [
            {"index": i, "data": {"blob": "x" * 400}} for i in range(3)
        ]
        with self.assertRaisesRegex(ProviderError, "chunk 2/"):
            provider.interpret_table({"rows": rows})
        allowance = SpendAllowance.objects.get(organization_id=self.org.id)
        self.assertEqual(allowance.tokens_spent, 15)
        self.assertEqual(allowance.tokens_reserved, 0)
        self.assertEqual(allowance.in_flight, 0)
