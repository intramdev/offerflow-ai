"""Row-level security for the mapping memory table (PostgreSQL only).

Same organization equality check as the other tenant tables, so no
context means no rows. Split from 0008 following the 0004/0006 pattern: a
policy migration can only touch tables that already exist.
"""

import importlib

from django.db import migrations

_rls = importlib.import_module("pipeline.migrations.0003_pipeline_rls")

TENANT_TABLES = ["pipeline_columnmappingmemory"]


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
    dependencies = [("pipeline", "0008_columnmappingmemory")]

    operations = [migrations.RunPython(apply_rls, reverse_code=drop_rls)]
