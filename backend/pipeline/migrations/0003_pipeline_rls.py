"""Row-level security for pipeline tenant tables (PostgreSQL only).

Every policy is a plain organization equality check against session context,
so no context means no rows. Rule set versions stay global reference data
with no policy by design.
"""

from django.db import migrations

TENANT_TABLES = [
    "pipeline_sourcefile",
    "pipeline_importrun",
    "pipeline_sourcerow",
    "pipeline_normalizedoffer",
    "pipeline_validationresult",
    "pipeline_reviewrequest",
    "pipeline_reviewdecision",
    "pipeline_exportartifact",
    "pipeline_usageevent",
    "pipeline_auditevent",
    # pipeline_spendallowance is covered by 0006: its table is created in
    # 0004, and a policy migration can only touch tables that already exist.
]

POLICY_TEMPLATE = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY {table}_org_isolation ON {table}
    FOR ALL
    USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer)
    WITH CHECK (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer);
"""

REVERT_TEMPLATE = """
DROP POLICY IF EXISTS {table}_org_isolation ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


def apply_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TENANT_TABLES:
        schema_editor.execute(POLICY_TEMPLATE.format(table=table))


def drop_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TENANT_TABLES:
        schema_editor.execute(REVERT_TEMPLATE.format(table=table))


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0002_seed_ruleset")]

    operations = [migrations.RunPython(apply_rls, reverse_code=drop_rls)]
