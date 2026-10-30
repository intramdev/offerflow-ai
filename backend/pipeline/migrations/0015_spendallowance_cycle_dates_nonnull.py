"""Backfill billing-cycle dates left NULL by 0014, then enforce non-null."""

from django.db import migrations, models
from django.utils import timezone


def backfill_cycle_dates(apps, schema_editor):
    allowance_model = apps.get_model("pipeline", "SpendAllowance")
    today = timezone.localdate()
    allowance_model.objects.filter(cycle_anchor__isnull=True).update(
        cycle_anchor=today
    )
    allowance_model.objects.filter(period_start__isnull=True).update(
        period_start=today
    )


class Migration(migrations.Migration):
    # The data backfill must commit before the ALTER TABLE; Postgres
    # refuses DDL with pending trigger events in one transaction.
    atomic = False

    dependencies = [
        ("pipeline", "0014_spendallowance_cycle_anchor_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_cycle_dates, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="spendallowance",
            name="cycle_anchor",
            field=models.DateField(default=timezone.localdate),
        ),
        migrations.AlterField(
            model_name="spendallowance",
            name="period_start",
            field=models.DateField(default=timezone.localdate),
        ),
    ]
