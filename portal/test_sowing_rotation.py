"""Task12 foundation slice 1 RED: planned-only multiple, required sow_date, locked legacy.

English comments/docstrings; HU/EN UI preserved. Tests use mocked local data only.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from portal.models import CropCycle, Farm, FarmLocation, FarmProperty

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email="slice1-owner@example.com"):
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


def cycle_payload(prop, name="Kukorica", state="planned", sow_date=None, **over):
    d = {
        "property": str(prop.pk),
        "crop_name": name,
        "state": state,
        "growth_stage": "",
        "sow_date": sow_date if sow_date is not None else "",
        "expected_end_date": "",
        "observations": "",
    }
    d.update(over)
    return d


class Slice1PlannedMultipleTests(TestCase):
    """Multiple planned per property; current uniqueness preserved."""

    def test_multiple_planned_allowed_same_property(self):
        # New contract: two planned rows on one property must validate.
        _, f = owner_farm("s1-multi@example.com")
        prop = make_property(f)
        CropCycle.objects.create(farm=f, property=prop, crop_name="P1", state="planned")
        try:
            CropCycle(farm=f, property=prop, crop_name="P2", state="planned").full_clean()
            ok = True
        except ValidationError:
            ok = False
        self.assertTrue(ok, "second planned on same property must be allowed")

    def test_second_current_still_blocked(self):
        # Preserved contract: second current on one property stays rejected.
        _, f = owner_farm("s1-cur@example.com")
        prop = make_property(f)
        CropCycle.objects.create(farm=f, property=prop, crop_name="A", state="current")
        with self.assertRaises(ValidationError):
            CropCycle(farm=f, property=prop, crop_name="B", state="current").full_clean()

    def test_legacy_null_date_planned_loads(self):
        # Legacy rows with null sow_date must keep loading.
        u, f = owner_farm("s1-legacy@example.com")
        prop = make_property(f)
        CropCycle.objects.create(farm=f, property=prop, crop_name="Regi", state="planned")
        self.assertEqual(CropCycle.objects.filter(farm=f).count(), 1)
        c = Client()
        c.force_login(u)
        r = c.get("/cycles/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Regi")


class Slice1ManualFormTests(TestCase):
    """Manual create/edit: force planned, require sow_date, lock legacy state."""

    def test_create_forces_planned_ignores_tampered_current(self):
        u, f = owner_farm("s1-force-cur@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        future = (date.today() + timedelta(days=7)).isoformat()
        r = c.post("/cycles/new/", cycle_payload(prop, name="Buza", state="current", sow_date=future))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        obj = CropCycle.objects.get(farm=f, crop_name="Buza")
        self.assertEqual(obj.state, "planned")

    def test_create_forces_planned_ignores_tampered_history(self):
        u, f = owner_farm("s1-force-hist@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        future = (date.today() + timedelta(days=7)).isoformat()
        r = c.post("/cycles/new/", cycle_payload(prop, name="Arpa", state="history", sow_date=future))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        obj = CropCycle.objects.get(farm=f, crop_name="Arpa")
        self.assertEqual(obj.state, "planned")

    def test_create_requires_sow_date(self):
        u, f = owner_farm("s1-req-date@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        r = c.post("/cycles/new/", cycle_payload(prop, name="Nodate", state="planned", sow_date=""))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CropCycle.objects.filter(farm=f, crop_name="Nodate").exists())

    def test_create_allows_past_sow_date_for_correction(self):
        # UI slice: past planned dates allowed to correct old unfulfilled plans.
        u, f = owner_farm("s1-past@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        past = (date.today() - timedelta(days=1)).isoformat()
        r = c.post("/cycles/new/", cycle_payload(prop, name="Mult", state="planned", sow_date=past))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        self.assertTrue(CropCycle.objects.filter(farm=f, crop_name="Mult").exists())

    def test_edit_locks_legacy_current_state(self):
        u, f = owner_farm("s1-lock-cur@example.com")
        prop = make_property(f)
        obj = CropCycle.objects.create(farm=f, property=prop, crop_name="RegiAkt", state="current")
        c = Client()
        c.force_login(u)
        future = (date.today() + timedelta(days=7)).isoformat()
        r = c.post(f"/cycles/{obj.pk}/edit/",
                   cycle_payload(prop, name="RegiAkt", state="planned", sow_date=future))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        obj.refresh_from_db()
        self.assertEqual(obj.state, "current")

    def test_edit_locks_legacy_history_state(self):
        u, f = owner_farm("s1-lock-hist@example.com")
        prop = make_property(f)
        obj = CropCycle.objects.create(farm=f, property=prop, crop_name="RegiHist", state="history")
        c = Client()
        c.force_login(u)
        future = (date.today() + timedelta(days=7)).isoformat()
        r = c.post(f"/cycles/{obj.pk}/edit/",
                   cycle_payload(prop, name="RegiHist", state="planned", sow_date=future))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        obj.refresh_from_db()
        self.assertEqual(obj.state, "history")

    def test_edit_planned_requires_sow_date(self):
        u, f = owner_farm("s1-edit-req@example.com")
        prop = make_property(f)
        future = date.today() + timedelta(days=7)
        obj = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Terv", state="planned", sow_date=future)
        c = Client()
        c.force_login(u)
        r = c.post(f"/cycles/{obj.pk}/edit/",
                   cycle_payload(prop, name="Terv", state="planned", sow_date=""))
        self.assertEqual(r.status_code, 200)
        obj.refresh_from_db()
        self.assertEqual(obj.sow_date, future)

    def test_active_farm_isolation_other_property_rejected(self):
        u1, f1 = owner_farm("s1-iso1@example.com")
        _, f2 = owner_farm("s1-iso2@example.com")
        prop2 = make_property(f2, name="Masik")
        c = Client()
        c.force_login(u1)
        future = (date.today() + timedelta(days=7)).isoformat()
        r = c.post("/cycles/new/", cycle_payload(prop2, name="Idegen", state="planned", sow_date=future))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CropCycle.objects.filter(crop_name="Idegen").exists())


class Slice2SoftHidePreserveTests(TestCase):
    """Delete of planned preserves row, WorkItems and journal link."""

    def test_delete_planned_preserves_row_workitems_journal(self):
        # No direct hidden-field reference: behavior must hold via preservation.
        from portal.models import FarmingEvent, WorkItem
        u, f = owner_farm("s2-preserve@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="TervBuza", state="planned",
            sow_date=date.today() + timedelta(days=7))
        w = WorkItem.objects.create(
            farm=f, crop_cycle=cyc, property=prop, title="Vetes",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=2), estimated_hours="2.0")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cyc, event_date=date(2024, 4, 10),
            event_type="sowing", description="Vetes naplo",
            created_by=u, updated_by=u)
        c = Client()
        c.force_login(u)
        r = c.post(f"/cycles/{cyc.pk}/delete/", {})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(CropCycle.objects.filter(pk=cyc.pk).exists())
        self.assertTrue(WorkItem.objects.filter(pk=w.pk).exists())
        self.assertTrue(FarmingEvent.objects.filter(pk=ev.pk).exists())
        ev.refresh_from_db()
        self.assertEqual(ev.crop_cycle_id, cyc.pk)

    def test_delete_current_still_physical(self):
        u, f = owner_farm("s2-cur-del@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Akt", state="current")
        c = Client()
        c.force_login(u)
        r = c.post(f"/cycles/{cyc.pk}/delete/", {})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(CropCycle.objects.filter(pk=cyc.pk).exists())

    def test_export_still_contains_soft_hidden_plan(self):
        import json as _json
        u, f = owner_farm("s2-export@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="ExportTerv", state="planned",
            sow_date=date.today() + timedelta(days=7))
        c = Client()
        c.force_login(u)
        r = c.post(f"/cycles/{cyc.pk}/delete/", {})
        self.assertEqual(r.status_code, 302)
        d = _json.loads(c.get("/fiok/export/").content.decode("utf-8"))
        names = [x.get("crop_name") for x in d.get("crop_cycles", [])]
        self.assertIn("ExportTerv", names)


class Slice2HidePropagationTests(TestCase):
    """Hidden plans excluded from plans/chat/utilization/selectors; journal edit retains link."""

    def _hidden_and_visible(self, email):
        from django.utils import timezone as _tz
        _, f = owner_farm(email)
        prop = make_property(f)
        vis = CropCycle.objects.create(
            farm=f, property=prop, crop_name="LathatoTerv", state="planned",
            sow_date=date.today() + timedelta(days=7))
        hid = CropCycle.objects.create(
            farm=f, property=prop, crop_name="RejtettTerv", state="planned",
            sow_date=date.today() + timedelta(days=8),
            is_hidden=True, hidden_at=_tz.now(), hidden_reason="deleted")
        return f, prop, vis, hid

    def test_plans_cycle_dicts_excludes_hidden(self):
        from portal.plans_views import _cycle_dicts
        f, _, vis, hid = self._hidden_and_visible("s2-prop-plans@example.com")
        ids = [d["id"] for d in _cycle_dicts(f)]
        self.assertIn(vis.id, ids)
        self.assertNotIn(hid.id, ids)

    def test_plans_get_excludes_hidden_linked_workitems(self):
        from portal.models import WorkItem
        u, f = owner_farm("s2-prop-plans-get@example.com")
        prop = make_property(f)
        vis = CropCycle.objects.create(
            farm=f, property=prop, crop_name="VisPlan", state="planned",
            sow_date=date.today() + timedelta(days=7))
        hid = CropCycle.objects.create(
            farm=f, property=prop, crop_name="HidPlan", state="planned",
            sow_date=date.today() + timedelta(days=8),
            is_hidden=True, hidden_reason="deleted")
        WorkItem.objects.create(
            farm=f, crop_cycle=vis, property=prop, title="LathatoMunkaAlpha",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=1), estimated_hours="1.0")
        WorkItem.objects.create(
            farm=f, crop_cycle=hid, property=prop, title="RejtettMunkaBeta",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=1), estimated_hours="1.0")
        c = Client()
        c.force_login(u)
        b = c.get("/terv/").content.decode("utf-8")
        self.assertIn("LathatoMunkaAlpha", b)
        self.assertNotIn("RejtettMunkaBeta", b)

    def test_chat_ctx_excludes_hidden(self):
        from portal.chat import _build_ctx_dict, _CTX_LIMITS
        f, _, vis, hid = self._hidden_and_visible("s2-prop-chat@example.com")
        ctx = _build_ctx_dict(f, _CTX_LIMITS[0])
        ids = [x["id"] for x in ctx.get("crop_cycles", [])]
        self.assertIn(vis.id, ids)
        self.assertNotIn(hid.id, ids)

    def test_event_form_excludes_hidden_but_retains_existing_link(self):
        from portal.domain_forms import FarmingEventForm
        from portal.models import FarmingEvent
        u, f = owner_farm("s2-prop-form@example.com")
        prop = make_property(f)
        vis = CropCycle.objects.create(
            farm=f, property=prop, crop_name="VisLink", state="planned",
            sow_date=date.today() + timedelta(days=7))
        hid = CropCycle.objects.create(
            farm=f, property=prop, crop_name="HidLink", state="planned",
            sow_date=date.today() + timedelta(days=8),
            is_hidden=True, hidden_reason="deleted")
        # New entry: hidden plans not offered.
        form = FarmingEventForm(farm=f)
        qids = list(form.fields["crop_cycle"].queryset.values_list("pk", flat=True))
        self.assertIn(vis.pk, qids)
        self.assertNotIn(hid.pk, qids)
        # Historical edit: existing hidden link retained in choices.
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=hid, event_date=date(2024, 4, 10),
            event_type="sowing", description="Hist link",
            created_by=u, updated_by=u)
        form2 = FarmingEventForm(instance=ev, farm=f)
        qids2 = list(form2.fields["crop_cycle"].queryset.values_list("pk", flat=True))
        self.assertIn(hid.pk, qids2)

    def test_utilization_excludes_hidden_linked(self):
        from portal.models import WorkItem
        u, f = owner_farm("s2-prop-util@example.com")
        prop = make_property(f)
        hid = CropCycle.objects.create(
            farm=f, property=prop, crop_name="HidUtil", state="planned",
            sow_date=date.today() + timedelta(days=8),
            is_hidden=True, hidden_reason="deleted")
        WorkItem.objects.create(
            farm=f, crop_cycle=hid, property=prop, title="RejtettUtilMunkaGamma",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=1), estimated_hours="1.0")
        c = Client()
        c.force_login(u)
        b = c.get("/kihasznaltsag/").content.decode("utf-8")
        self.assertNotIn("RejtettUtilMunkaGamma", b)

    def test_cycle_list_excludes_hidden(self):
        u, f = owner_farm("s2-prop-cyclelist@example.com")
        _, _, vis, hid = self._hidden_and_visible("s2-prop-cyclelist2@example.com")
        # Use same farm for list check via fresh rows.
        from django.utils import timezone as _tz
        prop = make_property(f)
        CropCycle.objects.create(
            farm=f, property=prop, crop_name="ListLathato", state="planned",
            sow_date=date.today() + timedelta(days=7))
        CropCycle.objects.create(
            farm=f, property=prop, crop_name="ListRejtett", state="planned",
            sow_date=date.today() + timedelta(days=8),
            is_hidden=True, hidden_at=_tz.now(), hidden_reason="deleted")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("ListLathato", b)
        self.assertNotIn("ListRejtett", b)


def event_payload(prop, cycle=None, **over):
    d = {
        "property": str(prop.pk),
        "crop_cycle": str(cycle.pk) if cycle else "",
        "crop_name_snapshot": "",
        "event_date": "2024-04-12",
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


class Slice3SnapshotTests(TestCase):
    """Sowing snapshot: entered crop or derived link; stable; legacy blank."""

    def test_sowing_without_link_requires_entered_crop(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-req@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FarmingEvent.objects.filter(farm=f).count(), 0)

    def test_sowing_without_link_stores_entered_crop(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-enter@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Kukorica"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        e = FarmingEvent.objects.get(farm=f)
        self.assertEqual(e.crop_name_snapshot, "Kukorica")
        self.assertIsNone(e.crop_cycle_id)

    def test_linked_sowing_derives_snapshot_from_cycle(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-derive@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Napraforgó", state="current")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, cyc))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        e = FarmingEvent.objects.get(farm=f)
        self.assertEqual(e.crop_name_snapshot, "Napraforgó")

    def test_snapshot_stable_after_cycle_rename(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-stable@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Eredeti", state="current")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, cyc))
        self.assertEqual(r.status_code, 302)
        cyc.crop_name = "Átnevezett"
        cyc.save()
        e = FarmingEvent.objects.get(farm=f)
        self.assertEqual(e.crop_name_snapshot, "Eredeti")

    def test_legacy_blank_snapshot_loads_and_exports_fallback(self):
        import json as _json
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-legacy@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Kapcsolt", state="current")
        FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cyc, event_date=date(2024, 4, 10),
            event_type="sowing", description="Régi vetés",
            created_by=u, updated_by=u)
        c = Client()
        c.force_login(u)
        self.assertEqual(c.get("/events/").status_code, 200)
        d = _json.loads(c.get("/fiok/export/").content.decode("utf-8"))
        rows = [x for x in d.get("farming_events", []) if x.get("description") == "Régi vetés"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("crop_cycle"), "Kapcsolt")
        self.assertIn("crop_name_snapshot", rows[0])

    def test_explicit_snapshot_kept_on_edit(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-keep@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="CiklusNövény", state="current")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(
            prop, cyc, crop_name_snapshot="KéziNövény"))
        self.assertEqual(r.status_code, 302)
        e = FarmingEvent.objects.get(farm=f)
        self.assertEqual(e.crop_name_snapshot, "KéziNövény")
        r = c.post(f"/events/{e.pk}/edit/", event_payload(
            prop, cyc, crop_name_snapshot="KéziNövény", description="Módosítva"))
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual(e.crop_name_snapshot, "KéziNövény")

    def test_edit_changing_link_refreshes_blank_snapshot(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-relink@example.com")
        prop = make_property(f)
        c1 = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Első", state="current")
        c2 = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Második", state="planned",
            sow_date=date.today() + timedelta(days=7))
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, c1))
        self.assertEqual(r.status_code, 302)
        e = FarmingEvent.objects.get(farm=f)
        # Simulate a legacy blank snapshot, then relink to another cycle.
        FarmingEvent.objects.filter(pk=e.pk).update(crop_name_snapshot="")
        r = c.post(f"/events/{e.pk}/edit/", event_payload(prop, c2))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        e.refresh_from_db()
        self.assertEqual(e.crop_cycle_id, c2.pk)
        self.assertEqual(e.crop_name_snapshot, "Második")

    def test_non_sowing_blank_snapshot_allowed(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s3-other@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, event_type="spraying"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        e = FarmingEvent.objects.get(farm=f)
        self.assertEqual(e.crop_name_snapshot, "")

    def test_backfill_only_linked_blank_snapshots(self):
        import importlib as _il
        from django.apps import apps as _apps
        from portal.models import FarmingEvent
        _mig = _il.import_module("portal.migrations.0025_event_crop_snapshot")
        backfill_snapshots = _mig.backfill_snapshots
        u, f = owner_farm("s3-backfill@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Töltött", state="current")
        linked_blank = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cyc, event_date=date(2024, 4, 10),
            event_type="sowing", description="LB", created_by=u, updated_by=u)
        FarmingEvent.objects.filter(pk=linked_blank.pk).update(crop_name_snapshot="")
        unlinked_blank = FarmingEvent.objects.create(
            farm=f, property=prop, event_date=date(2024, 4, 11),
            event_type="spraying", description="UB", created_by=u, updated_by=u)
        filled = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cyc, event_date=date(2024, 4, 12),
            event_type="sowing", description="F", crop_name_snapshot="Régi",
            created_by=u, updated_by=u)
        backfill_snapshots(_apps, None)
        linked_blank.refresh_from_db()
        unlinked_blank.refresh_from_db()
        filled.refresh_from_db()
        self.assertEqual(linked_blank.crop_name_snapshot, "Töltött")
        self.assertEqual(unlinked_blank.crop_name_snapshot, "")
        self.assertEqual(filled.crop_name_snapshot, "Régi")


class Slice4RotationTests(TestCase):
    """Rotation page: journal-derived groups, filter, badges, legacy rows."""

    def _seed_rotation(self, email):
        from portal.models import FarmingEvent
        u, f = owner_farm(email)
        loc = make_location(f, name="L-rot")
        pa = make_property(f, loc, name="ParcellaA")
        pb = make_property(f, loc, name="ParcellaB")
        FarmingEvent.objects.create(
            farm=f, property=pa, event_date=date(2024, 3, 5),
            event_type="sowing", description="Régi vetés",
            crop_name_snapshot="RégiKukorica", created_by=u, updated_by=u)
        FarmingEvent.objects.create(
            farm=f, property=pa, event_date=date(2024, 5, 20),
            event_type="sowing", description="Új vetés",
            crop_name_snapshot="ÚjBúza", created_by=u, updated_by=u)
        FarmingEvent.objects.create(
            farm=f, property=pb, event_date=date(2024, 4, 10),
            event_type="sowing", description="B vetés",
            crop_name_snapshot="BKukorica", created_by=u, updated_by=u)
        p1 = CropCycle.objects.create(
            farm=f, property=pa, crop_name="TervKorai", state="planned",
            sow_date=date.today() + timedelta(days=7))
        p2 = CropCycle.objects.create(
            farm=f, property=pa, crop_name="TervKésői", state="planned",
            sow_date=date.today() + timedelta(days=21))
        leg = CropCycle.objects.create(
            farm=f, property=pa, crop_name="AktuálisRégi", state="current")
        hist = CropCycle.objects.create(
            farm=f, property=pb, crop_name="TörténetiRégi", state="history")
        return u, f, pa, pb, p1, p2, leg, hist

    def test_groups_by_property_newest_first(self):
        u, f, pa, pb, p1, p2, leg, hist = self._seed_rotation("s4-group@example.com")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("ParcellaA", b)
        self.assertIn("ParcellaB", b)
        # Newest effective sowing date first, for journal and planned rows.
        self.assertLess(b.index("ÚjBúza"), b.index("RégiKukorica"))
        self.assertLess(b.index("TervKésői"), b.index("TervKorai"))
        self.assertIn("BKukorica", b)

    def test_property_filter_tenant_safe(self):
        u, f, pa, pb, p1, p2, leg, hist = self._seed_rotation("s4-filter@example.com")
        u2, f2 = owner_farm("s4-other@example.com")
        other_prop = make_property(f2, name="Idegen")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/", {"property": str(pb.pk)}).content.decode("utf-8")
        self.assertIn("BKukorica", b)
        self.assertNotIn("ÚjBúza", b)
        # The filter dropdown still lists all farm properties; only the
        # selected property gets a group section.
        self.assertNotIn("<h2>ParcellaA</h2>", b)
        self.assertNotIn('aria-label="ParcellaA"', b)
        self.assertIn("<h2>ParcellaB</h2>", b)
        # Other farm's property and malformed input yield no rows.
        b = c.get("/cycles/", {"property": str(other_prop.pk)}).content.decode("utf-8")
        self.assertNotIn("ÚjBúza", b)
        self.assertNotIn("BKukorica", b)
        self.assertIn("Nincs elem.", b)
        b = c.get("/cycles/", {"property": "abc"}).content.decode("utf-8")
        self.assertNotIn("ÚjBúza", b)
        self.assertIn("Nincs elem.", b)

    def test_planned_badge_visible_hidden_excluded(self):
        from django.utils import timezone as _tz
        u, f, pa, pb, p1, p2, leg, hist = self._seed_rotation("s4-badge@example.com")
        CropCycle.objects.create(
            farm=f, property=pa, crop_name="TervRejtett", state="planned",
            sow_date=date.today() + timedelta(days=9),
            is_hidden=True, hidden_at=_tz.now(), hidden_reason="deleted")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("rotation-planned", b)
        self.assertIn("TervKorai", b)
        self.assertNotIn("TervRejtett", b)

    def test_legacy_rows_preserved_with_actions(self):
        u, f, pa, pb, p1, p2, leg, hist = self._seed_rotation("s4-legacy@example.com")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("AktuálisRégi", b)
        self.assertIn("TörténetiRégi", b)
        self.assertIn("Korábbi rögzítések", b)
        self.assertIn(f"/cycles/{leg.pk}/edit/", b)
        self.assertIn(f"/cycles/{leg.pk}/delete/", b)
        self.assertIn(f"/cycles/{leg.pk}/observe/", b)
        self.assertIn('<ul class="entity-list">', b)
        self.assertNotIn("domain-table", b)

    def test_snapshot_or_linked_name_shown_for_actuals(self):
        from portal.models import FarmingEvent
        u, f = owner_farm("s4-actual@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="KapcsoltNövény", state="current")
        FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cyc, event_date=date(2024, 4, 10),
            event_type="sowing", description="Kapcsolt vetés",
            created_by=u, updated_by=u)
        FarmingEvent.objects.create(
            farm=f, property=prop, event_date=date(2024, 4, 12),
            event_type="sowing", description="Kézi vetés",
            crop_name_snapshot="KéziÁrpa", created_by=u, updated_by=u)
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("Tényleges vetések", b)
        self.assertIn("KapcsoltNövény", b)
        self.assertIn("KéziÁrpa", b)

    def test_new_cycle_button_present(self):
        u, f, pa, pb, p1, p2, leg, hist = self._seed_rotation("s4-newbtn@example.com")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("Tervezett vetések", b)
        self.assertIn("/cycles/new/", b)
        self.assertIn("Új vetésforgó", b)

    def test_hu_and_en_rotation_headings(self):
        u, f, pa, pb, p1, p2, leg, hist = self._seed_rotation("s4-i18n@example.com")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        for hu in ("Tényleges vetések", "Tervezett vetések", "Korábbi rögzítések"):
            self.assertIn(hu, b)
        c.post("/i18n/setlang/", {"language": "en", "next": "/cycles/"})
        b = c.get("/cycles/").content.decode("utf-8")
        for en in ("Actual sowings", "Planned sowings", "Legacy records", "Crop cycles"):
            self.assertIn(en, b)
        for hu in ("Tényleges vetések", "Tervezett vetések", "Korábbi rögzítések",
                   "Vetésforgók", "Terület", "mind", "Szűrés", "Nincs elem."):
            self.assertNotIn(hu, b)
