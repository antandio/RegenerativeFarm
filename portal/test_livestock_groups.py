"""Task4 slice A RED: usable livestock group foundation (no stubs).

Covers: species choices + custom label, groups CRUD/archive preserving
history with initial count nonnegative, housing via tenant Property,
group current location and notes, tenant scope, owner/manager write,
viewer readonly, worker no group write (existing domain capability).
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import Farm, FarmLocation, FarmProperty, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="ls-owner@example.com"):
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def make_location(farm, name="Loc"):
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude="46.253000",
        longitude="20.141400", timezone="Europe/Budapest")


def make_property(farm, loc=None, name="Parcel", kind="field"):
    loc = loc or make_location(farm, name="L-" + name)
    return FarmProperty.objects.create(
        farm=farm, location=loc, name=name, kind=kind, area_hectares="5.00")


def add_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def login(client, user):
    client.force_login(user)


class LivestockGroupModelTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)

    def test_create_group_with_species_choice_and_initial_count(self):
        from portal.models import LivestockGroup
        g = LivestockGroup.objects.create(
            farm=self.f, name="Fejős csoport", species="sheep",
            breed_label="Merinó", headcount=10,
            housing_property=self.prop, notes="Legelőn tartott.")
        self.assertEqual(g.headcount, 10)
        self.assertEqual(g.species, "sheep")
        self.assertEqual(g.housing_property_id, self.prop.pk)

    def test_negative_initial_count_rejected(self):
        from portal.models import LivestockGroup
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            LivestockGroup.objects.create(
                farm=self.f, name="Neg", species="cattle", headcount=-1)

    def test_cross_farm_housing_property_rejected(self):
        from portal.models import LivestockGroup
        from django.core.exceptions import ValidationError
        _, other = owner_farm("ls-other@example.com")
        foreign_prop = make_property(other, name="Foreign")
        with self.assertRaises(ValidationError):
            LivestockGroup.objects.create(
                farm=self.f, name="X", species="goat", headcount=2,
                housing_property=foreign_prop)

    def test_name_unique_per_farm(self):
        from portal.models import LivestockGroup
        LivestockGroup.objects.create(
            farm=self.f, name="Dupla", species="pig", headcount=1)
        from django.core.exceptions import ValidationError as VE
        from django.db import IntegrityError
        with self.assertRaises((IntegrityError, VE)):
            LivestockGroup.objects.create(
                farm=self.f, name="Dupla", species="pig", headcount=1)

    def test_barn_and_pasture_property_kinds_accepted(self):
        from portal.domain_models import PROPERTY_KIND_CHOICES
        kinds = dict(PROPERTY_KIND_CHOICES)
        self.assertIn("barn", kinds)
        self.assertIn("pasture", kinds)


class LivestockGroupViewTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.c = Client()

    def test_list_and_detail_links_visible(self):
        from portal.models import LivestockGroup
        login(self.c, self.u)
        g = LivestockGroup.objects.create(
            farm=self.f, name="Fejős", species="sheep", headcount=10,
            housing_property=self.prop)
        r = self.c.get("/livestock/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Fejős")
        r = self.c.get(f"/livestock/{g.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Fejős")

    def test_tenant_isolation_other_farm_404(self):
        from portal.models import LivestockGroup
        login(self.c, self.u)
        g = LivestockGroup.objects.create(
            farm=self.f, name="Titkos", species="goat", headcount=3)
        u2, f2 = owner_farm("ls-second@example.com")
        c2 = Client()
        login(c2, u2)
        self.assertEqual(c2.get(f"/livestock/{g.pk}/").status_code, 404)
        r = c2.get("/livestock/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Titkos")

    def test_roles_viewer_readonly_worker_no_write(self):
        from portal.models import LivestockGroup
        viewer = add_member(self.f, "ls-viewer@example.com", "viewer")
        worker = add_member(self.f, "ls-worker@example.com", "worker")
        manager = add_member(self.f, "ls-manager@example.com", "manager")
        g = LivestockGroup.objects.create(
            farm=self.f, name="Nyáj", species="sheep", headcount=5)
        for user, code in ((viewer, 200), (worker, 200)):
            c = Client()
            login(c, user)
            self.assertEqual(c.get("/livestock/").status_code, code)
        payload = {"name": "Próba", "species": "sheep", "headcount": "2",
                   "housing_property": "", "breed_label": "", "notes": ""}
        for user in (viewer, worker):
            c = Client()
            login(c, user)
            self.assertIn(c.post("/livestock/new/", payload).status_code, (403, 404))
        c = Client()
        login(c, manager)
        r = c.post("/livestock/new/", dict(payload, name="Menyét"))
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(LivestockGroup.objects.filter(
            farm=self.f, name="Menyét").exists())
        _ = g

    def test_archive_preserves_history(self):
        from portal.models import LivestockGroup
        login(self.c, self.u)
        g = LivestockGroup.objects.create(
            farm=self.f, name="Arch", species="cattle", headcount=4)
        r = self.c.post(f"/livestock/{g.pk}/archive/")
        self.assertIn(r.status_code, (302, 303))
        g.refresh_from_db()
        self.assertFalse(g.is_active)
        # Archived group still has a detail page (history preserved).
        self.assertEqual(self.c.get(f"/livestock/{g.pk}/").status_code, 200)

    def test_sidebar_link_present_hu_en(self):
        login(self.c, self.u)
        r = self.c.get("/livestock/")
        self.assertEqual(r.status_code, 200)
        # Sidebar is rendered via base.html on every page; check dashboard too.
        r = self.c.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "llat")
