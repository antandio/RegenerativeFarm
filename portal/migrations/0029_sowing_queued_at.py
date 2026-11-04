# Scheduling reservation for the sowing retry race fix.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0028_sowing_attempt_ledger'),
    ]

    operations = [
        migrations.AddField(
            model_name='sowingcomparison',
            name='queued_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
