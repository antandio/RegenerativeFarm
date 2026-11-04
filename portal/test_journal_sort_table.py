"""Task3 RED slice: sortable accessible journal table (server-side sort allowlist)."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import Farm, FarmLocation, FarmProperty, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="sort3-owner@example.com"):
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


class JournalSortTableTests(TestCase):
    def setUp(self):
        from portal.models import FarmingEvent  # noqa
        self.owner, self.farm = owner_farm()
        self.loc = make_location(self.farm)
        self.prop_a = make_property(self.farm, self.loc, name="SortA")
        self.prop_b = make_property(self.farm, self.loc, name="SortB")
        self.manager = add_member(self.farm, "sort3-manager@example.com", "manager")
        self.viewer = add_member(self.farm, "sort3-viewer@example.com", "viewer")

    def login(self, u):
        c = Client()
        c.force_login(u)
        return c

    def _add(self, prop, day, desc, etype="sowing"):
        from portal.models import FarmingEvent
        return FarmingEvent.objects.create(
            farm=self.farm, property=prop, event_date=day,
            event_type=etype, description=desc,
            created_by=self.owner, updated_by=self.owner)

    def test_sort_allowlist_property_desc_and_injection_fallback(self):
        self._add(self.prop_a, date(2024, 1, 1), "EV-A")
        self._add(self.prop_b, date(2024, 1, 2), "EV-B")
        c = self.login(self.owner)
        r = c.get("/events/", {"sort": "property", "dir": "desc"})
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertEqual(rows, ["EV-B", "EV-A"])
        # injection / dunder sort falls back to chronological default, still 200
        for bad in ["description;DROP", "__class__", "event_date"]:
            r = c.get("/events/", {"sort": bad})
            self.assertEqual(r.status_code, 200)
            rows = [e.description for e in r.context["events"]]
            self.assertEqual(rows, ["EV-A", "EV-B"])

    def test_sort_title_and_unknown_dir_fallback_stable_tie(self):
        e1 = self._add(self.prop_a, date(2024, 3, 1), "AAA-first")
        e2 = self._add(self.prop_a, date(2024, 3, 1), "ZZZ-second")
        c = self.login(self.owner)
        # default chronological keeps pk tie-break on same date
        rows = [e.description for e in c.get("/events/").context["events"]]
        self.assertEqual(rows, ["AAA-first", "ZZZ-second"])
        self.assertLess(e1.pk, e2.pk)
        # title asc/desc sorts by description
        rows = [e.description for e in c.get("/events/", {"sort": "title", "dir": "asc"}).context["events"]]
        self.assertEqual(rows, ["AAA-first", "ZZZ-second"])
        rows = [e.description for e in c.get("/events/", {"sort": "title", "dir": "desc"}).context["events"]]
        self.assertEqual(rows, ["ZZZ-second", "AAA-first"])
        # unknown dir falls back to asc
        rows = [e.description for e in c.get("/events/", {"sort": "title", "dir": "sideways"}).context["events"]]
        self.assertEqual(rows, ["AAA-first", "ZZZ-second"])

    def test_sort_preserves_filters_and_pagination(self):
        for i in range(21):
            self._add(self.prop_a, date(2024, 5, 1), "PG-%02d" % i, etype="sowing")
        c = self.login(self.owner)
        r = c.get("/events/", {"type": "sowing", "sort": "title", "dir": "desc"})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        # header sort links keep the active type filter
        self.assertIn("type=sowing", html)
        self.assertIn("sort=title", html)
        # page-2 link keeps both filter and sort
        self.assertIn("page=2", html)
        self.assertIn("type=sowing", html.split("page=2")[0][-500:] + html.split("page=2")[1][:500])

    def test_table_a11y_and_role_controls(self):
        self._add(self.prop_a, date(2024, 6, 1), "AX-1")
        e = self._add(self.prop_b, date(2024, 6, 2), "AX-2")
        body_manager = self.login(self.manager).get("/events/").content.decode()
        self.assertIn("<table", body_manager)
        self.assertIn("<caption", body_manager)
        self.assertIn('scope="col"', body_manager)
        self.assertIn("aria-sort", body_manager)
        self.assertIn(f"/events/{e.pk}/edit/", body_manager)
        self.assertIn(f"/events/{e.pk}/delete/", body_manager)
        body_viewer = self.login(self.viewer).get("/events/").content.decode()
        self.assertIn("<table", body_viewer)
        self.assertNotIn(f"/events/{e.pk}/edit/", body_viewer)
        self.assertNotIn(f"/events/{e.pk}/delete/", body_viewer)
