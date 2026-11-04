"""Demo seed for utilization Gantt: simulated tasks per eligible property."""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from portal.domain_models import AGRICULTURAL_KINDS, EQUIPMENT_CAPABILITY_VALUES
from portal.models import CropCycle, Equipment, EquipmentProperty, Farm

DEMO_EQUIPMENT_NAME = "DEMO GANTT Equipment"
DEMO_GUIDANCE = "SIMULATED demonstration; not confirmed schedule"
DEMO_MISSING_REC = "SIMULATED: missing harvesting capacity"
DEMO_TITLE_FMT = "[DEMO GANTT] {property_pk} - {num}"


class Command(BaseCommand):
    help = "Seed simulated utilization Gantt demo tasks for a farm."

    def add_arguments(self, parser):
        parser.add_argument("--farm-id", type=int, required=True)

    def handle(self, *args, **options):
        farm_id = options["farm_id"]
        try:
            farm = Farm.objects.get(pk=farm_id)
        except Farm.DoesNotExist:
            raise CommandError("Farm %r not found." % (farm_id,))

        cycles = list(
            CropCycle.objects.filter(
                farm=farm,
                property__kind__in=AGRICULTURAL_KINDS,
                state__in=["current", "planned"],
            ).select_related("property")
        )
        cycles.sort(key=lambda c: (0 if c.state == "current" else 1, c.pk))
        chosen = {}
        for c in cycles:
            chosen.setdefault(c.property_id, c)

        if not chosen:
            raise CommandError("No eligible crop cycles for farm %r." % (farm_id,))

        today = timezone.now().date()
        monday = today - timedelta(days=today.weekday())

        specs = [
            {"offsets": (0, 2), "hours": "18", "cap": "other",
             "missing": False, "use_eq": True, "rec": ""},
            {"offsets": (8, 11), "hours": "12", "cap": "irrigation",
             "missing": False, "use_eq": True, "rec": ""},
            {"offsets": (14, 18), "hours": "8", "cap": "harvest",
             "missing": True, "use_eq": False, "rec": DEMO_MISSING_REC},
        ]

        with transaction.atomic():
            demo_caps = sorted(EQUIPMENT_CAPABILITY_VALUES)
            eq, _ = Equipment.objects.get_or_create(
                farm=farm,
                name=DEMO_EQUIPMENT_NAME,
                defaults={
                    "capabilities": demo_caps,
                    "available": True,
                    "hours_per_day": "2",
                },
            )
            for property_id in sorted(chosen):
                cycle = chosen[property_id]
                prop = cycle.property
                EquipmentProperty.objects.get_or_create(
                    equipment=eq, property=prop
                )
                for i, spec in enumerate(specs, start=1):
                    title = DEMO_TITLE_FMT.format(property_pk=prop.pk, num=i)
                    start = monday + timedelta(days=spec["offsets"][0])
                    end = monday + timedelta(days=spec["offsets"][1])
                    from portal.models import WorkItem

                    WorkItem.objects.get_or_create(
                        farm=farm,
                        title=title,
                        defaults={
                            "crop_cycle": cycle,
                            "property": prop,
                            "capability": spec["cap"],
                            "start_date": start,
                            "end_date": end,
                            "estimated_hours": spec["hours"],
                            "equipment": eq if spec["use_eq"] else None,
                            "missing": spec["missing"],
                            "recommendation": spec["rec"],
                            "guidance": DEMO_GUIDANCE,
                        },
                    )
        self.stdout.write(self.style.SUCCESS(
            "Seeded demo utilization for farm %s: %d properties." % (farm.pk, len(chosen))
        ))
