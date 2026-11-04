"""Stage A RED tracer: historical farming-event journal (actuals, not plans)."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase

from portal.models import AuditEvent, CropCycle, Farm, FarmLocation, FarmProperty, Membership, WorkItem

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="event-owner@example.com"):
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


def make_cycle(farm, prop, name="Buza", state="current"):
    return CropCycle.objects.create(
        farm=farm, property=prop, crop_name=name, state=state)


def add_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def event_payload(prop, cycle=None, **over):
    d = {
        "property": str(prop.pk),
        "crop_cycle": str(cycle.pk) if cycle else "",
        "event_date": "1998-04-12",
        "event_type": "sowing",
        "description": "Tavaszi vetés",
        "quantity": "",
        "unit": "",
        "materials": "",
        "treated_area_hectares": "",
        "notes": "",
    }
    d.update(over)
    return d


class FarmingEventModelTests(TestCase):
    def setUp(self):
        from portal.models import FarmingEvent  # noqa
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)

    def test_tracer_create_and_chronological_order(self):
        from portal.models import FarmingEvent
        FarmingEvent.objects.create(
            farm=self.f, property=self.prop, event_date=date(2020, 5, 1),
            event_type="harvesting", description="Aratás",
            created_by=self.u, updated_by=self.u)
        FarmingEvent.objects.create(
            farm=self.f, property=self.prop, event_date=date(1998, 4, 12),
            event_type="sowing", description="Régi vetés",
            created_by=self.u, updated_by=self.u)
        got = list(FarmingEvent.objects.filter(farm=self.f))
        self.assertEqual([e.event_date for e in got], [date(1998, 4, 12), date(2020, 5, 1)])
        self.assertEqual(WorkItem.objects.filter(farm=self.f).count(), 0)

    def test_all_eight_types_and_actor_timestamps(self):
        from portal.models import FarmingEvent
        types = ["sowing", "spraying", "fertilization", "irrigation",
                 "harvesting", "tillage", "weeding_pest", "other"]
        for t in types:
            e = FarmingEvent.objects.create(
                farm=self.f, property=self.prop, event_date=date(2010, 1, 2),
                event_type=t, description="D " + t,
                created_by=self.u, updated_by=self.u)
            self.assertIsNotNone(e.created_at)
            self.assertIsNotNone(e.updated_at)
            self.assertEqual(e.created_by_id, self.u.pk)
        self.assertEqual(FarmingEvent.objects.filter(farm=self.f).count(), 8)

    def test_optional_cycle_and_matching(self):
        from portal.models import FarmingEvent
        cyc = make_cycle(self.f, self.prop)
        e = FarmingEvent.objects.create(
            farm=self.f, property=self.prop, crop_cycle=cyc,
            event_date=date(2015, 6, 1), event_type="irrigation",
            description="Öntözés", created_by=self.u, updated_by=self.u)
        self.assertEqual(e.crop_cycle_id, cyc.pk)

    def test_cross_farm_property_rejected(self):
        from portal.models import FarmingEvent
        u2, f2 = owner_farm("other@example.com")
        p2 = make_property(f2)
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=p2, event_date=date(2015, 6, 1),
                         event_type="sowing", description="X").full_clean()

    def test_cross_farm_cycle_and_other_property_cycle_rejected(self):
        from portal.models import FarmingEvent
        u2, f2 = owner_farm("other2@example.com")
        p2 = make_property(f2)
        c2 = make_cycle(f2, p2)
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=self.prop, crop_cycle=c2,
                         event_date=date(2015, 6, 1), event_type="sowing",
                         description="X").full_clean()
        other_prop = make_property(self.f, name="P2")
        c3 = make_cycle(self.f, other_prop, state="history")
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=self.prop, crop_cycle=c3,
                         event_date=date(2015, 6, 1), event_type="sowing",
                         description="X").full_clean()

    def test_quantity_rules_and_negative_area(self):
        from portal.models import FarmingEvent
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=self.prop, event_date=date(2015, 6, 1),
                         event_type="spraying", description="X",
                         quantity=Decimal("1.5"), unit="").full_clean()
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=self.prop, event_date=date(2015, 6, 1),
                         event_type="spraying", description="X",
                         quantity=Decimal("-1"), unit="kg").full_clean()
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=self.prop, event_date=date(2015, 6, 1),
                         event_type="spraying", description="X",
                         treated_area_hectares=Decimal("-2")).full_clean()
        with self.assertRaises(ValidationError):
            FarmingEvent(farm=self.f, property=self.prop, event_date=date(2015, 6, 1),
                         event_type="nope", description="X").full_clean()

    def test_property_protect_and_cycle_set_null(self):
        from portal.models import FarmingEvent
        cyc = make_cycle(self.f, self.prop)
        e = FarmingEvent.objects.create(
            farm=self.f, property=self.prop, crop_cycle=cyc,
            event_date=date(2015, 6, 1), event_type="tillage",
            description="Szántás", created_by=self.u, updated_by=self.u)
        with self.assertRaises(ProtectedError):
            self.prop.delete()
        cyc.delete()
        e.refresh_from_db()
        self.assertIsNone(e.crop_cycle)


class FarmingEventViewTests(TestCase):
    def setUp(self):
        self.owner, self.farm = owner_farm()
        self.loc = make_location(self.farm)
        self.prop = make_property(self.farm, self.loc, name="P1")
        self.prop2 = make_property(self.farm, self.loc, name="P2")
        self.cycle = make_cycle(self.farm, self.prop)
        self.worker = add_member(self.farm, "worker@example.com", "worker")
        self.viewer = add_member(self.farm, "viewer@example.com", "viewer")
        self.manager = add_member(self.farm, "manager@example.com", "manager")
        self.o2, self.f2 = owner_farm("o2@example.com")
        self.p_o2 = make_property(self.f2, name="OP")

    def login(self, u):
        c = Client()
        c.force_login(u)
        return c

    def create(self, c, **over):
        return c.post("/events/new/", event_payload(self.prop, self.cycle, **over))

    def test_owner_crud_and_audit(self):
        from portal.models import FarmingEvent
        c = self.login(self.owner)
        r = c.get("/events/")
        self.assertEqual(r.status_code, 200)
        r = self.create(c)
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        e = FarmingEvent.objects.get(farm=self.farm)
        self.assertEqual(e.created_by_id, self.owner.pk)
        ev = AuditEvent.objects.filter(farm=self.farm, action="domain.event.create").first()
        self.assertIsNotNone(ev)
        self.assertNotIn("SECRET", str(ev.meta) + str(ev.action))
        r = c.post(f"/events/{e.pk}/edit/", event_payload(self.prop, self.cycle, description="Módosítva"))
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.description, "Módosítva")
        self.assertEqual(e.updated_by_id, self.owner.pk)
        self.assertTrue(AuditEvent.objects.filter(farm=self.farm, action="domain.event.update").exists())
        r = c.post(f"/events/{e.pk}/delete/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(FarmingEvent.objects.filter(pk=e.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(farm=self.farm, action="domain.event.delete").exists())

    def test_manager_full_and_worker_create_edit_only(self):
        from portal.models import FarmingEvent
        c = self.login(self.manager)
        r = self.create(c)
        self.assertEqual(r.status_code, 302)
        e = FarmingEvent.objects.get(farm=self.farm)
        r = c.post(f"/events/{e.pk}/delete/")
        self.assertEqual(r.status_code, 302)
        cw = self.login(self.worker)
        r = self.create(cw)
        self.assertEqual(r.status_code, 302)
        ew = FarmingEvent.objects.get(farm=self.farm)
        r = cw.post(f"/events/{ew.pk}/edit/", event_payload(self.prop, self.cycle, description="W"))
        self.assertEqual(r.status_code, 302)
        n_audit = AuditEvent.objects.filter(farm=self.farm).count()
        r = cw.post(f"/events/{ew.pk}/delete/")
        self.assertEqual(r.status_code, 403)
        self.assertTrue(FarmingEvent.objects.filter(pk=ew.pk).exists())
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), n_audit)

    def test_viewer_read_only_no_change(self):
        from portal.models import FarmingEvent
        c = self.login(self.viewer)
        r = c.get("/events/")
        self.assertEqual(r.status_code, 200)
        n0 = FarmingEvent.objects.filter(farm=self.farm).count()
        a0 = AuditEvent.objects.filter(farm=self.farm).count()
        for url, data in [("/events/new/", event_payload(self.prop)),
                          ("/events/9999/edit/", event_payload(self.prop)),
                          ("/events/9999/delete/", {})]:
            r = c.post(url, data)
            self.assertIn(r.status_code, (403, 404))
        self.assertEqual(FarmingEvent.objects.filter(farm=self.farm).count(), n0)
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), a0)

    def test_cross_farm_isolation_and_bad_farm_ids(self):
        from portal.models import FarmingEvent
        c = self.login(self.owner)
        e = FarmingEvent.objects.create(
            farm=self.f2, property=self.p_o2, event_date=date(2019, 1, 1),
            event_type="sowing", description="Másik", created_by=self.o2, updated_by=self.o2)
        self.assertEqual(c.get(f"/events/{e.pk}/edit/").status_code, 404)
        self.assertEqual(c.post(f"/events/{e.pk}/delete/").status_code, 404)
        n0 = FarmingEvent.objects.filter(farm=self.farm).count()
        r = c.post("/events/new/", event_payload(self.p_o2, event_date="2020-01-01"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FarmingEvent.objects.filter(farm=self.farm).count(), n0)
        self.assertEqual(FarmingEvent.objects.filter(farm=self.farm).count(), 0)

    def test_filters_default_all_years_chronological_malformed(self):
        from portal.models import FarmingEvent
        for d, t, p in [(date(1999, 3, 1), "sowing", self.prop),
                        (date(2021, 7, 1), "harvesting", self.prop2),
                        (date(2022, 1, 5), "sowing", self.prop2)]:
            FarmingEvent.objects.create(
                farm=self.farm, property=p, event_date=d, event_type=t,
                description=f"E-{d}", created_by=self.owner, updated_by=self.owner)
        c = self.login(self.owner)
        r = c.get("/events/")
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertIn("E-1999-03-01", rows)
        self.assertIn("E-2022-01-05", rows)
        self.assertLess(rows.index("E-1999-03-01"), rows.index("E-2022-01-05"))
        self.assertIn("mind", r.content.decode().lower())
        self.assertNotIn("years", r.context.keys())
        self.assertNotIn('name="year"', r.content.decode())
        r = c.get("/events/", {"year": "2021"})
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertIn("E-2021-07-01", rows)
        self.assertNotIn("E-1999-03-01", rows)
        r = c.get("/events/", {"date_from": "2020-01-01", "date_to": "2021-12-31"})
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertIn("E-2021-07-01", rows)
        self.assertNotIn("E-1999-03-01", rows)
        r = c.get("/events/", {"property": str(self.prop2.pk), "type": "sowing"})
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertIn("E-2022-01-05", rows)
        self.assertNotIn("E-2021-07-01", rows)
        for bad in [{"year": "abcd"}, {"date_from": "not-a-date"},
                    {"type": "nope"}, {"property": "99999"}]:
            r = c.get("/events/", bad)
            self.assertEqual(r.status_code, 200)
        self.assertEqual([e.description for e in c.get("/events/", {"year": "abcd"}).context["events"]], [])


class FarmingEventUiExportChatTests(TestCase):
    def setUp(self):
        self.owner, self.farm = owner_farm("ui-owner@example.com")
        self.loc = make_location(self.farm)
        self.prop = make_property(self.farm, self.loc, name="PU")
        self.worker = add_member(self.farm, "ui-worker@example.com", "worker")
        self.viewer = add_member(self.farm, "ui-viewer@example.com", "viewer")

    def login(self, u):
        c = Client()
        c.force_login(u)
        return c

    def test_nav_sidebar_and_property_history_link(self):
        c = self.login(self.owner)
        r = c.get("/properties/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "/events/")
        r = c.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "/events/")

    def test_role_controls_on_list(self):
        from portal.models import FarmingEvent
        from datetime import date as d
        FarmingEvent.objects.create(
            farm=self.farm, property=self.prop, event_date=d(2020, 1, 1),
            event_type="sowing", description="X",
            created_by=self.owner, updated_by=self.owner)
        e = FarmingEvent.objects.get(farm=self.farm)
        body_owner = self.login(self.owner).get("/events/").content.decode()
        self.assertIn(f"/events/{e.pk}/edit/", body_owner)
        self.assertIn(f"/events/{e.pk}/delete/", body_owner)
        body_worker = self.login(self.worker).get("/events/").content.decode()
        self.assertIn(f"/events/{e.pk}/edit/", body_worker)
        self.assertNotIn(f"/events/{e.pk}/delete/", body_worker)
        body_viewer = self.login(self.viewer).get("/events/").content.decode()
        self.assertNotIn(f"/events/{e.pk}/edit/", body_viewer)
        self.assertNotIn("/events/new/", body_viewer)

    def test_hu_en_labels_and_disclaimer(self):
        c = self.login(self.owner)
        hu = c.get("/events/", HTTP_ACCEPT_LANGUAGE="hu").content.decode()
        self.assertIn("Gazdálkodási napló", hu)
        self.assertIn("eGN", hu)
        en = c.get("/events/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn("Field log", en)
        self.assertIn("eGN", en)

    def test_export_additive_no_leak_no_email(self):
        import json
        from portal.models import FarmingEvent
        from datetime import date as d
        FarmingEvent.objects.create(
            farm=self.farm, property=self.prop, event_date=d(2020, 1, 1),
            event_type="sowing", description="EXP-1",
            created_by=self.owner, updated_by=self.owner)
        o2, f2 = owner_farm("exp-other@example.com")
        p2 = make_property(f2, name="EXP-P")
        FarmingEvent.objects.create(
            farm=f2, property=p2, event_date=d(2020, 1, 1),
            event_type="sowing", description="EXP-OTHER",
            created_by=o2, updated_by=o2)
        r = self.login(self.owner).get("/fiok/export/")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content.decode())
        self.assertIn("farming_events", data)
        self.assertEqual(data["account_email"], self.owner.email)
        descs = [e["description"] for e in data["farming_events"]]
        self.assertIn("EXP-1", descs)
        self.assertNotIn("EXP-OTHER", descs)
        actors = [e.get("created_by") for e in data["farming_events"]] + [e.get("updated_by") for e in data["farming_events"]]
        self.assertIn(self.owner.pk, [a for a in actors if a])
        self.assertNotIn(o2.email, json.dumps(data["farming_events"]))
        self.assertNotIn(self.owner.email, json.dumps(data["farming_events"]))
        self.assertNotIn("ui-worker@example.com", json.dumps(data["farming_events"]))
        self.assertNotIn("ui-viewer@example.com", json.dumps(data["farming_events"]))
        self.assertIn("crop_cycles", data)

    def test_chat_kinds_unchanged(self):
        from portal.models import ChatProposal
        kinds = [k for k, _ in ChatProposal.KIND_CHOICES]
        self.assertEqual(sorted(kinds), ["action_create", "action_toggle", "navigate", "plans_generate"])


class Task1NoYearFieldTests(TestCase):
    """Task1: no visible year field; history across years via date range."""

    def setUp(self):
        self.owner, self.farm = owner_farm("task1-owner@example.com")
        self.loc = make_location(self.farm)
        self.prop = make_property(self.farm, self.loc, name="T1P")

    def login(self, u):
        c = Client()
        c.force_login(u)
        return c

    def _add(self, day, desc):
        from portal.models import FarmingEvent
        return FarmingEvent.objects.create(
            farm=self.farm, property=self.prop, event_date=day,
            event_type="sowing", description=desc,
            created_by=self.owner, updated_by=self.owner)

    def test_no_year_field_rendered(self):
        c = self.login(self.owner)
        html = c.get("/events/").content.decode()
        self.assertNotIn('name="year"', html)
        self.assertIn('name="date_from"', html)
        self.assertIn('name="date_to"', html)
        self.assertNotIn("years", c.get("/events/").context.keys())

    def test_multiyear_range_search(self):
        self._add(date(2023, 5, 1), "T1-2023")
        self._add(date(2024, 6, 1), "T1-2024")
        self._add(date(2025, 7, 1), "T1-2025")
        c = self.login(self.owner)
        r = c.get("/events/", {"date_from": "2024-01-01", "date_to": "2024-12-31"})
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertIn("T1-2024", rows)
        self.assertNotIn("T1-2023", rows)
        self.assertNotIn("T1-2025", rows)
        r = c.get("/events/")
        rows = [e.description for e in r.context["events"]]
        self.assertEqual(rows, ["T1-2023", "T1-2024", "T1-2025"])

    def test_legacy_year_param_still_filters(self):
        self._add(date(2023, 5, 1), "T1Y-2023")
        self._add(date(2024, 6, 1), "T1Y-2024")
        c = self.login(self.owner)
        r = c.get("/events/", {"year": "2024"})
        self.assertEqual(r.status_code, 200)
        rows = [e.description for e in r.context["events"]]
        self.assertIn("T1Y-2024", rows)
        self.assertNotIn("T1Y-2023", rows)
