"""Seed utilization demo command tests: creation, idempotency, isolation, errors."""
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from portal.models import CropCycle, Equipment, EquipmentProperty, WorkItem
from portal.test_utilization import mkfarm, mkenv, mkuser


def _count(model_label):
    try:
        app_label, model_name = model_label.split(".")
        model = apps.get_model(app_label, model_name)
    except Exception:
        return None
    if model is None:
        return None
    return model.objects.count()


class SeedUtilizationDemoTests(TestCase):
    def test_seed_creates_and_idempotent(self):
        u = mkuser("seed")
        farm = mkfarm(u)
        _loc1, prop1, _cyc1 = mkenv(farm)
        _loc2, prop2, _cyc2 = mkenv(farm)
        other_user = mkuser("seedother")
        other_farm = mkfarm(other_user)

        crops_before = CropCycle.objects.count()
        consent_before = _count("portal.ConsentEvent")
        advice_before = _count("portal.AdviceRun")

        call_command("seed_utilization_demo", farm_id=farm.pk)

        self.assertEqual(WorkItem.objects.filter(farm=farm).count(), 6)
        self.assertEqual(Equipment.objects.filter(farm=farm).count(), 1)
        self.assertEqual(
            EquipmentProperty.objects.filter(equipment__farm=farm).count(), 2
        )
        eq = Equipment.objects.get(farm=farm)
        for prop in (prop1, prop2):
            for n in (1, 2, 3):
                title = "[DEMO GANTT] %s - %d" % (prop.pk, n)
                wi = WorkItem.objects.get(farm=farm, title=title)
                self.assertEqual(wi.property_id, prop.pk)
                self.assertEqual(wi.guidance, "SIMULATED demonstration; not confirmed schedule")
            third = WorkItem.objects.get(farm=farm, title="[DEMO GANTT] %s - 3" % prop.pk)
            self.assertIsNone(third.equipment)
            self.assertTrue(third.missing)
            self.assertEqual(third.recommendation, "SIMULATED: missing harvesting capacity")

        # Snapshot for idempotency comparison.
        first_rows = list(
            WorkItem.objects.filter(farm=farm).order_by("title").values_list(
                "title", "start_date", "end_date", "estimated_hours",
                "capability", "equipment_id", "missing", "recommendation", "guidance",
            )
        )
        first_links = list(
            EquipmentProperty.objects.filter(equipment__farm=farm).order_by(
                "equipment_id", "property_id"
            ).values_list("equipment_id", "property_id")
        )

        call_command("seed_utilization_demo", farm_id=farm.pk)

        self.assertEqual(WorkItem.objects.filter(farm=farm).count(), 6)
        self.assertEqual(Equipment.objects.filter(farm=farm).count(), 1)
        self.assertEqual(
            EquipmentProperty.objects.filter(equipment__farm=farm).count(), 2
        )
        second_rows = list(
            WorkItem.objects.filter(farm=farm).order_by("title").values_list(
                "title", "start_date", "end_date", "estimated_hours",
                "capability", "equipment_id", "missing", "recommendation", "guidance",
            )
        )
        second_links = list(
            EquipmentProperty.objects.filter(equipment__farm=farm).order_by(
                "equipment_id", "property_id"
            ).values_list("equipment_id", "property_id")
        )
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_links, second_links)
        self.assertEqual(eq.pk, Equipment.objects.get(farm=farm).pk)

        # Other farm isolation.
        self.assertEqual(WorkItem.objects.filter(farm=other_farm).count(), 0)
        self.assertEqual(Equipment.objects.filter(farm=other_farm).count(), 0)

        # No other data changes.
        self.assertEqual(CropCycle.objects.count(), crops_before)
        if consent_before is not None:
            self.assertEqual(_count("portal.ConsentEvent"), consent_before)
        if advice_before is not None:
            self.assertEqual(_count("portal.AdviceRun"), advice_before)

    def test_existing_demo_equipment_not_updated(self):
        u = mkuser("seedex")
        farm = mkfarm(u)
        _loc, _prop, _cyc = mkenv(farm)
        eq = Equipment.objects.create(
            farm=farm, name="DEMO GANTT Equipment",
            capabilities=["tillage"], available=False, hours_per_day="5",
        )
        call_command("seed_utilization_demo", farm_id=farm.pk)
        eq.refresh_from_db()
        self.assertEqual(list(eq.capabilities), ["tillage"])
        self.assertFalse(eq.available)
        self.assertEqual(str(eq.hours_per_day), "5.0")

    def test_no_eligible_cycle_fails_without_mutation(self):
        u = mkuser("seedno")
        farm = mkfarm(u)
        with self.assertRaises(CommandError):
            call_command("seed_utilization_demo", farm_id=farm.pk)
        self.assertEqual(Equipment.objects.filter(farm=farm).count(), 0)
        self.assertEqual(WorkItem.objects.filter(farm=farm).count(), 0)
