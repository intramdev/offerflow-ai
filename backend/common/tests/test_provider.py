"""Tests for fail-closed provider selection and fixture determinism."""

import os
import socket
from unittest import mock

from django.test import SimpleTestCase

from common.provider import FixtureProvider, ProviderNotConfigured, get_provider

FULL_META_ENV = {
    "OFFERFLOW_PROVIDER": "meta",
    "OFFERFLOW_ALLOW_PAID": "true",
    "OFFERFLOW_META_API_KEY": "LLM_9_testsecret",
    "OFFERFLOW_META_MODEL": "test-model",
}


class ProviderFactoryTests(SimpleTestCase):
    def test_default_is_fixture(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = get_provider()
        self.assertIsInstance(provider, FixtureProvider)
        self.assertTrue(provider.is_simulated)

    def test_fixture_marks_simulated(self):
        result = FixtureProvider().interpret_table({"rows": [{"sku": "a"}]})
        self.assertTrue(result["simulated"])
        self.assertEqual(result["label"], "synthetic fixture response")
        self.assertEqual(result["row_count"], 1)

    def test_fixture_is_deterministic(self):
        payload = {"rows": [{"sku": "a"}, {"sku": "b"}]}
        first = FixtureProvider().interpret_table(payload)
        second = FixtureProvider().interpret_table(payload)
        self.assertEqual(first, second)

    def test_fixture_handles_malformed_payload(self):
        self.assertEqual(FixtureProvider().interpret_table({})["row_count"], 0)
        self.assertEqual(FixtureProvider().interpret_table({"rows": "nope"})["row_count"], 0)

    def test_fixture_uses_no_sockets(self):
        def blocked(*args, **kwargs):
            raise AssertionError("fixture touched the network")

        with mock.patch.object(socket, "socket", blocked):
            result = FixtureProvider().interpret_table({"rows": [{"a": 1}]})
        self.assertTrue(result["simulated"])

    def test_unknown_provider_is_refused(self):
        with mock.patch.dict(os.environ, {"OFFERFLOW_PROVIDER": "bogus"}, clear=True):
            with self.assertRaises(ProviderNotConfigured):
                get_provider()

    def test_meta_is_refused_without_approval(self):
        with mock.patch.dict(os.environ, {"OFFERFLOW_PROVIDER": "meta"}, clear=True):
            with self.assertRaises(ProviderNotConfigured):
                get_provider(organization_id=7)

    def test_meta_needs_organization(self):
        with mock.patch.dict(os.environ, FULL_META_ENV, clear=True):
            with self.assertRaises(ProviderNotConfigured):
                get_provider()

    def test_meta_rejects_malformed_key(self):
        env = dict(FULL_META_ENV, OFFERFLOW_META_API_KEY="not-a-model-key")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ProviderNotConfigured):
                get_provider(organization_id=7)

    def test_meta_constructs_with_full_config(self):
        with mock.patch.dict(os.environ, FULL_META_ENV, clear=True):
            provider = get_provider(organization_id=7)
        self.assertEqual(provider.name, "meta")
        self.assertFalse(provider.is_simulated)

    def test_meta_defaults_model_when_env_leaves_it_blank(self):
        env = dict(FULL_META_ENV, OFFERFLOW_META_MODEL="")
        with mock.patch.dict(os.environ, env, clear=True):
            provider = get_provider(organization_id=7)
        self.assertEqual(provider._config.model, "muse-spark-1.3-contributor")
