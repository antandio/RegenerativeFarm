"""Usability TDD: list pages expose same-farm edit/delete hrefs, crop observe href for worker, no write controls for viewer; row details shown."""
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from portal.models import Farm, FarmLocation, FarmProperty, Equipment, CropCycle, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def seed(farm):
    loc = FarmLocation.objects.create(farm=farm, name="Tanya", latitude="46.250000", longitude="20.140000", timezone="Europe/Budapest")
    prop = FarmProperty.objects.create(farm=farm, location=loc, name="A parcella", kind="field", area_hectares="5.00")
    eq = Equipment.objects.create(farm=farm, name="Eke", capabilities=["tillage"], available=True, hours_per_day="6.0")
    cyc = CropCycle.objects.create(farm=farm, property=prop, crop_name="Buza", state="current", growth_stage="szarba", observations="eszlelt")
    return loc, prop, eq, cyc


def owner_client(email="own-u@example.com"):
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    c = Client()
    c.force_login(u)
    return u, f, c


class ListUsabilityTests(TestCase):
    def test_owner_lists_show_details_and_row_links(self):
        u, f, c = owner_client("own-u1@example.com")
        loc, prop, eq, cyc = seed(f)
        r = c.get("/locations/")
        b = r.content.decode()
        self.assertIn("Europe/Budapest", b)
        self.assertTrue("46,25" in b or "46.25" in b, b[:2000])
        self.assertIn(f"/locations/{loc.pk}/edit/", b)
        self.assertIn(f"/locations/{loc.pk}/delete/", b)
        self.assertIn("/locations/new/", b)
        r = c.get("/properties/")
        b = r.content.decode()
        self.assertIn("A parcella", b)
        self.assertIn("Tanya", b)
        self.assertTrue("5.00" in b or "5,00" in b, b[:2000])
        self.assertIn(f"/properties/{prop.pk}/edit/", b)
        self.assertIn(f"/properties/{prop.pk}/delete/", b)
        r = c.get("/equipment/")
        b = r.content.decode()
        self.assertIn("Eke", b)
        self.assertIn(f"/equipment/{eq.pk}/edit/", b)
        self.assertIn(f"/equipment/{eq.pk}/delete/", b)
        r = c.get("/cycles/")
        b = r.content.decode()
        self.assertIn("Buza", b)
        self.assertIn("szarba", b)
        self.assertIn("eszlelt", b)
        self.assertIn(f"/cycles/{cyc.pk}/edit/", b)
        self.assertIn(f"/cycles/{cyc.pk}/delete/", b)
        self.assertIn(f"/cycles/{cyc.pk}/observe/", b)

    def test_same_farm_isolation_negative_ids(self):
        u, f, c = owner_client("own-u2@example.com")
        loc, prop, eq, cyc = seed(f)
        u2 = make_user("own-u2b@example.com")
        f2 = Farm.objects.create(owner=u2, name="Farm B")
        c2 = Client()
        c2.force_login(u2)
        for url in ("/locations/", "/properties/", "/equipment/", "/cycles/"):
            b = c2.get(url).content.decode()
            self.assertNotIn("Tanya", b)
            self.assertNotIn("Buza", b)
            self.assertNotIn(f"/locations/{loc.pk}/edit/", b)
            self.assertNotIn(f"/cycles/{cyc.pk}/edit/", b)
        for url in (f"/locations/{loc.pk}/edit/", f"/properties/{prop.pk}/delete/", f"/equipment/{eq.pk}/edit/", f"/cycles/{cyc.pk}/observe/"):
            self.assertEqual(c2.get(url).status_code, 404, url)

    def test_worker_cycle_observe_visible_no_write(self):
        u, f, c = owner_client("own-u3@example.com")
        loc, prop, eq, cyc = seed(f)
        w = make_user("worker-u3@example.com")
        Membership.objects.create(farm=f, user=w, role="worker", is_active=True)
        cw = Client()
        cw.force_login(w)
        b = cw.get("/cycles/").content.decode()
        self.assertIn(f"/cycles/{cyc.pk}/observe/", b)
        self.assertNotIn(f"/cycles/{cyc.pk}/edit/", b)
        self.assertNotIn(f"/cycles/{cyc.pk}/delete/", b)
        self.assertNotIn("cycle_new", b)

    def test_viewer_no_write_controls(self):
        u, f, c = owner_client("own-u4@example.com")
        loc, prop, eq, cyc = seed(f)
        v = make_user("viewer-u4@example.com")
        Membership.objects.create(farm=f, user=v, role="viewer", is_active=True)
        cv = Client()
        cv.force_login(v)
        for url in ("/locations/", "/properties/", "/equipment/", "/cycles/"):
            b = cv.get(url).content.decode()
            self.assertEqual(b.count("/edit/"), 0, url)
            self.assertEqual(b.count("/delete/"), 0, url)
            self.assertNotIn("_new", b)
        b = cv.get("/cycles/").content.decode()
        self.assertNotIn(f"/cycles/{cyc.pk}/observe/", b)

    def test_role_context_present(self):
        u, f, c = owner_client("own-u5@example.com")
        seed(f)
        r = c.get("/cycles/")
        self.assertTrue(r.context["can_manage"])
        self.assertTrue(r.context["can_observe"])
        self.assertEqual(r.context["role"], "owner")

    def test_cycle_form_date_widgets(self):
        u, f, c = owner_client("own-u6@example.com")
        b = c.get("/cycles/new/").content.decode()
        self.assertIn('type="date"', b)
