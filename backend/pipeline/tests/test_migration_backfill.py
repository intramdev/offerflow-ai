"""Migration 0028 must backfill through historical models.

Regression test: the backfill passed historical Organization instances
into release helpers that queried by relation, which crashed the live
migrate with "Must be Organization instance". The helpers now key by id,
so the backfill works on the historical state.
"""

import importlib

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

from organizations.models import Membership, Organization
from pipeline.models import (
    CustomerQuote,
    ImportRun,
    PricingRule,
    RuleSetVersion,
    RunStatus,
    SourceFile,
)

User = get_user_model()

MIGRATION = ("pipeline", "0028_backfill_release_bindings")


class BackfillReleaseBindingsTests(TestCase):
    def test_backfill_binds_legacy_approvals(self):
        user = User.objects.create_user(username="owner", password="x")
        org = Organization.objects.create(name="Org")
        Membership.objects.create(user=user, organization=org, role="owner")
        PricingRule.objects.create(
            organization=org,
            name="Base",
            ops=[{"op": "markup", "percent": "10"}],
        )
        source_file = SourceFile.objects.create(
            organization=org,
            uploaded_by=user,
            original_name="s.xlsx",
            size_bytes=10,
            sha256="0" * 64,
            storage_key=f"org/{org.id}/1",
        )
        rule_version, _ = RuleSetVersion.objects.get_or_create(version="v1")
        run = ImportRun.objects.create(
            organization=org,
            source_file=source_file,
            rule_version=rule_version,
            status=RunStatus.APPROVED,
            approved_rules_digest="",
        )
        quote = CustomerQuote.objects.create(
            organization=org,
            status="ready",
            lines=[
                {
                    "brand": "Apple",
                    "model": "iPhone 15",
                    "unit_price": "100.0000",
                    "currency": "USD",
                    "quantity": 1,
                }
            ],
            source_run_ids=[run.id],
            approved_lines_digest="",
            created_by=user,
        )

        loader = MigrationLoader(connection, ignore_no_migrations=True)
        state = loader.project_state(nodes=[MIGRATION])
        module = importlib.import_module(
            f"{MIGRATION[0]}.migrations.{MIGRATION[1]}"
        )
        module.backfill(state.apps, None)

        run.refresh_from_db()
        quote.refresh_from_db()
        self.assertTrue(run.approved_rules_digest)
        self.assertEqual(quote.revision, 1)
        self.assertTrue(quote.approved_lines_digest)
        self.assertIn(str(run.id), quote.source_digests)
