"""Task4 slice C RED: manual livestock planned work (grazing/feeding/health).

Covers: WorkItem.livestock_group tenant-validated manual items
(create/edit), negative chronology, permission matrix (all roles),
plan-regenerate preservation, done -> actual LivestockEvent sync
(explicit event form when not existing), reachable edit/archive/
event/reversal controls.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import Farm, FarmLocation, FarmProperty, LivestockGroup, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="lw-owner@example.com"):
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def make_location(farm, name="Loc"):
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude="46.253000",
        longitude="20.141400", timezone="Europe/Budapest")


def make_property(farm, loc=None, name="Parcel", kind="pasture"):
    loc = loc or make_location(farm, name="L-" + name)
    return FarmProperty.objects.create(
        farm=farm, location=loc, name=name, kind=kind, area_hectares="5.00")


def add_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def login(client, user):
    client.force_login(user)


def make_group(farm, name="Nyaj", species="sheep", headcount=10, prop=None):
    return LivestockGroup.objects.create(
        farm=farm, name=name, species=species, headcount=headcount,
        housing_property=prop)


def work_payload(**over):
    d = {"livestock_kind": "grazing", "title": "Legeltetes",
         "start_date": "2024-04-01", "end_date": "2024-04-02",
         "estimated_hours": "2.0", "property": "", "notes": ""}
    d.update(over)
    return d


class LivestockWorkModelTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)

    def test_manual_livestock_item_tenant_validated(self):
        from portal.models import WorkItem
        w = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="grazing",
            title="Legeltetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 2),
            estimated_hours="2.0")
        self.assertEqual(w.livestock_group_id, self.g.pk)
        self.assertEqual(w.status, "open")

    def test_forged_group_and_property_rejected(self):
        from django.core.exceptions import ValidationError
        from portal.models import WorkItem
        u2, f2 = owner_farm("lw-other@example.com")
        p2 = make_property(f2, name="Masik")
        g2 = make_group(f2, name="MasikNyaj")
        with self.assertRaises(ValidationError):
            WorkItem.objects.create(
                farm=self.f, crop_cycle=None, property=self.prop,
                livestock_group=g2, livestock_kind="grazing",
                title="Hamis", capability="other",
                start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))
        with self.assertRaises(ValidationError):
            WorkItem.objects.create(
                farm=self.f, crop_cycle=None, property=p2,
                livestock_group=self.g, livestock_kind="feeding",
                title="Hamis2", capability="other",
                start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))

    def test_negative_chronology_rejected(self):
        from django.core.exceptions import ValidationError
        from portal.models import WorkItem
        with self.assertRaises(ValidationError):
            WorkItem.objects.create(
                farm=self.f, crop_cycle=None, property=self.prop,
                livestock_group=self.g, livestock_kind="health",
                title="Rossz", capability="other",
                start_date=date(2024, 4, 5), end_date=date(2024, 4, 2))

    def test_mixed_crop_and_livestock_rejected(self):
        from django.core.exceptions import ValidationError
        from portal.models import CropCycle, WorkItem
        field = make_property(self.f, name="Szanto", kind="field")
        cyc = CropCycle.objects.create(
            farm=self.f, property=field, crop_name="Buza", state="current")
        with self.assertRaises(ValidationError):
            WorkItem.objects.create(
                farm=self.f, crop_cycle=cyc, property=self.prop,
                livestock_group=self.g, livestock_kind="grazing",
                title="Kevert", capability="other",
                start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))


class LivestockWorkViewTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)
        self.c = Client()

    def test_permission_matrix_all_roles(self):
        from portal.models import WorkItem
        viewer = add_member(self.f, "lw-viewer@example.com", "viewer")
        worker = add_member(self.f, "lw-worker@example.com", "worker")
        manager = add_member(self.f, "lw-manager@example.com", "manager")
        url = f"/livestock/{self.g.pk}/work/new/"
        for user in (viewer, worker):
            c = Client()
            login(c, user)
            self.assertIn(c.post(url, work_payload()).status_code, (403, 404))
        c = Client()
        login(c, manager)
        r = c.post(url, work_payload(title="Menedzser munka"))
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(WorkItem.objects.filter(
            farm=self.f, title="Menedzser munka").exists())
        w = WorkItem.objects.get(farm=self.f, title="Menedzser munka")
        # Worker cannot edit, but can mark done; viewer can do neither.
        c = Client()
        login(c, worker)
        self.assertIn(
            c.post(f"/livestock/work/{w.pk}/edit/", work_payload(title="X")).status_code,
            (403, 404))
        r = c.post(f"/livestock/work/{w.pk}/done/", {"event_date": "2024-04-02"})
        self.assertIn(r.status_code, (302, 303))
        c = Client()
        login(c, viewer)
        self.assertIn(
            c.post(f"/livestock/work/{w.pk}/done/", {"event_date": "2024-04-02"}).status_code,
            (403, 404))

    def test_planner_regenerate_preserves_manual(self):
        from portal.models import CropCycle, WorkItem
        login(self.c, self.u)
        loc = make_location(self.f, name="Cyc-Loc")
        prop = make_property(self.f, loc=loc, name="Tabla", kind="field")
        cyc = CropCycle.objects.create(
            farm=self.f, property=prop, crop_name="Buza", state="current")
        auto = WorkItem.objects.create(
            farm=self.f, crop_cycle=cyc, property=prop, title="Auto regi",
            capability="other", start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1))
        manual = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="feeding",
            title="Kezi etetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 1))
        r = self.c.post("/terv/")
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(WorkItem.objects.filter(pk=manual.pk).exists())
        self.assertFalse(WorkItem.objects.filter(pk=auto.pk).exists())

    def test_done_sync_creates_event_once(self):
        from portal.models import LivestockEvent, WorkItem
        login(self.c, self.u)
        w = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="grazing",
            title="Legeltetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))
        # Explicit event form is shown when nothing is linked yet.
        r = self.c.get(f"/livestock/work/{w.pk}/done/")
        self.assertEqual(r.status_code, 200)
        r = self.c.post(f"/livestock/work/{w.pk}/done/", {"event_date": "2024-04-02"})
        self.assertIn(r.status_code, (302, 303))
        w.refresh_from_db()
        self.assertEqual(w.status, "done")
        self.assertIsNotNone(w.completed_event_id)
        ev = LivestockEvent.objects.get(pk=w.completed_event_id)
        self.assertEqual(ev.group_id, self.g.pk)
        self.assertEqual(ev.kind, "grazing")
        n = LivestockEvent.objects.filter(group=self.g).count()
        r = self.c.post(f"/livestock/work/{w.pk}/done/", {"event_date": "2024-04-02"})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(LivestockEvent.objects.filter(group=self.g).count(), n)

    def test_reachable_controls(self):
        from portal.models import LivestockEvent, WorkItem
        login(self.c, self.u)
        LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                              kind="grazing", headcount_delta=0, property=self.prop)
        w = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="grazing",
            title="Legeltetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))
        r = self.c.get(f"/livestock/{self.g.pk}/")
        self.assertEqual(r.status_code, 200)
        for needle in (f"/livestock/{self.g.pk}/edit/",
                       f"/livestock/{self.g.pk}/archive/",
                       f"/livestock/{self.g.pk}/events/new/",
                       "/livestock/events/",
                       f"/livestock/{self.g.pk}/work/new/",
                       f"/livestock/work/{w.pk}/edit/",
                       f"/livestock/work/{w.pk}/done/"):
            self.assertContains(r, needle)
