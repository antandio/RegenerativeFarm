"""Stage B: property geometry fields (additive, nullable). No real migrate here."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("portal", "0011_farmingevent")]
    operations = [
        migrations.AddField(model_name="farmproperty", name="boundary_geojson",
                            field=models.JSONField(blank=True, default=None, null=True)),
        migrations.AddField(model_name="farmproperty", name="point_latitude",
                            field=models.DecimalField(blank=True, decimal_places=6, default=None, max_digits=8, null=True)),
        migrations.AddField(model_name="farmproperty", name="point_longitude",
                            field=models.DecimalField(blank=True, decimal_places=6, default=None, max_digits=9, null=True)),
    ]
