"""Stage 3 RED: deterministic plan, missing equipment, GET no writes, POST roles, isolation."""
from datetime import date
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import translation
from portal.models import Farm, Membership, FarmLocation, FarmProperty, Equipment, CropCycle, WorkItem
from portal.planning import generate_plan

User = get_user_model()
PW = "TesztJelszo123!"
N = [0]


def owner_farm3(prefix):
    N[0] += 1
    em = "%s%d@example.com" % (prefix, N[0])
    u = User.objects.create_user(email=em, password=PW)
    f = Farm.objects.create(owner=u, name="F%d" % N[0])
    return u, f


def env(f, cname="Buza"):
    loc = FarmLocation.objects.create(farm=f, name="L", latitude="47.1", longitude="19.1", timezone="Europe/Budapest")
    prop = FarmProperty.objects.create(farm=f, location=loc, name="P", kind="field", area_hectares="1")
    cyc = CropCycle.objects.create(farm=f, property=prop, crop_name=cname, state="current",
                                   sow_date=date(2026, 3, 1), expected_end_date=date(2026, 7, 1))
    return loc, prop, cyc


def cyc_dict(cyc):
    return {"id": cyc.id, "property_id": cyc.property_id, "crop_name": cyc.crop_name,
            "sow_date": cyc.sow_date, "expected_end_date": cyc.expected_end_date}


class PlanPureTests(TestCase):
    def test_deterministic(self):
        u, f = owner_farm3("det")
        _, _, c = env(f)
        eq = [{"id": 1, "capabilities": ["tillage", "sowing", "irrigation", "harvest"],
               "available": True, "property_ids": [c.property_id]}]
        a, _ = generate_plan([cyc_dict(c)], eq)
        b, _ = generate_plan([cyc_dict(c)], eq)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 4)

    def test_missing_equipment_rent(self):
        u, f = owner_farm3("mis")
        _, _, c = env(f)
        # Contract is Hungarian guidance ("garancia", "Bérlés"): bind locale
        # explicitly so the pure-function result never depends on test order.
        with translation.override("hu"):
            items, summary = generate_plan([cyc_dict(c)], [])
        self.assertTrue(all(i["missing"] for i in items))
        self.assertTrue(all("rl" in i["recommendation"] or "Bérlés" in i["recommendation"] for i in items))
        self.assertIn("week_peaks", summary)
        self.assertTrue(all("garancia" in i["guidance"] for i in items))


class PlanViewTests(TestCase):
    def test_get_no_writes(self):
        u, f = owner_farm3("get")
        env(f)
        c = Client()
        c.force_login(u)
        self.assertEqual(c.get("/terv/").status_code, 200)
        self.assertEqual(WorkItem.objects.filter(farm=f).count(), 0)

    def test_post_owner_manager_viewer(self):
        u, f = owner_farm3("own")
        env(f)
        mgr = User.objects.create_user(email="mgrx%d@example.com" % N[0], password=PW)
        Membership.objects.create(farm=f, user=mgr, role="manager", is_active=True)
        vwr = User.objects.create_user(email="vwrx%d@example.com" % N[0], password=PW)
        Membership.objects.create(farm=f, user=vwr, role="viewer", is_active=True)
        co = Client()
        co.force_login(u)
        self.assertEqual(co.post("/terv/").status_code, 302)
        self.assertEqual(WorkItem.objects.filter(farm=f).count(), 4)
        cm = Client()
        cm.force_login(mgr)
        self.assertEqual(cm.post("/terv/").status_code, 302)
        cv = Client()
        cv.force_login(vwr)
        self.assertEqual(cv.post("/terv/").status_code, 403)

    def test_isolation(self):
        ua, fa = owner_farm3("isoa")
        ub, fb = owner_farm3("isob")
        env(fa, "A")
        _, _, cb = env(fb, "B")
        ca = Client()
        ca.force_login(ua)
        ca.post("/terv/")
        body = ca.get("/terv/").content.decode()
        self.assertIn("A – ", body)
        self.assertNotIn("B – ", body)
