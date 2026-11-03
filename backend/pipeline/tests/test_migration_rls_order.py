"""RLS migrations must only touch tables that exist at their plan position.

Regression test for the 0003/0004 ordering bug: 0003 applied a policy to
pipeline_spendallowance, a table created one migration later, which broke
every PostgreSQL migrate. The sqlite suite skips the RLS steps, hiding it.

The check reads what each RLS migration will actually execute (its table
list plus any ALTER TABLE targets in its inline SQL) and asserts every
target exists in the migration state at that node. Same-app references are
exact: a table created later in the same app can never precede the node in
a global plan.
"""

import importlib
import re

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

RLS_MIGRATIONS = [
    ("pipeline", "0003_pipeline_rls"),
    ("pipeline", "0006_spendallowance_rls"),
    ("pipeline", "0009_columnmappingmemory_rls"),
    ("pipeline", "0011_pricingrule_rls"),
    ("pipeline", "0026_tenant_rls_phase2"),
    ("pipeline", "0030_assistidempotency"),
    ("organizations", "0002_invitation_rls"),
]

_ALTER_TABLE = re.compile(r"ALTER TABLE (\w+)")


def _migration_tables(app_label, name):
    module = importlib.import_module(f"{app_label}.migrations.{name}")
    tables = set(getattr(module, "TENANT_TABLES", ()))
    for value in vars(module).values():
        if isinstance(value, str) and "ROW LEVEL SECURITY" in value:
            tables.update(_ALTER_TABLE.findall(value))
    assert tables, f"{app_label}.{name} declares no RLS tables"
    return sorted(tables)


class RlsMigrationOrderTests(TestCase):
    def test_rls_tables_exist_at_migration_point(self):
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        for app_label, name in RLS_MIGRATIONS:
            with self.subTest(migration=f"{app_label}.{name}"):
                state = loader.project_state(nodes=[(app_label, name)])
                existing = set()
                for (model_app, model_name), model_state in state.models.items():
                    table = model_state.options.get("db_table")
                    if not table:
                        table = f"{model_app}_{model_name}"
                    existing.add(table)
                for table in _migration_tables(app_label, name):
                    self.assertIn(table, existing)
