"""Queued preview-assist lifecycle: 202 send, worker attempt, status poll.

The browser never holds a connection open across the provider call:
a keyed send queues one worker attempt and returns 202, and the
client polls the status endpoint until the attempt completes. These
tests pin that contract: pending/completed/failed/unknown mapping,
exactly-once dispatch across replays and redeliveries, tenant
scoping of the status endpoint, and revision safety for proposals
that finish after the draft moved.
"""

import os
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from common.meta import MetaProvider, ProviderError
from organizations.models import AIProviderConfig, Membership, Organization
from pipeline.jobs import run_assist_turn
from pipeline.models import AssistIdempotency, UsageEvent
from pipeline.tests.test_preview_assist import PASTE

User = get_user_model()


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CELERY_TASK_ALWAYS_EAGER=True,
)
class AssistLifecycleTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        file_override = override_settings(OFFERFLOW_FILE_ROOT=self.tmp.name)
        file_override.enable()
        self.addCleanup(file_override.disable)
        self.alice = User.objects.create_user(
            username="alice", email="alice@example.test", password="x"
        )
        self.bob = User.objects.create_user(
            username="bob", email="bob@example.test", password="x"
        )
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(
            user=self.alice, organization=self.org_a, role="owner"
        )
        Membership.objects.create(
            user=self.bob, organization=self.org_b, role="viewer"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        AIProviderConfig.objects.create(model="admin-model")

    def _paid_env(self):
        return mock.patch.dict(
            os.environ,
            {
                "OFFERFLOW_PROVIDER": "meta",
                "OFFERFLOW_ALLOW_PAID": "true",
                "OFFERFLOW_META_API_KEY": "LLM_9_envkey",
                "OFFERFLOW_META_MODEL": "env-model",
            },
            clear=False,
        )

    def _paste(self, text=PASTE):
        return self.client.post(
            "/api/pipeline/previews/from-text/",
            {"organization_id": self.org_a.id, "text": text},
            format="json",
        )

    def _send(self, key, content, idempotency_key):
        # No commit capture: the queued attempt stays pending, as if
        # the worker had not picked it up yet. Queueing still needs
        # the paid model configured; it never dispatches by itself.
        with self._paid_env():
            return self.client.post(
                f"/api/pipeline/previews/{key}/assist/",
                {
                    "messages": [{"role": "user", "content": content}],
                    "idempotency_key": idempotency_key,
                },
                format="json",
            )

    def _send_eager(self, key, content, idempotency_key):
        # Capture the commit so the eager test worker runs inline.
        with self.captureOnCommitCallbacks(execute=True):
            return self._send(key, content, idempotency_key)

    def _status(self, key, idempotency_key, client=None):
        client = client or self.client
        url = f"/api/pipeline/previews/{key}/assist/status/"
        return client.get(url, {"idempotency_key": idempotency_key})

    def _envelope(self, **overrides):
        envelope = {
            "message": "Set supplier, drop the footer.",
            "set_constants": {"supplier": "ACME Phones"},
            "drop_lines": [4],
            "usage": {"total_tokens": 42},
        }
        envelope.update(overrides)
        return envelope

    def _run_worker(self, record_id, organization_id):
        return run_assist_turn.apply(
            args=[record_id, organization_id]
        ).get()

    def _record(self, key):
        return AssistIdempotency.objects.get(
            organization=self.org_a, key=key
        )

    def test_send_queues_and_status_tracks_pending_then_completed(self):
        preview = self._paste().data["key"]
        calls = []

        def _fake(payload, on_chunk=None):
            calls.append(payload)
            on_chunk(1, 1, 30, 12)
            return self._envelope()

        send = self._send(preview, "Set the supplier.", "k-life")
        self.assertEqual(send.status_code, 202, send.content)
        self.assertTrue(send.json()["accepted"])
        self.assertEqual(send.json()["status"], "pending")
        self.assertEqual(send.json()["attempts"], 1)
        polling = self._status(preview, "k-life")
        self.assertEqual(polling.json()["status"], "pending")
        self.assertEqual(polling.json()["attempts"], 1)
        self.assertEqual(len(calls), 0)
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_fake
        ):
            outcome = self._run_worker(
                self._record("k-life").pk, self.org_a.id
            )
        self.assertEqual(outcome, "ok")
        done = self._status(preview, "k-life")
        self.assertEqual(done.json()["status"], "completed")
        self.assertTrue(done.json()["replayed"])
        self.assertEqual(done.json()["attempts"], 1)
        self.assertEqual(done.json()["base_revision"], 1)
        self.assertEqual(
            done.json()["proposal"]["set_constants"],
            {"supplier": "ACME Phones"},
        )
        replay = self._send(preview, "Set the supplier.", "k-life")
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(UsageEvent.objects.count(), 1)
        self.assertTrue(self._record("k-life").claimed)

    def test_second_send_while_claimed_conflicts_then_replays(self):
        preview = self._paste().data["key"]
        calls = []

        def _fake(payload, on_chunk=None):
            calls.append(payload)
            on_chunk(1, 1, 30, 12)
            return self._envelope()

        first = self._send(preview, "Set the supplier.", "k-claim")
        self.assertEqual(first.status_code, 202, first.content)
        # A worker picked the attempt up and holds the claim.
        AssistIdempotency.objects.filter(
            organization=self.org_a, key="k-claim"
        ).update(claimed=True)
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_fake
        ):
            conflict = self._send(preview, "Set the supplier.", "k-claim")
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertTrue(conflict.json()["retryable"])
        self.assertEqual(len(calls), 0)
        AssistIdempotency.objects.filter(
            organization=self.org_a, key="k-claim"
        ).update(claimed=False)
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_fake
        ):
            outcome = self._run_worker(
                self._record("k-claim").pk, self.org_a.id
            )
        self.assertEqual(outcome, "ok")
        done = self._status(preview, "k-claim")
        self.assertEqual(done.json()["status"], "completed")
        replay = self._send(preview, "Set the supplier.", "k-claim")
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(UsageEvent.objects.count(), 1)

    def test_failed_attempt_reports_failed_then_explicit_retry(self):
        preview = self._paste().data["key"]
        attempts = []

        def _flaky(payload, on_chunk=None):
            attempts.append(payload)
            on_chunk(1, 1, 10, 5)
            if len(attempts) == 1:
                raise ProviderError(
                    "Provider error (500) on assist; not retried."
                )
            return self._envelope()

        self._send(preview, "Set the supplier.", "k-fail")
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_flaky
        ):
            outcome = self._run_worker(
                self._record("k-fail").pk, self.org_a.id
            )
        self.assertEqual(outcome, "failed")
        failed = self._status(preview, "k-fail")
        self.assertEqual(failed.json()["status"], "failed")
        self.assertEqual(failed.json()["error_kind"], "provider")
        self.assertIn("Provider error (500)", failed.json()["error"])
        self.assertTrue(failed.json()["retryable"])
        self.assertEqual(failed.json()["attempts"], 1)
        # The failed attempt's partial spend stays booked.
        self.assertEqual(UsageEvent.objects.count(), 1)
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_flaky
        ):
            retry = self._send_eager(preview, "Set the supplier.", "k-fail")
        self.assertEqual(retry.status_code, 202, retry.content)
        done = self._status(preview, "k-fail")
        self.assertEqual(done.json()["status"], "completed")
        self.assertEqual(done.json()["attempts"], 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(UsageEvent.objects.count(), 2)

    def test_uncertain_provider_error_reports_unknown(self):
        preview = self._paste().data["key"]

        def _timeout(payload, on_chunk=None):
            on_chunk(1, 1, 20, 5)
            timed_out = ProviderError(
                "Provider assist call timed out; not retried."
            )
            timed_out.uncertain = True
            raise timed_out

        self._send(preview, "Set the supplier.", "k-unc")
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_timeout
        ):
            outcome = self._run_worker(
                self._record("k-unc").pk, self.org_a.id
            )
        self.assertEqual(outcome, "failed")
        unknown = self._status(preview, "k-unc")
        self.assertEqual(unknown.json()["status"], "unknown")
        self.assertEqual(unknown.json()["error_kind"], "unknown")
        self.assertIn("timed out", unknown.json()["error"])
        self.assertIn("credits log", unknown.json()["error"])
        self.assertEqual(unknown.json()["tokens"], 25)
        # Uncertain spend is kept, never silently freed.
        self.assertEqual(UsageEvent.objects.count(), 1)
        row = self._record("k-unc")
        self.assertEqual(row.state, "failed")
        self.assertEqual(row.attempts, 1)

    def test_unusable_proposal_surfaces_as_completed_guidance(self):
        preview = self._paste().data["key"]

        def _bad(payload, on_chunk=None):
            on_chunk(1, 1, 30, 12)
            return self._envelope(set_constants={"supplier": ""})

        self._send(preview, "Set the supplier.", "k-unuse")
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_bad
        ):
            outcome = self._run_worker(
                self._record("k-unuse").pk, self.org_a.id
            )
        self.assertEqual(outcome, "failed")
        row = self._record("k-unuse")
        self.assertEqual(row.response["error_kind"], "unusable")
        # ... but polling reports guidance, not a retry trap: same
        # shape as the inline path.
        guidance = self._status(preview, "k-unuse")
        self.assertEqual(guidance.json()["status"], "completed")
        self.assertIn(
            "could not turn that into", guidance.json()["message"]
        )
        self.assertEqual(
            guidance.json()["proposal"]["set_constants"], {}
        )
        self.assertIn("proposal failed validation", guidance.json()["hint"])

    def test_status_access_scoped_to_tenant_and_preview(self):
        preview = self._paste().data["key"]
        other = self._paste("Other pricelist\nWidget 10\n").data["key"]

        def _fake(payload, on_chunk=None):
            on_chunk(1, 1, 30, 12)
            return self._envelope()

        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_fake
        ):
            self._send_eager(preview, "Set the supplier.", "k-scope")
        mine = self._status(preview, "k-scope")
        self.assertEqual(mine.json()["status"], "completed")
        bob = APIClient()
        bob.force_authenticate(user=self.bob)
        foreign = self._status(preview, "k-scope", client=bob)
        self.assertEqual(foreign.status_code, 403, foreign.content)
        anonymous = APIClient()
        denied = self._status(preview, "k-scope", client=anonymous)
        self.assertEqual(denied.status_code, 403, denied.content)
        missing = self._status(preview, "k-nope")
        self.assertEqual(missing.status_code, 404, missing.content)
        mismatched = self._status(other, "k-scope")
        self.assertEqual(mismatched.status_code, 404, mismatched.content)

    def test_stale_proposal_cannot_apply_after_revision_moves(self):
        preview = self._paste().data["key"]

        def _fake(payload, on_chunk=None):
            on_chunk(1, 1, 30, 12)
            return self._envelope()

        self._send(preview, "Set the supplier.", "k-rev")
        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_fake
        ):
            self._run_worker(self._record("k-rev").pk, self.org_a.id)
        done = self._status(preview, "k-rev")
        self.assertEqual(done.json()["base_revision"], 1)
        manual = self.client.post(
            f"/api/pipeline/previews/{preview}/assist/apply/",
            {
                "set_constants": {"supplier": "Manual Co"},
                "drop_lines": [],
                "revision": 1,
            },
            format="json",
        )
        self.assertEqual(manual.status_code, 200, manual.content)
        proposal = done.json()["proposal"]
        stale = self.client.post(
            f"/api/pipeline/previews/{preview}/assist/apply/",
            {
                "set_constants": proposal["set_constants"],
                "drop_lines": proposal["drop_lines"],
                "operations": proposal["operations"],
                "revision": 2,
                "base_revision": done.json()["base_revision"],
            },
            format="json",
        )
        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertIn("recomputed", stale.json()["detail"])
        draft = self.client.get(f"/api/pipeline/previews/{preview}/").data[
            "draft"
        ]
        self.assertEqual(draft["revision"], 2)
        suppliers = [
            rule["value"]
            for rule in draft["rules"]
            if rule.get("field") == "supplier"
        ]
        self.assertEqual(suppliers, ["Manual Co"])

    def test_redelivered_task_never_dispatches_twice(self):
        preview = self._paste().data["key"]
        calls = []

        def _fake(payload, on_chunk=None):
            calls.append(payload)
            on_chunk(1, 1, 30, 12)
            return self._envelope()

        with self._paid_env(), mock.patch.object(
            MetaProvider, "assist_preview", side_effect=_fake
        ):
            self._send_eager(preview, "Set the supplier.", "k-dupe")
            record_id = self._record("k-dupe").pk
            # Broker redelivery of the same attempt: the claim holds.
            again = self._run_worker(record_id, self.org_a.id)
        self.assertEqual(again, "duplicate")
        self.assertEqual(len(calls), 1)
        self.assertEqual(UsageEvent.objects.count(), 1)
        done = self._status(preview, "k-dupe")
        self.assertEqual(done.json()["status"], "completed")
        self.assertEqual(done.json()["attempts"], 1)
