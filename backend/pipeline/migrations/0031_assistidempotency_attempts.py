# Keyed assist turns run on the worker while the browser polls: the row
# carries the queued turns, counts explicit attempts, and admits
# exactly one worker execution per attempt.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0030_assistidempotency"),
    ]

    operations = [
        migrations.AddField(
            model_name="assistidempotency",
            name="request",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="assistidempotency",
            name="attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="assistidempotency",
            name="claimed",
            field=models.BooleanField(default=False),
        ),
    ]
