"""Agent changeset envelope: immutable preview/apply/verify."""

from django.db import migrations, models

import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0032_agent_run_no_payload_unique'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentChangeset',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('idempotency_key', models.CharField(max_length=80)),
                ('payload_hash', models.CharField(max_length=64)),
                ('commands', models.JSONField(default=list)),
                ('before_snapshot', models.JSONField(default=dict)),
                ('before_hash', models.CharField(blank=True, default='', max_length=64)),
                ('after_snapshot', models.JSONField(default=dict)),
                ('after_hash', models.CharField(blank=True, default='', max_length=64)),
                ('source_vector', models.JSONField(default=list)),
                ('policy_version', models.CharField(blank=True, default='', max_length=40)),
                ('policy_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('run_critic_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('state', models.CharField(choices=[('preview', 'preview'), ('applying', 'applying'), ('applied', 'applied'), ('verified', 'verified'), ('rejected', 'rejected')], default='preview', max_length=16)),
                ('expires_at', models.DateTimeField()),
                ('receipt', models.JSONField(default=dict)),
                ('audit_ids', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_changesets', to='portal.user')),
                ('farm', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_changesets', to='portal.farm')),
                ('run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='changesets', to='portal.agentrun')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['farm', 'state', 'created_at'], name='portal_agen_farm_id_3189fa_idx')],
                'constraints': [models.UniqueConstraint(fields=('farm', 'idempotency_key'), name='uniq_changeset_farm_idem')],
            },
        ),
    ]
