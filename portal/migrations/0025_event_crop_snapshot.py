"""Task12 slice 3: stable crop snapshot on journal rows plus linked-only backfill."""
from django.db import migrations, models


def backfill_snapshots(apps, schema_editor):
    # Fill only blank snapshots that have a linked cycle; never overwrite.
    FarmingEvent = apps.get_model("portal", "FarmingEvent")
    qs = FarmingEvent.objects.filter(
        crop_name_snapshot="", crop_cycle__isnull=False).select_related("crop_cycle")
    for e in qs.iterator():
        name = ((e.crop_cycle.crop_name if e.crop_cycle else "") or "").strip()
        if name:
            FarmingEvent.objects.filter(
                pk=e.pk, crop_name_snapshot="").update(crop_name_snapshot=name[:120])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0024_cropcycle_soft_hide'),
    ]

    operations = [
        migrations.AddField(
            model_name='farmingevent',
            name='crop_name_snapshot',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.RunPython(backfill_snapshots, noop_reverse),
    ]
