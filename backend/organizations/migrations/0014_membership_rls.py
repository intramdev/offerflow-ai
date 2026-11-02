"""Row-level security for organization memberships (PostgreSQL only).

Membership rows are visible to the member themself (user context, which the
middleware publishes before membership resolution) or inside the row's own
organization context (so owners and admins can manage their company). No
context at all means no rows. Writes follow the same rule: joining and
leaving stay possible exactly where the application authorizes them.
"""

from django.db import migrations

ENABLE_RLS = """
ALTER TABLE organizations_membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations_membership FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_user_or_org_isolation ON organizations_membership
    FOR ALL
    USING (
        user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
        OR organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
        OR organization_id = NULLIF(current_setting('app.current_org_id', true), '')::integer
    );
"""

DISABLE_RLS = """
DROP POLICY IF EXISTS membership_user_or_org_isolation ON organizations_membership;
ALTER TABLE organizations_membership NO FORCE ROW LEVEL SECURITY;
ALTER TABLE organizations_membership DISABLE ROW LEVEL SECURITY;
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
    dependencies = [("organizations", "0013_organization_quote_export")]

    operations = [migrations.RunPython(apply_rls, reverse_code=drop_rls)]
