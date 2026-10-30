"""Row-level security for the spend allowance table (PostgreSQL only).

Split out of 0003_pipeline_rls because this table is created in
0004_spendallowance: a policy migration can only touch tables that already
exist at its point in the plan. Same organization equality check as the
other tenant tables, so no context means no rows.
"""

import importlib

from django.db import migrations

_rls = importlib.import_module("pipeline.migrations.0003_pipeline_rls")

TENANT_TABLES = ["pipeline_spendallowance"]


def apply_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TENANT_TABLES:
        schema_editor.execute(_rls.POLICY_TEMPLATE.format(table=table))


def drop_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TENANT_TABLES:
        schema_editor.execute(_rls.REVERT_TEMPLATE.format(table=table))


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0005_normalizedoffer_interpreted")]

    operations = [migrations.RunPython(apply_rls, reverse_code=drop_rls)]
