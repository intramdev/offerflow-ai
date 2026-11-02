"""Scope mapping memory by supplier as well as header signature.

Message pastes share one signature, so a single (organization,
signature) row let one supplier's confirmed supplier, currency, and
grade leak into another supplier's new paste. Profiles now key on
(organization, signature, supplier_hint): each supplier keeps its own
row, matched only on an explicit supplier, and proposed visibly.
Existing rows keep their stored supplier hint, so the tightened
constraint cannot collide: (organization, signature) was already
unique.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0028_backfill_release_bindings")]

    operations = [
        migrations.RemoveConstraint(
            model_name="columnmappingmemory",
            name="unique_org_mapping_memory",
        ),
        migrations.AddConstraint(
            model_name="columnmappingmemory",
            constraint=models.UniqueConstraint(
                fields=["organization", "signature", "supplier_hint"],
                name="unique_org_mapping_memory",
            ),
        ),
    ]
