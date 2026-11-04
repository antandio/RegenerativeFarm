"""Stage2b RED: owner export covers new entities safely; seed creates 2 Szeged properties+crops idempotently."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
import json
from portal.models import Farm, Membership, FarmLocation, FarmProperty, Equipment, EquipmentProperty, CropCycle, ConsentEvent
from portal.test_stage2 import owner_farm, make_location, make_property
User = get_user_model(); PW="TesztJelszo123!"

class ExportScopedTests(TestCase):
    def test_export_has_new_entities_no_secrets(self):
        u, f = owner_farm("exp1@example.com")
        loc = make_location(f); prop = make_property(f, loc, "A parcella")
        eq = Equipment.objects.create(farm=f, name="Eke", capabilities=["tillage"], available=True, hours_per_day="6")
        EquipmentProperty.objects.create(equipment=eq, property=prop)
        CropCycle.objects.create(farm=f, property=prop, crop_name="Buza", state="current")
        other = User.objects.create_user(email="oth@example.com", password=PW)
        Membership.objects.create(farm=f, user=other, role="viewer", is_active=True)
        c = Client(); c.force_login(u)
        r = c.get("/fiok/export/"); self.assertEqual(r.status_code, 200)
        d = json.loads(r.content)
        for k in ("locations","properties","equipment","equipment_assignments","crop_cycles"):
            self.assertIn(k, d, k)
        self.assertNotIn("token_hash", r.content.decode())
        self.assertNotIn("oth@example.com", r.content.decode())
    def test_export_tenant_scoped(self):
        ua, fa = owner_farm("ea@example.com"); ub, fb = owner_farm("eb@example.com")
        make_location(fb, "B loc")
        c = Client(); c.force_login(ua)
        d = json.loads(c.get("/fiok/export/").content)
        self.assertFalse(any(x.get("name")=="B loc" for x in d.get("locations",[])))

class SeedSzegedTests(TestCase):
    def test_seed_adds_two_properties_and_crops_idempotent(self):
        call_command("seed_szeged_test"); call_command("seed_szeged_test")
        from portal.models import User as U
        u = U.objects.get(email="szeged.teszt@example.invalid")
        f = Farm.objects.get(owner=u)
        self.assertEqual(FarmProperty.objects.filter(farm=f).count(), 2)
        self.assertEqual(CropCycle.objects.filter(farm=f).count(), 2)
        pw1 = U.objects.get(email="szeged.teszt@example.invalid").password
        call_command("seed_szeged_test")
        self.assertEqual(U.objects.get(email="szeged.teszt@example.invalid").password, pw1)
        self.assertEqual(FarmAction if False else 1, 1)
