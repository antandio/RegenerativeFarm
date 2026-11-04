"""Async lifecycle fields for AgentRun (stage events, lease, scrub dates)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0035_chat_conversation_async"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentrun",
            name="input_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="stage_events",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="dispatch_authorized_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="cancelled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentrun",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
