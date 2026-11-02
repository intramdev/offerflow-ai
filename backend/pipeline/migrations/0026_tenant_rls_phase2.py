"""Row-level security for newer tenant tables (PostgreSQL only).

Covers the quote, alias, category, supplier, spending-reservation, and
assistant-flag records added after the first policy pass, following the
0004/0006 pattern: one policy migration per table batch. Supplier notes
carry no organization column of their own, so their policy resolves the
tenant through the parent supplier row instead.
"""

import importlib

from django.db import migrations

_rls = importlib.import_module("pipeline.migrations.0003_pipeline_rls")

TENANT_TABLES = [
    "pipeline_category",
    "pipeline_supplier",
    "pipeline_productalias",
    "pipeline_customerquote",
    "pipeline_spendreservation",
    "pipeline_assistantflag",
]

SUPPLIER_NOTE_POLICY = """
ALTER TABLE pipeline_suppliernote ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_suppliernote FORCE ROW LEVEL SECURITY;
CREATE POLICY pipeline_suppliernote_org_isolation ON pipeline_suppliernote
    FOR ALL
    USING (supplier_id IN (
        SELECT s.id FROM pipeline_supplier s
        WHERE s.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer
    ))
    WITH CHECK (supplier_id IN (
        SELECT s.id FROM pipeline_supplier s
        WHERE s.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer
    ));
"""

SUPPLIER_NOTE_REVERT = """
DROP POLICY IF EXISTS pipeline_suppliernote_org_isolation ON pipeline_suppliernote;
ALTER TABLE pipeline_suppliernote NO FORCE ROW LEVEL SECURITY;
ALTER TABLE pipeline_suppliernote DISABLE ROW LEVEL SECURITY;
"""


def apply_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TENANT_TABLES:
        schema_editor.execute(_rls.POLICY_TEMPLATE.format(table=table))
    schema_editor.execute(SUPPLIER_NOTE_POLICY)


def drop_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TENANT_TABLES:
        schema_editor.execute(_rls.REVERT_TEMPLATE.format(table=table))
    schema_editor.execute(SUPPLIER_NOTE_REVERT)


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0025_operatorspend")]

    operations = [migrations.RunPython(apply_rls, reverse_code=drop_rls)]
