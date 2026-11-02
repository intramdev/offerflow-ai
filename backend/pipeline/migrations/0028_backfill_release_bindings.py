"""Backfill release bindings for approvals that predate them.

Approval previously bound run exports to the offer digest and source hash
only, and customer quotes carried no binding at all. Existing approved runs
and ready quotes adopt their current state as the bound state: pricing-rule
digests for runs, presentation plus source digests for quotes. Anything the
backfill cannot bind (deleted sources, unpriced legacy lines) stays
unreleasable until the owner starts a fresh quote; nothing is silently
approved.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    from pipeline.release import (
        active_rules_digest,
        bind_customer_quote,
    )

    ImportRun = apps.get_model("pipeline", "ImportRun")
    CustomerQuote = apps.get_model("pipeline", "CustomerQuote")
    for run in ImportRun.objects.filter(
        status__in=("approved", "completed"), approved_rules_digest=""
    ):
        run.approved_rules_digest = active_rules_digest(run.organization)
        run.save(update_fields=["approved_rules_digest"])
    for quote in CustomerQuote.objects.filter(
        status="ready", approved_lines_digest=""
    ):
        quote.revision = 1
        bind_customer_quote(quote)
        quote.save(
            update_fields=[
                "revision",
                "approved_lines_digest",
                "source_digests",
            ]
        )


def noop(apps, schema_editor):
    return None


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0027_customerquote_approved_lines_digest_and_more")]

    operations = [migrations.RunPython(backfill, reverse_code=noop)]
