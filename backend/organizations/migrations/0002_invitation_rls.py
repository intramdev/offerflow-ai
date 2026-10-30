"""Row-level security for organization-scoped invitations.

PostgreSQL only; other engines skip the SQL. The policy compares the tenant
column against session context published by the middleware, so no context
means no rows. FORCE keeps the policy effective even for table owners, which
is what makes it real defense in depth on single-role development databases.
"""

from django.db import migrations

ENABLE_RLS = """
ALTER TABLE organizations_invitation ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations_invitation FORCE ROW LEVEL SECURITY;
CREATE POLICY invitation_org_isolation ON organizations_invitation
    FOR ALL
    USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer)
    WITH CHECK (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer);
"""

DISABLE_RLS = """
DROP POLICY IF EXISTS invitation_org_isolation ON organizations_invitation;
ALTER TABLE organizations_invitation NO FORCE ROW LEVEL SECURITY;
ALTER TABLE organizations_invitation DISABLE ROW LEVEL SECURITY;
"""


def apply_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(ENABLE_RLS)


def drop_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DISABLE_RLS)


class Migration(migrations.Migration):
    dependencies = [("organizations", "0001_initial")]

    operations = [migrations.RunPython(apply_rls, reverse_code=drop_rls)]
