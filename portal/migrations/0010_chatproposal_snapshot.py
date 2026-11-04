from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0009_alter_chatproposal_kind_alter_invitation_role_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatproposal',
            name='snapshot_status',
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name='chatproposal',
            name='snapshot_text',
            field=models.TextField(blank=True, null=True),
        ),
    ]
