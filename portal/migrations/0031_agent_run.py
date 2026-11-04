# Generated for WP1 sourced planner: durable agent run tables.

from django.db import migrations, models

import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0030_chatattempt_selection_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('idempotency_key', models.CharField(max_length=80)),
                ('payload_hash', models.CharField(max_length=64)),
                ('goal', models.TextField()),
                ('language', models.CharField(default='hu', max_length=10)),
                ('status', models.CharField(choices=[('queued', 'queued'), ('running', 'running'), ('preview', 'preview'), ('blocked', 'blocked'), ('done', 'done'), ('error', 'error'), ('outcome_unknown', 'outcome_unknown'), ('deleted', 'deleted')], default='queued', max_length=16)),
                ('stage', models.PositiveSmallIntegerField(default=0)),
                ('context_manifest', models.JSONField(default=dict)),
                ('policy_version', models.CharField(blank=True, default='', max_length=40)),
                ('policy_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('policy_cited_sections', models.JSONField(default=list)),
                ('candidate', models.JSONField(blank=True, null=True)),
                ('candidate_revision', models.PositiveSmallIntegerField(default=0)),
                ('critic_findings', models.JSONField(default=list)),
                ('budgets', models.JSONField(default=dict)),
                ('usage_summary', models.JSONField(default=dict)),
                ('error_code', models.CharField(blank=True, max_length=64, null=True)),
                ('error_message_safe', models.CharField(blank=True, max_length=300, null=True)),
                ('revalidation', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('farm', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_runs', to='portal.farm')),
                ('initiated_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_runs', to='portal.user')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['farm', 'status', 'created_at'], name='portal_agen_farm_id_4f8c1a_idx')],
                'constraints': [models.UniqueConstraint(fields=('farm', 'idempotency_key'), name='uniq_agentrun_farm_idem'), models.UniqueConstraint(fields=('farm', 'payload_hash'), name='uniq_agentrun_farm_payload'), models.UniqueConstraint(fields=('id', 'farm'), name='uniq_agentrun_id_farm'), models.CheckConstraint(condition=models.Q(('stage__gte', 0), ('stage__lte', 7)), name='agentrun_stage_1_7')],
            },
        ),
        migrations.CreateModel(
            name='AgentDocGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('granted', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_grants', to='portal.document')),
                ('granted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='agent_doc_grants', to='portal.user')),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='doc_grants', to='portal.agentrun')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('run', 'document'), name='uniq_agentdocgrant_run_doc')],
            },
        ),
        migrations.CreateModel(
            name='AgentCall',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('planner', 'planner'), ('critic', 'critic'), ('agri_classify', 'agri_classify')], max_length=16)),
                ('model', models.CharField(max_length=120)),
                ('status', models.CharField(choices=[('reserved', 'reserved'), ('success', 'success'), ('failed', 'failed'), ('unknown', 'unknown')], default='reserved', max_length=16)),
                ('input_tokens', models.PositiveIntegerField(blank=True, null=True)),
                ('output_tokens', models.PositiveIntegerField(blank=True, null=True)),
                ('cost_estimate', models.FloatField(blank=True, null=True)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='calls', to='portal.agentrun')),
            ],
            options={
                'ordering': ['started_at', 'id'],
            },
        ),
    ]
