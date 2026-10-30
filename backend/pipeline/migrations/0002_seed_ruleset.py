"""Seed the initial active rule set (engine agnostic)."""

from django.db import migrations

RULES_V1 = {
    "critical_fields": [
        "supplier",
        "model_name",
        "capacity_gb",
        "grade",
        "lock_state",
        "quantity",
        "unit_price",
        "currency",
    ],
    "optional_fields": ["brand", "color", "conditions"],
    "required_columns": ["supplier", "model", "price"],
    "bare_capacity_unit": "GB",
    "locked_excluded": True,
}


def seed_rules(apps, schema_editor):
    rule_model = apps.get_model("pipeline", "RuleSetVersion")
    rule_model.objects.get_or_create(
        version="v1", defaults={"spec": RULES_V1, "is_active": True}
    )


def unseed_rules(apps, schema_editor):
    rule_model = apps.get_model("pipeline", "RuleSetVersion")
    rule_model.objects.filter(version="v1").delete()


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0001_initial")]

    operations = [migrations.RunPython(seed_rules, reverse_code=unseed_rules)]
