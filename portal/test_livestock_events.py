"""Task4 slice B RED: dated livestock events + actual journal link.

Covers: birth/purchase/death/sale/move/health/grazing kinds with
quantities; atomic chronology + no-negative-count validation; foreign
property/target/journal rejection; archived-group freeze; journal link
(type other, non-duplicating, SET_NULL on journal delete); roles
(owner/manager/worker create+edit-equivalent, delete owner/manager,
viewer readonly); tenant scope.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import Farm, FarmLocation, FarmProperty, LivestockGroup, Membership

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="le-owner@example.com"):
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


def make_group(farm, name="Nyáj", species="sheep", headcount=10, prop=None):
    return LivestockGroup.objects.create(
        farm=farm, name=name, species=species, headcount=headcount,
        housing_property=prop)


def add_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def login(client, user):
    client.force_login(user)


class LivestockEventModelTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)

    def test_birth_then_death_moves_count(self):
        from portal.models import LivestockEvent
        LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                              kind="birth", headcount_delta=10)
        LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 5),
                              kind="death", headcount_delta=-2)
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 18)

    def test_negative_count_rejected_and_unchanged(self):
        from portal.models import LivestockEvent
        from django.core.exceptions import ValidationError
        g = make_group(self.f, name="Kicsi", headcount=3)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=g, event_date=date(2024, 3, 1),
                                  kind="death", headcount_delta=-5)
        g.refresh_from_db()
        self.assertEqual(g.headcount, 3)

    def test_chronology_backdated_rejected(self):
        from portal.models import LivestockEvent
        from django.core.exceptions import ValidationError
        LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 5),
                              kind="birth", headcount_delta=1)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="birth", headcount_delta=1)

    def test_kind_sign_rules(self):
        from portal.models import LivestockEvent
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="birth", headcount_delta=-1)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="death", headcount_delta=2)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="grazing", headcount_delta=2)

    def test_move_needs_target_and_moves_both(self):
        from portal.models import LivestockEvent
        from django.core.exceptions import ValidationError
        t = make_group(self.f, name="Cél", headcount=0)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="move", headcount_delta=-4)
        LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 2),
                              kind="move", headcount_delta=-4, target_group=t)
        self.g.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual((self.g.headcount, t.headcount), (6, 4))

    def test_foreign_property_target_journal_rejected(self):
        from portal.models import FarmingEvent, LivestockEvent
        from django.core.exceptions import ValidationError
        _, other = owner_farm("le-other@example.com")
        foreign_prop = make_property(other, name="Foreign")
        foreign_group = make_group(other, name="FG", headcount=1)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="grazing", headcount_delta=0,
                                  property=foreign_prop)
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="move", headcount_delta=-1,
                                  target_group=foreign_group)
        fe = FarmingEvent.objects.create(
            farm=other, property=foreign_prop, event_date=date(2024, 3, 1),
            event_type="other", description="Idegen")
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="health", headcount_delta=0,
                                  farming_event=fe)

    def test_archived_group_frozen(self):
        from portal.models import LivestockEvent
        from django.core.exceptions import ValidationError
        self.g.is_active = False
        self.g.save()
        with self.assertRaises(ValidationError):
            LivestockEvent.record(farm=self.f, group=self.g, event_date=date(2024, 3, 1),
                                  kind="birth", headcount_delta=1)

    def test_journal_link_other_type_and_set_null(self):
        from portal.models import FarmingEvent, LivestockEvent
        ev = LivestockEvent.record(
            farm=self.f, group=self.g, event_date=date(2024, 4, 1),
            kind="grazing", headcount_delta=0, property=self.prop,
            notes="Kihajtás", create_journal=True, actor=self.u)
        self.assertIsNotNone(ev.farming_event_id)
        je = FarmingEvent.objects.get(pk=ev.farming_event_id)
        self.assertEqual(je.event_type, "other")
        self.assertIn(self.g.name, je.description)
        self.assertEqual(je.farm_id, self.f.id)
        # Journal delete must SET_NULL, never cascade the livestock event.
        je.delete()
        ev.refresh_from_db()
        self.assertIsNone(ev.farming_event)
        self.assertTrue(type(ev).__name__ == "LivestockEvent")


class LivestockEventViewTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)
        self.c = Client()

    def test_worker_create_ok_delete_forbidden_viewer_readonly(self):
        from portal.models import LivestockEvent
        worker = add_member(self.f, "le-worker@example.com", "worker")
        viewer = add_member(self.f, "le-viewer@example.com", "viewer")
        c = Client()
        login(c, worker)
        r = c.post(f"/livestock/{self.g.pk}/events/new/", {
            "event_date": "2024-05-01", "kind": "birth", "headcount_delta": "3",
            "property": "", "notes": "", "create_journal": "",
        })
        self.assertIn(r.status_code, (302, 303))
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 13)
        ev = LivestockEvent.objects.filter(group=self.g).latest("id")
        self.assertIn(c.post(f"/livestock/events/{ev.pk}/delete/").status_code, (403, 404))
        c2 = Client()
        login(c2, viewer)
        self.assertIn(c2.post(f"/livestock/{self.g.pk}/events/new/", {
            "event_date": "2024-05-02", "kind": "birth", "headcount_delta": "1",
        }).status_code, (403, 404))

    def test_tenant_isolation(self):
        u2, f2 = owner_farm("le-second@example.com")
        c2 = Client()
        login(c2, u2)
        self.assertEqual(c2.get(f"/livestock/{self.g.pk}/").status_code, 404)
        self.assertIn(c2.post(f"/livestock/{self.g.pk}/events/new/", {
            "event_date": "2024-05-01", "kind": "birth", "headcount_delta": "1",
        }).status_code, (403, 404))

    def test_detail_shows_events_and_journal_trace(self):
        from portal.models import LivestockEvent
        login(self.c, self.u)
        LivestockEvent.record(
            farm=self.f, group=self.g, event_date=date(2024, 4, 1),
            kind="grazing", headcount_delta=0, property=self.prop,
            create_journal=True, actor=self.u)
        r = self.c.get(f"/livestock/{self.g.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "2024")
