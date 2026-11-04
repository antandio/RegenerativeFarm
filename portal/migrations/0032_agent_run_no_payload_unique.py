# Generated for gates: drop wrong farm+payload uniqueness (key stays unique).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0031_agent_run'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='agentrun',
            name='uniq_agentrun_farm_payload',
        ),
    ]
