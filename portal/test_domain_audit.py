"""Domain audit + global farm selector tests."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import AuditEvent, CropCycle, Equipment, Farm, FarmLocation, FarmProperty, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="owner@example.com"):
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def make_location(farm, name="Loc"):
    return FarmLocation.objects.create(farm=farm, name=name, latitude="46.253000", longitude="20.141400", timezone="Europe/Budapest")


def make_property(farm, loc=None, name="Parcel", kind="field"):
    loc = loc or make_location(farm)
    return FarmProperty.objects.create(farm=farm, location=loc, name=name, kind=kind, area_hectares="5.00")


LOC = {"name": "Tanya", "latitude": "46.25", "longitude": "20.14", "timezone": "Europe/Budapest"}


def prop_payload(loc, name="P1"):
    return {"location": str(loc.pk), "name": name, "kind": "field", "area_hectares": "5.00", "notes": "SECRET-NOTE-XYZ"}


def equip_payload(name="Eke"):
    return {"name": name, "capabilities": ["tillage"], "available": "on", "hours_per_day": "6.0"}


def crop_payload(prop, name="Buza", state="current"):
    # Manual creates are plans and require a future sowing date.
    future = (date.today() + timedelta(days=30)).isoformat()
    return {"property": str(prop.pk), "crop_name": name, "state": state, "growth_stage": "",
            "sow_date": future, "expected_end_date": "", "observations": "SECRET-OBS-XYZ"}


def check_audit(test, farm, actor, action, obj_pk):
    ev = AuditEvent.objects.filter(farm=farm, action=action).order_by("-id").first()
    test.assertIsNotNone(ev, action)
    test.assertEqual(ev.actor_id, actor.pk)
    test.assertEqual(ev.object_id, str(obj_pk))
    test.assertTrue(ev.object_type, action)
    blob = str(ev.meta) + str(ev.action) + str(ev.object_type)
    test.assertNotIn("SECRET", blob)


class DomainAuditTests(TestCase):
    def test_crud_audits_no_raw_notes(self):
        u, f = owner_farm()
        c = Client()
        c.force_login(u)
        # location create/edit/delete
        r = c.post("/locations/new/", LOC)
        self.assertEqual(r.status_code, 302)
        loc = FarmLocation.objects.get(farm=f, name="Tanya")
        check_audit(self, f, u, "domain.location.create", loc.pk)
        r = c.post(f"/locations/{loc.pk}/edit/", {**LOC, "name": "Tanya2"})
        self.assertEqual(r.status_code, 302)
        check_audit(self, f, u, "domain.location.update", loc.pk)
        # property create/edit/delete
        r = c.post("/properties/new/", prop_payload(loc))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        prop = FarmProperty.objects.get(farm=f, name="P1")
        check_audit(self, f, u, "domain.property.create", prop.pk)
        r = c.post(f"/properties/{prop.pk}/edit/", prop_payload(loc, "P1"))
        self.assertEqual(r.status_code, 302)
        check_audit(self, f, u, "domain.property.update", prop.pk)
        # equipment create/edit/delete
        r = c.post("/equipment/new/", equip_payload())
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        eq = Equipment.objects.get(farm=f, name="Eke")
        check_audit(self, f, u, "domain.equipment.create", eq.pk)
        r = c.post(f"/equipment/{eq.pk}/edit/", equip_payload())
        self.assertEqual(r.status_code, 302)
        check_audit(self, f, u, "domain.equipment.update", eq.pk)
        # crop create/edit/observe/delete
        r = c.post("/cycles/new/", crop_payload(prop))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        cy = CropCycle.objects.get(farm=f, crop_name="Buza")
        check_audit(self, f, u, "domain.crop.create", cy.pk)
        r = c.post(f"/cycles/{cy.pk}/edit/", crop_payload(prop))
        self.assertEqual(r.status_code, 302)
        check_audit(self, f, u, "domain.crop.update", cy.pk)
        r = c.post(f"/cycles/{cy.pk}/observe/", {"observation": "my note"})
        self.assertEqual(r.status_code, 302)
        check_audit(self, f, u, "domain.crop.observe", cy.pk)
        # Planned delete is soft-hide and keeps blocking property delete;
        # convert to legacy current so this audit flow keeps testing physical delete.
        cy.state = "current"
        cy.save()
        for url in (f"/cycles/{cy.pk}/delete/", f"/equipment/{eq.pk}/delete/", f"/properties/{prop.pk}/delete/", f"/locations/{loc.pk}/delete/"):
            r = c.post(url, {})
            self.assertEqual(r.status_code, 302, url)
        for action, pk in (("domain.crop.delete", cy.pk), ("domain.equipment.delete", eq.pk), ("domain.property.delete", prop.pk), ("domain.location.delete", loc.pk)):
            check_audit(self, f, u, action, pk)

    def test_viewer_denied_no_audits(self):
        u, f = owner_farm()
        loc = make_location(f)
        prop = make_property(f, loc)
        cy = CropCycle.objects.create(farm=f, property=prop, crop_name="Buza", state="current")
        v = make_user("v@example.com")
        Membership.objects.create(farm=f, user=v, role="viewer", is_active=True)
        c = Client()
        c.force_login(v)
        before = AuditEvent.objects.filter(farm=f).count()
        self.assertEqual(c.post("/locations/new/", LOC).status_code, 403)
        self.assertEqual(c.post(f"/locations/{loc.pk}/edit/", {**LOC, "name": "X"}).status_code, 403)
        self.assertEqual(c.post(f"/locations/{loc.pk}/delete/", {}).status_code, 403)
        self.assertEqual(c.post("/properties/new/", prop_payload(loc, "PX")).status_code, 403)
        self.assertEqual(c.post("/equipment/new/", equip_payload()).status_code, 403)
        self.assertEqual(c.post("/cycles/new/", crop_payload(prop, state="planned")).status_code, 403)
        self.assertEqual(c.post(f"/cycles/{cy.pk}/observe/", {"observation": "x"}).status_code, 403)
        self.assertEqual(AuditEvent.objects.filter(farm=f).count(), before)

    def test_worker_observe_allowed(self):
        u, f = owner_farm()
        loc = make_location(f)
        prop = make_property(f, loc)
        cy = CropCycle.objects.create(farm=f, property=prop, crop_name="Buza", state="current")
        w = make_user("w@example.com")
        Membership.objects.create(farm=f, user=w, role="worker", is_active=True)
        c = Client()
        c.force_login(w)
        r = c.post(f"/cycles/{cy.pk}/observe/", {"observation": "worker note"})
        self.assertEqual(r.status_code, 302)
        check_audit(self, f, w, "domain.crop.observe", cy.pk)


class GlobalSelectorTests(TestCase):
    def setUp(self):
        self.u, self.f1 = owner_farm("s1@example.com")
        self.f1.name = "Alpha Farm"
        self.f1.save()
        self.o2 = make_user("s2@example.com")
        self.f2 = Farm.objects.create(owner=self.o2, name="Beta Farm")
        Membership.objects.create(farm=self.f2, user=self.u, role="manager", is_active=True)
        self.c = Client()
        self.c.force_login(self.u)

    def assert_selector(self, url):
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200, url)
        body = r.content.decode()
        self.assertIn("aktiv-farm", body, url)
        self.assertIn("csrfmiddlewaretoken", body, url)
        self.assertIn("Alpha Farm", body, url)
        self.assertIn("Beta Farm", body, url)
        return r

    def test_selector_on_dashboard_chat_plans(self):
        for url in ("/attekintes/", "/chat/", "/terv/"):
            self.assert_selector(url)
        # switch works, only authorized farms leak
        r = self.c.post("/aktiv-farm/", {"farm_id": self.f2.id})
        self.assertEqual(r.status_code, 302)
        r = self.c.get("/attekintes/")
        self.assertIn("Beta Farm", r.content.decode())
        other = make_user("other@example.com")
        fo = Farm.objects.create(owner=other, name="Hidden Farm")
        r = self.c.get("/attekintes/")
        self.assertNotIn("Hidden Farm", r.content.decode())

    def test_stale_active_recovers(self):
        s = self.c.session
        s["active_farm_id"] = 999999
        s.save()
        for url in ("/attekintes/", "/fiok/"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("aktiv-farm", r.content.decode(), url)
