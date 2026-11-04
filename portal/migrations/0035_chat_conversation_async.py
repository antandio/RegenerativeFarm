# Generated for the durable async chat slice (docs/CHAT_ASYNC.md).
# English comments per project convention. Assigns every legacy ChatAttempt
# to the owning initiated_by default ChatConversation; no answers invented.

from django.db import migrations, models
import django.db.models.deletion


def assign_default_conversations(apps, schema_editor):
    ChatAttempt = apps.get_model("portal", "ChatAttempt")
    ChatConversation = apps.get_model("portal", "ChatConversation")
    pairs = (ChatAttempt.objects.values_list("farm_id", "initiated_by_id")
             .distinct())
    for farm_id, user_id in pairs:
        conv, _ = ChatConversation.objects.get_or_create(
            farm_id=farm_id, owner_id=user_id, key="default")
        (ChatAttempt.objects.filter(farm_id=farm_id, initiated_by_id=user_id,
                                    conversation__isnull=True)
         .update(conversation=conv))


def unassign_conversations(apps, schema_editor):
    ChatAttempt = apps.get_model("portal", "ChatAttempt")
    ChatAttempt.objects.update(conversation=None)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0034_rename_portal_agen_farm_id_3189fa_idx_portal_agen_farm_id_685b10_idx_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChatConversation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(default='default', max_length=32)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('farm', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chat_conversations', to='portal.farm')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chat_conversations', to='portal.user')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('farm', 'owner', 'key'), name='uniq_chatconv_farm_owner_key')],
                'indexes': [models.Index(fields=['farm', 'owner'], name='portal_chatconv_farm_owner_idx')],
            },
        ),
        migrations.AddField(
            model_name='chatattempt',
            name='conversation',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='attempts', to='portal.chatconversation'),
        ),
        migrations.AddField(
            model_name='chatattempt',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(assign_default_conversations, unassign_conversations),
    ]
