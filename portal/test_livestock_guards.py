"""Task4 M1-M3 guard RED: count/history invariants + 2 coverage gaps.

M1: group edit cannot rewrite live headcount/housing; archived frozen
    except owner/manager unarchive.
M2: hard delete refused with incoming moves / any work / live animals.
M3: latest-only reversal on both chronologies, idempotent, archived and
    completed-work refusal, tenant count integrity.
Coverage: mid-session membership revocation; chat regen preserves manual.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import (
    AuditEvent, Farm, FarmLocation, FarmProperty, LivestockGroup,
    Membership, WorkItem,
)

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="lg-owner@example.com"):
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


def make_group(farm, name="Nyal", species="sheep", headcount=10, prop=None):
    return LivestockGroup.objects.create(
        farm=farm, name=name, species=species, headcount=headcount,
        housing_property=prop)


def add_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def login(client, user):
    client.force_login(user)


def record(farm, group, day, kind, delta=0, **kw):
    from portal.models import LivestockEvent
    return LivestockEvent.record(
        farm=farm, group=group, event_date=day, kind=kind,
        headcount_delta=delta, **kw)


class M1EditGuardTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)
        self.c = Client()
        login(self.c, self.u)

    def edit_payload(self, **over):
        d = {"name": self.g.name, "species": "sheep", "breed_label": "",
             "headcount": "999", "housing_property": str(self.prop.pk),
             "notes": ""}
        d.update(over)
        return d

    def test_edit_post_cannot_change_headcount_after_events(self):
        from portal.models import LivestockEvent
        record(self.f, self.g, date(2024, 3, 1), "birth", 5)
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 15)
        r = self.c.post(f"/livestock/{self.g.pk}/edit/", self.edit_payload())
        self.assertIn(r.status_code, (302, 303))
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 15)
        self.assertEqual(LivestockEvent.objects.filter(group=self.g).count(), 1)

    def test_edit_form_headcount_not_editable(self):
        r = self.c.get(f"/livestock/{self.g.pk}/edit/")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertNotIn('name="headcount"', b)
        self.assertIn(str(self.g.headcount), b)

    def test_housing_change_leaves_audit_trace(self):
        prop2 = make_property(self.f, name="Masik", kind="barn")
        r = self.c.post(f"/livestock/{self.g.pk}/edit/", self.edit_payload(
            headcount=str(self.g.headcount),
            housing_property=str(prop2.pk)))
        self.assertIn(r.status_code, (302, 303))
        self.g.refresh_from_db()
        self.assertEqual(self.g.housing_property_id, prop2.pk)
        audits = AuditEvent.objects.filter(
            farm=self.f, action="livestock.group.update",
            object_id=str(self.g.pk)).order_by("-id")
        self.assertTrue(audits.exists())
        meta = audits.first().meta or {}
        self.assertEqual(meta.get("housing_old"), self.prop.pk)
        self.assertEqual(meta.get("housing_new"), prop2.pk)

    def test_archived_group_edit_post_refused(self):
        self.g.is_active = False
        self.g.save()
        r = self.c.post(f"/livestock/{self.g.pk}/edit/", self.edit_payload(
            headcount=str(self.g.headcount), name="Atirt"))
        self.assertIn(r.status_code, (302, 303, 403))
        self.g.refresh_from_db()
        self.assertEqual(self.g.name, "Nyal")
        self.assertFalse(self.g.is_active)

    def test_unarchive_owner_ok_viewer_forbidden(self):
        self.g.is_active = False
        self.g.save()
        r = self.c.post(f"/livestock/{self.g.pk}/archive/")
        self.assertIn(r.status_code, (302, 303))
        self.g.refresh_from_db()
        self.assertTrue(self.g.is_active)
        self.g.is_active = False
        self.g.save()
        viewer = add_member(self.f, "lg-viewer@example.com", "viewer")
        c2 = Client()
        login(c2, viewer)
        self.assertIn(c2.post(f"/livestock/{self.g.pk}/archive/").status_code,
                      (403, 404))


class M2DeleteGuardTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)
        self.c = Client()
        login(self.c, self.u)

    def test_delete_refused_when_move_target(self):
        src = make_group(self.f, name="Forras", headcount=10)
        record(self.f, src, date(2024, 3, 2), "move", -4, target_group=self.g)
        r = self.c.post(f"/livestock/{self.g.pk}/delete/")
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(LivestockGroup.objects.filter(pk=self.g.pk).exists())
        src.refresh_from_db()
        self.g.refresh_from_db()
        self.assertEqual((src.headcount, self.g.headcount), (6, 14))

    def test_delete_refused_with_planned_work(self):
        WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="feeding",
            title="Etetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 1))
        r = self.c.post(f"/livestock/{self.g.pk}/delete/")
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(LivestockGroup.objects.filter(pk=self.g.pk).exists())
        self.assertEqual(WorkItem.objects.filter(livestock_group=self.g).count(), 1)

    def test_delete_refused_with_completed_work(self):
        w = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="grazing",
            title="Legeltetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))
        self.c.post(f"/livestock/work/{w.pk}/done/", {"event_date": "2024-04-02"})
        w.refresh_from_db()
        self.assertEqual(w.status, "done")
        r = self.c.post(f"/livestock/{self.g.pk}/delete/")
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(LivestockGroup.objects.filter(pk=self.g.pk).exists())
        self.assertTrue(WorkItem.objects.filter(pk=w.pk).exists())

    def test_delete_refused_with_live_headcount(self):
        g = make_group(self.f, name="Ures-nincs-esemeny", headcount=3)
        r = self.c.post(f"/livestock/{g.pk}/delete/")
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(LivestockGroup.objects.filter(pk=g.pk).exists())

    def test_delete_truly_empty_ok_and_archive_with_history_ok(self):
        g = make_group(self.f, name="TenylegUres", headcount=0)
        r = self.c.post(f"/livestock/{g.pk}/delete/")
        self.assertIn(r.status_code, (302, 303))
        self.assertFalse(LivestockGroup.objects.filter(pk=g.pk).exists())
        record(self.f, self.g, date(2024, 3, 1), "birth", 2)
        r = self.c.post(f"/livestock/{self.g.pk}/archive/")
        self.assertIn(r.status_code, (302, 303))
        self.g.refresh_from_db()
        self.assertFalse(self.g.is_active)
        self.assertEqual(self.c.get(f"/livestock/{self.g.pk}/").status_code, 200)


class M3ReversalGuardTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm()
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)
        self.c = Client()
        login(self.c, self.u)

    def test_double_reversal_no_double_count(self):
        from django.core.exceptions import ValidationError
        ev = record(self.f, self.g, date(2024, 3, 1), "birth", 5)
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 15)
        ev.reverse_delete()
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 10)
        with self.assertRaises(ValidationError):
            ev.reverse_delete()
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 10)

    def test_repeated_view_delete_no_double_count(self):
        ev = record(self.f, self.g, date(2024, 3, 1), "birth", 5)
        r = self.c.post(f"/livestock/events/{ev.pk}/delete/")
        self.assertIn(r.status_code, (302, 303))
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 10)
        r = self.c.post(f"/livestock/events/{ev.pk}/delete/")
        self.assertEqual(r.status_code, 404)
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 10)

    def test_non_latest_reversal_refused(self):
        from django.core.exceptions import ValidationError
        old = record(self.f, self.g, date(2024, 3, 1), "birth", 5)
        record(self.f, self.g, date(2024, 3, 5), "death", -2)
        with self.assertRaises(ValidationError):
            old.reverse_delete()
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 13)

    def test_move_reversal_needs_target_latest(self):
        from django.core.exceptions import ValidationError
        tgt = make_group(self.f, name="Cel", headcount=0)
        mv = record(self.f, self.g, date(2024, 3, 2), "move", -4,
                    target_group=tgt)
        record(self.f, tgt, date(2024, 3, 6), "birth", 1)
        with self.assertRaises(ValidationError):
            mv.reverse_delete()
        self.g.refresh_from_db()
        tgt.refresh_from_db()
        self.assertEqual((self.g.headcount, tgt.headcount), (6, 5))

    def test_move_reversal_latest_both_ok(self):
        tgt = make_group(self.f, name="Cel", headcount=0)
        mv = record(self.f, self.g, date(2024, 3, 2), "move", -4,
                    target_group=tgt)
        mv.reverse_delete()
        self.g.refresh_from_db()
        tgt.refresh_from_db()
        self.assertEqual((self.g.headcount, tgt.headcount), (10, 0))

    def test_reversal_on_archived_group_refused(self):
        from django.core.exceptions import ValidationError
        ev = record(self.f, self.g, date(2024, 3, 1), "birth", 5)
        self.g.refresh_from_db()
        self.g.is_active = False
        self.g.save()
        with self.assertRaises(ValidationError):
            ev.reverse_delete()
        self.g.refresh_from_db()
        self.assertEqual(self.g.headcount, 15)

    def test_completion_event_reversal_refused(self):
        from django.core.exceptions import ValidationError
        from portal.models import LivestockEvent
        w = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="grazing",
            title="Legeltetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 2))
        self.c.post(f"/livestock/work/{w.pk}/done/", {"event_date": "2024-04-02"})
        w.refresh_from_db()
        ev = LivestockEvent.objects.get(pk=w.completed_event_id)
        with self.assertRaises(ValidationError):
            ev.reverse_delete()
        w.refresh_from_db()
        self.assertEqual(w.status, "done")
        self.assertTrue(LivestockEvent.objects.filter(pk=ev.pk).exists())

    def test_tenant_reversal_integrity(self):
        u2, f2 = owner_farm("lg-second@example.com")
        g2 = make_group(f2, name="Masik", headcount=7)
        ev = record(self.f, self.g, date(2024, 3, 1), "birth", 5)
        c2 = Client()
        login(c2, u2)
        self.assertEqual(c2.post(f"/livestock/events/{ev.pk}/delete/").status_code, 404)
        self.g.refresh_from_db()
        g2.refresh_from_db()
        self.assertEqual((self.g.headcount, g2.headcount), (15, 7))


class CoverageGapTests(TestCase):
    def setUp(self):
        self.u, self.f = owner_farm("lg-cov@example.com")
        self.prop = make_property(self.f)
        self.g = make_group(self.f, prop=self.prop)
        self.c = Client()

    def test_revoked_membership_denies_livestock_post(self):
        manager = add_member(self.f, "lg-mgr@example.com", "manager")
        c = Client()
        login(c, manager)
        r = c.post("/livestock/new/", {"name": "Uj", "species": "sheep",
                                       "headcount": "2", "housing_property": "",
                                       "breed_label": "", "notes": ""})
        self.assertIn(r.status_code, (302, 303))
        Membership.objects.filter(farm=self.f, user=manager).update(is_active=False)
        r = c.post("/livestock/new/", {"name": "Masik", "species": "sheep",
                                       "headcount": "2", "housing_property": "",
                                       "breed_label": "", "notes": ""})
        self.assertIn(r.status_code, (403, 404))
        self.assertFalse(LivestockGroup.objects.filter(
            farm=self.f, name="Masik").exists())

    def test_chat_regenerate_preserves_manual_livestock(self):
        from portal.chat import _regenerate_plans
        login(self.c, self.u)
        manual = WorkItem.objects.create(
            farm=self.f, crop_cycle=None, property=self.prop,
            livestock_group=self.g, livestock_kind="feeding",
            title="Kezi etetes", capability="other",
            start_date=date(2024, 4, 1), end_date=date(2024, 4, 1))
        _regenerate_plans(self.f)
        self.assertTrue(WorkItem.objects.filter(pk=manual.pk).exists())
