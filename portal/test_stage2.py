"""Stage 2 RED tests: tenant-scoped CRUD locations/properties/equipment/cropcycles."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from portal.models import Farm, Membership, FarmLocation, FarmProperty, Equipment, EquipmentProperty, CropCycle

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="owner@example.com"):
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def add_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def make_location(farm, name="Szeged loc"):
    return FarmLocation.objects.create(farm=farm, name=name, latitude="46.253000", longitude="20.141400", timezone="Europe/Budapest")


def make_property(farm, loc=None, name="A parcella", kind="field"):
    loc = loc or make_location(farm)
    return FarmProperty.objects.create(farm=farm, location=loc, name=name, kind=kind, area_hectares="5.00")


class LocationCrudTests(TestCase):
    def test_location_crud_e2e(self):
        u, f = owner_farm()
        c = Client()
        c.force_login(u)
        r = c.post("/locations/new/", {"name": "Tanya", "latitude": "46.25", "longitude": "20.14", "timezone": "Europe/Budapest"})
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        loc = FarmLocation.objects.get(farm=f, name="Tanya")
        r = c.get("/locations/")
        self.assertContains(r, "Tanya")
        r = c.post(f"/locations/{loc.pk}/edit/", {"name": "Tanya2", "latitude": "46.26", "longitude": "20.15", "timezone": "Europe/Budapest"})
        self.assertEqual(r.status_code, 302)
        loc.refresh_from_db()
        self.assertEqual(loc.name, "Tanya2")
        r = c.get(f"/locations/{loc.pk}/delete/")
        self.assertEqual(r.status_code, 200)
        r = c.post(f"/locations/{loc.pk}/delete/", {})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(FarmLocation.objects.filter(pk=loc.pk).exists())

    def test_invalid_coords_timezone(self):
        u, f = owner_farm()
        c = Client()
        c.force_login(u)
        for bad in ({"name": "X", "latitude": "200", "longitude": "20", "timezone": "Europe/Budapest"},
                    {"name": "X", "latitude": "46", "longitude": "-200", "timezone": "Europe/Budapest"},
                    {"name": "X", "latitude": "46", "longitude": "20", "timezone": "Mars/Olympus"}):
            r = c.post("/locations/new/", bad)
            self.assertEqual(r.status_code, 200, bad)
        self.assertEqual(FarmLocation.objects.filter(farm=f).count(), 0)
        with self.assertRaises(ValidationError):
            FarmLocation(farm=f, name="B", latitude=91, longitude=0, timezone="Europe/Budapest").full_clean()

    def test_negative_ids_404(self):
        u, f = owner_farm()
        c = Client()
        c.force_login(u)
        self.assertEqual(c.get("/locations/-5/edit/").status_code, 404)
        self.assertEqual(c.get("/properties/-1/delete/").status_code, 404)


class CrossFarmTests(TestCase):
    def test_crossfarm_property_fk_blocked(self):
        ua, fa = owner_farm("a@example.com")
        ub, fb = owner_farm("b@example.com")
        locb = make_location(fb, "B loc")
        c = Client()
        c.force_login(ua)
        r = c.post("/properties/new/", {"location": str(locb.pk), "name": "Sneaky", "kind": "field", "area_hectares": "1", "notes": ""})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(FarmProperty.objects.filter(farm=fa, name="Sneaky").exists())
        with self.assertRaises(ValidationError):
            p = FarmProperty(farm=fa, location=locb, name="S", kind="field", area_hectares="1")
            p.full_clean()

    def test_crossfarm_equipment_m2m_blocked(self):
        ua, fa = owner_farm("a2@example.com")
        ub, fb = owner_farm("b2@example.com")
        propb = make_property(fb)
        propa = make_property(fa)
        c = Client()
        c.force_login(ua)
        r = c.post("/equipment/new/", {"name": "Traktor", "capabilities": ["transport"], "available": "on",
                                       "hours_per_day": "8", "properties": [str(propb.pk)]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Equipment.objects.filter(farm=fa, name="Traktor").exists())
        eq = Equipment.objects.create(farm=fa, name="Eke", capabilities=["tillage"], available=True, hours_per_day="6")
        with self.assertRaises(ValidationError):
            EquipmentProperty(equipment=eq, property=propb).save()
        EquipmentProperty.objects.create(equipment=eq, property=propa)
        self.assertEqual(eq.properties.count(), 1)


class RoleTests(TestCase):
    def test_viewer_worker_write_blocked(self):
        uo, f = owner_farm("own@example.com")
        viewer = add_member(f, "viewer@example.com", "viewer")
        worker = add_member(f, "worker@example.com", "worker")
        for u in (viewer, worker):
            c = Client()
            c.force_login(u)
            r = c.post("/locations/new/", {"name": "Nope", "latitude": "46", "longitude": "20", "timezone": "Europe/Budapest"})
            self.assertEqual(r.status_code, 403, u.email)
        self.assertEqual(FarmLocation.objects.filter(farm=f).count(), 0)
        loc = make_location(f)
        c = Client()
        c.force_login(worker)
        self.assertEqual(c.post(f"/locations/{loc.pk}/delete/", {}).status_code, 403)
        self.assertTrue(FarmLocation.objects.filter(pk=loc.pk).exists())

    def test_worker_observation_allowed_viewer_blocked(self):
        uo, f = owner_farm("own3@example.com")
        worker = add_member(f, "w3@example.com", "worker")
        viewer = add_member(f, "v3@example.com", "viewer")
        prop = make_property(f)
        cyc = CropCycle.objects.create(farm=f, property=prop, crop_name="Buza", state="current", growth_stage="csirazas")
        c = Client()
        c.force_login(worker)
        r = c.post(f"/cycles/{cyc.pk}/observe/", {"observation": "Szep zold foltokban."})
        self.assertIn(r.status_code, (200, 302), r.content[:300] if r.status_code not in (200, 302) else "")
        cyc.refresh_from_db()
        self.assertIn("Szep zold", cyc.observations)
        c.force_login(viewer)
        self.assertEqual(c.post(f"/cycles/{cyc.pk}/observe/", {"observation": "x"}).status_code, 403)


class CropCycleTests(TestCase):
    def test_duplicate_current_blocked_planned_multiple_history_open(self):
        u, f = owner_farm("cc@example.com")
        prop = make_property(f)
        CropCycle.objects.create(farm=f, property=prop, crop_name="A", state="current")
        with self.assertRaises(ValidationError):
            CropCycle(farm=f, property=prop, crop_name="B", state="current").full_clean()
        # New contract: multiple planned rows per property are allowed.
        CropCycle.objects.create(farm=f, property=prop, crop_name="P1", state="planned")
        CropCycle(farm=f, property=prop, crop_name="P2", state="planned").full_clean()
        CropCycle.objects.create(farm=f, property=prop, crop_name="P2", state="planned")
        self.assertEqual(CropCycle.objects.filter(farm=f, property=prop, state="planned").count(), 2)
        CropCycle.objects.create(farm=f, property=prop, crop_name="H1", state="history")
        CropCycle.objects.create(farm=f, property=prop, crop_name="H2", state="history")

    def test_dates_and_nonagri(self):
        u, f = owner_farm("cc2@example.com")
        prop = make_property(f)
        with self.assertRaises(ValidationError):
            CropCycle(farm=f, property=prop, crop_name="X", state="planned",
                      sow_date="2026-06-01", expected_end_date="2026-05-01").full_clean()
        off = make_property(f, name="Iroda", kind="office")
        with self.assertRaises(ValidationError):
            CropCycle(farm=f, property=off, crop_name="X", state="planned").full_clean()
        c = Client()
        c.force_login(u)
        r = c.post("/cycles/new/", {"property": str(off.pk), "crop_name": "X", "state": "planned",
                                    "growth_stage": "", "sow_date": "", "expected_end_date": "", "observations": ""})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CropCycle.objects.filter(farm=f, crop_name="X").exists())

    def test_delete_protect_location_friendly(self):
        u, f = owner_farm("del@example.com")
        loc = make_location(f)
        make_property(f, loc=loc)
        c = Client()
        c.force_login(u)
        r = c.post(f"/locations/{loc.pk}/delete/", {})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(FarmLocation.objects.filter(pk=loc.pk).exists())
