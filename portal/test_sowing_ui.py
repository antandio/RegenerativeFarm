"""Task12 UI slice RED: review UI, badge, preservation, dates, journal, sorting.

English comments/docstrings; HU/EN UI preserved. Provider mocked only.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import (
    ConsentEvent, CropCycle, Farm, FarmingEvent, FarmLocation, FarmProperty,
    Membership, SowingComparison, WorkItem,
)

User = get_user_model()
PW = "TesztJelszo123!"
CSS_VERSION = "20261104-knowledge-tables"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email):
    u = make_user(email)
    f = Farm.objects.create(owner=u, name="Farm " + email)
    return u, f


def make_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def make_location(farm, name="Loc"):
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude="46.253000",
        longitude="20.141400", timezone="Europe/Budapest")


def make_property(farm, loc=None, name="Parcel", kind="field"):
    loc = loc or make_location(farm, name="L-" + name)
    return FarmProperty.objects.create(
        farm=farm, location=loc, name=name, kind=kind, area_hectares="5.00")


def grant_consent(farm, owner):
    return ConsentEvent.objects.create(farm=farm, actor=owner, event_type="granted")


def seed_pending(farm, actor, prop, actual_crop="Buza", plan_crop="Buza",
                 event_date=None, sow_offset=5):
    """Create one pending comparison via the service (DB only, no network)."""
    from portal.crop_sowing import enqueue_sowing_reconciliation
    event_date = event_date or date.today()
    plan = CropCycle.objects.create(
        farm=farm, property=prop, crop_name=plan_crop, state="planned",
        sow_date=event_date + timedelta(days=sow_offset))
    ev = FarmingEvent.objects.create(
        farm=farm, property=prop, event_date=event_date, event_type="sowing",
        description="Vetes", crop_name_snapshot=actual_crop,
        created_by=actor, updated_by=actor)
    row = enqueue_sowing_reconciliation(farm, ev, actor)
    assert row is not None
    return row, ev, plan


def confirm_mismatch(row):
    row.status = "success"
    row.decision = "mismatch"
    row.reason_code = "crop_mismatch"
    row.review_state = "confirmed_mismatch"
    row.save()
    return row


class RotationReviewDisplayTests(TestCase):
    """Pending vs confirmed distinct; yellow accessible mismatch; badge."""

    def test_pending_section_never_claims_confirmed_contradiction(self):
        u, f = owner_farm("ui-pending@example.com")
        prop = make_property(f)
        seed_pending(f, u, prop, actual_crop="Kukorica", plan_crop="Buza")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        # Pending wording present, confirmed-contradiction wording absent.
        self.assertIn("Ellen\u0151rz\u00e9s alatt", b)
        for bad in ("Meger\u0151s\u00edtett elt\u00e9r\u00e9s",
                    "ellentmond\u00e1s", "Confirmed mismatch",
                    "contradiction"):
            # Pending page must not claim a confirmed contradiction.
            if "Meger\u0151s\u00edtett elt\u00e9r\u00e9s" in b and bad == "Meger\u0151s\u00edtett elt\u00e9r\u00e9s":
                # Only allowed inside the confirmed section; with pending only,
                # the whole page must not contain it.
                pass
            self.assertNotIn(bad, b)

    def test_confirmed_mismatch_yellow_accessible(self):
        u, f = owner_farm("ui-conf@example.com")
        prop = make_property(f)
        row, _, _ = seed_pending(f, u, prop)
        confirm_mismatch(row)
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("Meger\u0151s\u00edtett elt\u00e9r\u00e9s", b)
        self.assertIn("rotation-mismatch", b)
        # Accessible icon/text: hidden icon plus readable label.
        self.assertIn('aria-hidden="true"', b)
        # No raw LLM reason enum leaked to the page.
        for raw in ("crop_mismatch", "date_mismatch", "crop_date_match",
                    "ambiguous", "provider_uncertain"):
            self.assertNotIn(raw, b)

    def test_nav_badge_confirmed_only_links_to_rotation(self):
        u, f = owner_farm("ui-badge@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        # Pending alone raises no badge.
        seed_pending(f, u, prop)
        b = c.get("/attekintes/").content.decode("utf-8")
        self.assertNotIn("nav-badge", b)
        # Confirmed raises a count badge linking to the rotation page.
        row = SowingComparison.objects.filter(farm=f).first()
        confirm_mismatch(row)
        b = c.get("/attekintes/").content.decode("utf-8")
        self.assertIn("nav-badge", b)
        self.assertIn('href="/cycles/"', b)
        # Badge shows the active-farm confirmed count.
        self.assertIn(">1<", b.replace(" ", ""))


class ReviewActionTests(TestCase):
    """POST-only owner/manager retry/retain/hide; scoping; stale; no GET provider."""

    def test_mutation_routes_get_405_and_no_provider(self):
        from unittest.mock import patch
        u, f = owner_farm("ui-405@example.com")
        prop = make_property(f)
        row, _, _ = seed_pending(f, u, prop)
        c = Client()
        c.force_login(u)
        with patch("portal.provider.MetaProviderClient.generate_sowing_comparison") as m:
            for url in ("/cycles/sowing/%d/retry/" % row.pk,
                        "/cycles/sowing/%d/retain/" % row.pk,
                        "/cycles/sowing/%d/hide/" % row.pk):
                r = c.get(url)
                self.assertEqual(r.status_code, 405, url)
            self.assertEqual(m.call_count, 0)

    def test_role_and_farm_scoping(self):
        u, f = owner_farm("ui-scope@example.com")
        prop = make_property(f)
        row, _, _ = seed_pending(f, u, prop)
        worker = make_member(f, "ui-scope-worker@example.com", "worker")
        viewer = make_member(f, "ui-scope-viewer@example.com", "viewer")
        u2, f2 = owner_farm("ui-scope-other@example.com")
        for who in (worker, viewer):
            c = Client()
            c.force_login(who)
            for url in ("/cycles/sowing/%d/retry/" % row.pk,
                        "/cycles/sowing/%d/retain/" % row.pk,
                        "/cycles/sowing/%d/hide/" % row.pk):
                r = c.post(url, {})
                self.assertEqual(r.status_code, 403, (who.email, url))
        c = Client()
        c.force_login(u2)
        # Other farm cannot see the row.
        r = c.post("/cycles/sowing/%d/retry/" % row.pk, {})
        self.assertEqual(r.status_code, 404)

    def test_retry_uses_consent_gate_no_bypass(self):
        from portal.test_sowing_reconcile import FakeSowingClient
        u, f = owner_farm("ui-retry-consent@example.com")
        prop = make_property(f)
        # No consent: enqueue stays pending without dispatch.
        row, _, _ = seed_pending(f, u, prop)
        self.assertIsNone(SowingComparison.objects.get(pk=row.pk).dispatch_authorized_at)
        c = Client()
        c.force_login(u)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        # Retry without consent must not dispatch (view redirects, row stays).
        r = c.post("/cycles/sowing/%d/retry/" % row.pk, {})
        self.assertIn(r.status_code, (302, 200))
        # Direct service path also refuses without consent (no provider call).
        from portal.crop_sowing import execute_sowing_comparison
        execute_sowing_comparison(row.pk, client=fake)
        self.assertEqual(fake.calls, 0)

    def test_retain_clears_badge_marks_plan_suppresses_reenqueue(self):
        u, f = owner_farm("ui-retain@example.com")
        prop = make_property(f)
        row, ev, plan = seed_pending(f, u, prop, actual_crop="Kukorica", plan_crop="Buza")
        confirm_mismatch(row)
        c = Client()
        c.force_login(u)
        b = c.get("/attekintes/").content.decode("utf-8")
        self.assertIn("nav-badge", b)
        r = c.post("/cycles/sowing/%d/retain/" % row.pk, {})
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.review_state, "dismissed_retained")
        # New-mismatch badge cleared, distinct retained indicator remains.
        b = c.get("/attekintes/").content.decode("utf-8")
        self.assertNotIn('class="nav-badge"', b)
        self.assertIn("nav-retained-badge", b)
        # Plan stays visible with a retained-difference marker.
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn(plan.crop_name, b)
        self.assertIn("Megtartott elt\u00e9r\u00e9s", b)
        # Unchanged journal edit does not re-enqueue a new comparison.
        n0 = SowingComparison.objects.filter(farm=f).count()
        from portal.test_sowing_rotation import event_payload
        r = c.post("/events/%d/edit/" % ev.pk, event_payload(
            prop, event_date=ev.event_date.isoformat(),
            crop_name_snapshot="Kukorica", description="Vetes"))
        self.assertEqual(r.status_code, 302)
        n1 = SowingComparison.objects.filter(farm=f).count()
        self.assertEqual(n0, n1)

    def test_hide_soft_hides_preserving_journal_workitems(self):
        u, f = owner_farm("ui-hide@example.com")
        prop = make_property(f)
        row, ev, plan = seed_pending(f, u, prop)
        confirm_mismatch(row)
        w = WorkItem.objects.create(
            farm=f, crop_cycle=plan, property=prop, title="HideKeepMunka",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=1), estimated_hours="1.0")
        c = Client()
        c.force_login(u)
        r = c.post("/cycles/sowing/%d/hide/" % row.pk, {})
        self.assertEqual(r.status_code, 302)
        plan.refresh_from_db()
        self.assertTrue(plan.is_hidden)
        self.assertTrue(WorkItem.objects.filter(pk=w.pk).exists())
        self.assertTrue(FarmingEvent.objects.filter(pk=ev.pk).exists())
        row.refresh_from_db()
        self.assertEqual(row.review_state, "dismissed_deleted")
        b = c.get("/attekintes/").content.decode("utf-8")
        self.assertNotIn("nav-badge", b)

    def test_stale_retain_supersedes_without_mutation(self):
        u, f = owner_farm("ui-stale@example.com")
        prop = make_property(f)
        row, ev, plan = seed_pending(f, u, prop)
        confirm_mismatch(row)
        # Stale edit: move the actual far outside the matching window.
        ev.event_date = date.today() + timedelta(days=120)
        ev.save()
        c = Client()
        c.force_login(u)
        r = c.post("/cycles/sowing/%d/retain/" % row.pk, {})
        self.assertIn(r.status_code, (302, 200))
        row.refresh_from_db()
        self.assertEqual(row.review_state, "superseded")
        plan.refresh_from_db()
        self.assertFalse(plan.is_hidden)


class PlannedBadgeCssTests(TestCase):
    """Planned badge is a real CSS rule; version bumped with contract update."""

    def test_rotation_css_rules_exist(self):
        import pathlib, re
        css = (pathlib.Path(__file__).parent / "static/portal/app.css").read_text()
        for cls in (r"\.rotation-planned\s*\{", r"\.rotation-mismatch\s*\{",
                    r"\.rotation-pending\s*\{", r"\.nav-badge\s*\{",
                    r"\.nav-retained-badge\s*\{"):
            self.assertRegex(css, cls, cls)
        m = re.search(r"\.rotation-mismatch\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(m)
        body = m.group(1).lower()
        # Yellow background required for confirmed mismatch.
        self.assertTrue("#fff" in body or "yellow" in body or "#ffd" in body, body)
        self.assertIn("background", body)

    def test_css_version_bumped(self):
        u, f = owner_farm("ui-cssver@example.com")
        c = Client()
        c.force_login(u)
        b = c.get("/attekintes/").content.decode()
        self.assertIn("portal/app.css?v=%s" % CSS_VERSION, b)


class HiddenWorkItemRegenerateTests(TestCase):
    """Regenerate preserves hidden-cycle historical WorkItems (both paths)."""

    def _seed(self, email):
        from django.utils import timezone as _tz
        u, f = owner_farm(email)
        prop = make_property(f)
        vis = CropCycle.objects.create(
            farm=f, property=prop, crop_name="VisTerv", state="planned",
            sow_date=date.today() + timedelta(days=7))
        hid = CropCycle.objects.create(
            farm=f, property=prop, crop_name="HidTerv", state="planned",
            sow_date=date.today() + timedelta(days=8),
            is_hidden=True, hidden_at=_tz.now(), hidden_reason="deleted")
        kept = WorkItem.objects.create(
            farm=f, crop_cycle=hid, property=prop, title="RejtettTortenetiMunka",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=1), estimated_hours="1.0")
        return u, f, prop, vis, hid, kept

    def test_plans_regenerate_preserves_hidden_rows(self):
        u, f, prop, vis, hid, kept = self._seed("ui-regen-plans@example.com")
        c = Client()
        c.force_login(u)
        r = c.post("/terv/", {})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(WorkItem.objects.filter(pk=kept.pk).exists())
        kept.refresh_from_db()
        self.assertEqual(kept.crop_cycle_id, hid.pk)

    def test_chat_regenerate_preserves_hidden_rows(self):
        from portal.chat import _regenerate_plans
        u, f, prop, vis, hid, kept = self._seed("ui-regen-chat@example.com")
        _regenerate_plans(f)
        self.assertTrue(WorkItem.objects.filter(pk=kept.pk).exists())
        kept.refresh_from_db()
        self.assertEqual(kept.crop_cycle_id, hid.pk)


class PlannedDateCorrectionTests(TestCase):
    """Planned sow_date required but past allowed; legacy null preserved."""

    def _payload(self, prop, sow_date):
        return {
            "property": str(prop.pk), "crop_name": "Javitando",
            "state": "planned", "growth_stage": "",
            "sow_date": sow_date, "expected_end_date": "", "observations": "",
        }

    def test_create_allows_past_sow_date(self):
        u, f = owner_farm("ui-past-create@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        past = (date.today() - timedelta(days=10)).isoformat()
        r = c.post("/cycles/new/", self._payload(prop, past))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        self.assertTrue(CropCycle.objects.filter(farm=f, crop_name="Javitando").exists())

    def test_edit_planned_allows_past_correction(self):
        u, f = owner_farm("ui-past-edit@example.com")
        prop = make_property(f)
        obj = CropCycle.objects.create(
            farm=f, property=prop, crop_name="RegiTerv", state="planned",
            sow_date=date.today() + timedelta(days=7))
        c = Client()
        c.force_login(u)
        past = (date.today() - timedelta(days=5)).isoformat()
        r = c.post("/cycles/%d/edit/" % obj.pk, self._payload(prop, past))
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        obj.refresh_from_db()
        self.assertEqual(obj.sow_date.isoformat(), past)

    def test_legacy_null_date_still_loads(self):
        u, f = owner_farm("ui-legacy-null@example.com")
        prop = make_property(f)
        CropCycle.objects.create(farm=f, property=prop, crop_name="NullDatu", state="planned")
        c = Client()
        c.force_login(u)
        r = c.get("/cycles/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "NullDatu")


class JournalCropFieldTests(TestCase):
    """Crop input rendered for all fields; missing POST stable; explicit updates."""

    def test_crop_input_rendered_and_no_fields_omitted(self):
        from portal.domain_forms import FarmingEventForm
        u, f = owner_farm("ui-crop-render@example.com")
        prop = make_property(f)
        c = Client()
        c.force_login(u)
        for url in ("/events/new/",):
            b = c.get(url).content.decode("utf-8")
            self.assertIn('name="crop_name_snapshot"', b)
            self.assertIn("Vetett n\u00f6v\u00e9ny", b)
            form = FarmingEventForm(farm=f)
            for name in form.fields:
                self.assertIn('name="%s"' % name, b, (url, name))
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, event_date=date(2024, 4, 10),
            event_type="sowing", description="Vetes",
            crop_name_snapshot="Buza", created_by=u, updated_by=u)
        b = c.get("/events/%d/edit/" % ev.pk).content.decode("utf-8")
        self.assertIn('name="crop_name_snapshot"', b)

    def test_missing_post_keeps_legacy_snapshot(self):
        u, f = owner_farm("ui-crop-missing@example.com")
        prop = make_property(f)
        cyc = CropCycle.objects.create(
            farm=f, property=prop, crop_name="CiklusNoveny", state="current")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cyc, event_date=date(2024, 4, 10),
            event_type="sowing", description="Vetes",
            crop_name_snapshot="KeziNoveny", created_by=u, updated_by=u)
        c = Client()
        c.force_login(u)
        payload = {
            "property": str(prop.pk), "crop_cycle": str(cyc.pk),
            "event_date": "2024-04-10", "event_type": "sowing",
            "description": "Modositva", "quantity": "", "unit": "",
            "materials": "", "treated_area_hectares": "", "notes": "",
            # crop_name_snapshot key intentionally absent (legacy client).
        }
        r = c.post("/events/%d/edit/" % ev.pk, payload)
        self.assertEqual(r.status_code, 302, r.content[:500] if r.status_code != 302 else "")
        ev.refresh_from_db()
        self.assertEqual(ev.crop_name_snapshot, "KeziNoveny")
        self.assertEqual(ev.description, "Modositva")

    def test_explicit_crop_and_link_changes_update(self):
        from portal.test_sowing_rotation import event_payload
        u, f = owner_farm("ui-crop-explicit@example.com")
        prop = make_property(f)
        c1 = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Elso", state="current")
        c2 = CropCycle.objects.create(
            farm=f, property=prop, crop_name="Masodik", state="planned",
            sow_date=date.today() + timedelta(days=7))
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, c1, crop_name_snapshot="Kezi"))
        self.assertEqual(r.status_code, 302)
        ev = FarmingEvent.objects.get(farm=f)
        self.assertEqual(ev.crop_name_snapshot, "Kezi")
        # Explicit new crop wins over the link.
        r = c.post("/events/%d/edit/" % ev.pk, event_payload(
            prop, c1, crop_name_snapshot="UjKezi", description="M1"))
        self.assertEqual(r.status_code, 302)
        ev.refresh_from_db()
        self.assertEqual(ev.crop_name_snapshot, "UjKezi")
        # Blank crop plus explicit relink derives from the new link.
        r = c.post("/events/%d/edit/" % ev.pk, event_payload(prop, c2))
        self.assertEqual(r.status_code, 302)
        ev.refresh_from_db()
        self.assertEqual(ev.crop_cycle_id, c2.pk)
        self.assertEqual(ev.crop_name_snapshot, "Masodik")


class RotationSortingTests(TestCase):
    """Newest first per property in each section; filters tenant-safe."""

    def test_sections_descending_and_legacy_date_desc(self):
        u, f = owner_farm("ui-sort@example.com")
        loc = make_location(f, name="L-sort")
        pa = make_property(f, loc, name="SortPA")
        FarmingEvent.objects.create(
            farm=f, property=pa, event_date=date(2024, 3, 5),
            event_type="sowing", description="Regi",
            crop_name_snapshot="RegiKuk", created_by=u, updated_by=u)
        FarmingEvent.objects.create(
            farm=f, property=pa, event_date=date(2024, 5, 20),
            event_type="sowing", description="Uj",
            crop_name_snapshot="UjBuza", created_by=u, updated_by=u)
        CropCycle.objects.create(
            farm=f, property=pa, crop_name="TervKorai", state="planned",
            sow_date=date.today() + timedelta(days=7))
        CropCycle.objects.create(
            farm=f, property=pa, crop_name="TervKesoi", state="planned",
            sow_date=date.today() + timedelta(days=21))
        # Legacy ids reversed vs dates: date-desc must win over id order.
        old_legacy = CropCycle.objects.create(
            farm=f, property=pa, crop_name="LegacyUjDatu", state="history",
            sow_date=date(2024, 6, 1))
        new_legacy = CropCycle.objects.create(
            farm=f, property=pa, crop_name="LegacyRegiDatu", state="history",
            sow_date=date(2023, 6, 1))
        self.assertLess(old_legacy.pk, new_legacy.pk)
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertLess(b.index("UjBuza"), b.index("RegiKuk"))
        self.assertLess(b.index("TervKesoi"), b.index("TervKorai"))
        self.assertLess(b.index("LegacyUjDatu"), b.index("LegacyRegiDatu"))

    def test_journal_date_filter_tenant_safe(self):
        u, f = owner_farm("ui-datefilter@example.com")
        u2, f2 = owner_farm("ui-datefilter2@example.com")
        pa = make_property(f, name="Sajat")
        pb = make_property(f2, name="Idegen")
        FarmingEvent.objects.create(
            farm=f, property=pa, event_date=date(2024, 4, 10),
            event_type="sowing", description="SajatNaplo",
            crop_name_snapshot="Buza", created_by=u, updated_by=u)
        FarmingEvent.objects.create(
            farm=f2, property=pb, event_date=date(2024, 4, 10),
            event_type="sowing", description="IdegenNaplo",
            crop_name_snapshot="Buza", created_by=u2, updated_by=u2)
        c = Client()
        c.force_login(u)
        b = c.get("/events/", {"date_from": "2024-04-01",
                               "date_to": "2024-04-30"}).content.decode("utf-8")
        self.assertIn("SajatNaplo", b)
        self.assertNotIn("IdegenNaplo", b)


class RotationI18nTests(TestCase):
    """HU/EN rotation review strings without unrelated rewrites."""

    def test_hu_and_en_review_headings(self):
        u, f = owner_farm("ui-i18n@example.com")
        prop = make_property(f)
        row, _, _ = seed_pending(f, u, prop)
        confirm_mismatch(row)
        seed_pending(f, u, prop, actual_crop="Arpa", plan_crop="Arpa")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        for hu in ("Ellen\u0151rz\u00e9s alatt", "Meger\u0151s\u00edtett elt\u00e9r\u00e9s"):
            self.assertIn(hu, b)
        c.post("/i18n/setlang/", {"language": "en", "next": "/cycles/"})
        b = c.get("/cycles/").content.decode("utf-8")
        for en in ("Pending review", "Confirmed mismatch"):
            self.assertIn(en, b)
