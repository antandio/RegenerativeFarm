"""Task12 reconciliation service behavior tests (RED then GREEN).

English comments/docstrings; HU/EN UI preserved via catalogs.
External provider calls are mocked only; no network in tests.
"""
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from portal.models import (
    AuditEvent, ConsentEvent, CropCycle, Farm, FarmingEvent,
    FarmLocation, FarmProperty, Membership, WorkItem,
)

User = get_user_model()
PW = "TesztJelszo123!"


def make_user(email, pw=PW):
    return User.objects.create_user(email=email, password=pw)


def owner_farm(email):
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


def grant_consent(farm, owner):
    return ConsentEvent.objects.create(farm=farm, actor=owner, event_type="granted")


def revoke_consent(farm, owner):
    return ConsentEvent.objects.create(farm=farm, actor=owner, event_type="revoked")


def make_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def event_payload(prop, cycle=None, **over):
    d = {
        "property": str(prop.pk),
        "crop_cycle": str(cycle.pk) if cycle else "",
        "crop_name_snapshot": "",
        "event_date": date.today().isoformat(),
        "event_type": "sowing",
        "description": "Tavaszi vetes",
        "quantity": "",
        "unit": "",
        "materials": "",
        "treated_area_hectares": "",
        "notes": "",
    }
    d.update(over)
    return d


def get_sowing_model(tc):
    """Return SowingComparison model or fail with assertion (RED)."""
    try:
        m = apps.get_model("portal", "SowingComparison")
    except LookupError:
        tc.fail("SowingComparison model missing (RED)")
    tc.assertIsNotNone(m, "SowingComparison model missing (RED)")
    return m


def get_sowing_service(tc):
    """Return crop_sowing module or fail with assertion (RED)."""
    try:
        from portal import crop_sowing as svc
    except ImportError:
        tc.fail("portal.crop_sowing missing (RED)")
    for name in ("enqueue_sowing_reconciliation", "authorize_sowing_dispatch",
                 "execute_sowing_comparison", "find_candidate",
                 "validate_sowing_parsed", "retry_sowing_comparison"):
        tc.assertTrue(hasattr(svc, name), "crop_sowing.%s missing (RED)" % name)
    return svc


def get_sowing_provider(tc):
    """Assert the existing client gained the sowing method (RED)."""
    from portal.provider import MetaProviderClient
    tc.assertTrue(hasattr(MetaProviderClient, "generate_sowing_comparison"),
                  "MetaProviderClient.generate_sowing_comparison missing (RED)")
    return MetaProviderClient


def get_sowing_worker(tc):
    """Return sowing_jobs module or fail with assertion (RED)."""
    try:
        from portal import sowing_jobs as w
    except ImportError:
        tc.fail("portal.sowing_jobs missing (RED)")
    for name in ("schedule_sowing_comparison", "_run_sowing_job", "_fail_safe"):
        tc.assertTrue(hasattr(w, name), "sowing_jobs.%s missing (RED)" % name)
    return w


class FakeSowingClient:
    """Configurable provider double for sowing comparison."""

    def __init__(self, decision="match", reason="crop_date_match",
                 wrong_id=False, exc=None, on_call=None,
                 model="test-sowing-model", usage_in=11, usage_out=13):
        self.calls = 0
        self.last = None
        self.decision = decision
        self.reason = reason
        self.wrong_id = wrong_id
        self.exc = exc
        self.on_call = on_call
        self.model = model
        self.usage_in = usage_in
        self.usage_out = usage_out

    def generate_sowing_comparison(self, actual_crop, actual_date,
                                   planned_cycle_id, planned_crop,
                                   planned_sow_date, delta_days, language=None):
        self.calls += 1
        self.last = {
            "actual_crop": actual_crop, "actual_date": actual_date,
            "planned_cycle_id": planned_cycle_id, "planned_crop": planned_crop,
            "planned_sow_date": planned_sow_date, "delta_days": delta_days,
        }
        if self.on_call is not None:
            self.on_call()
        if self.exc is not None:
            raise self.exc
        pid = 999999999 if self.wrong_id else planned_cycle_id
        return {
            "decision": self.decision,
            "planned_cycle_id": pid,
            "reason_code": self.reason,
            "_actual_model": self.model,
            "_usage_input_tokens": self.usage_in,
            "_usage_output_tokens": self.usage_out,
        }


def seed_candidate(farm, prop, name="Buza", days_offset=5, hidden=False):
    return CropCycle.objects.create(
        farm=farm, property=prop, crop_name=name, state="planned",
        sow_date=date.today() + timedelta(days=days_offset),
        is_hidden=hidden)


class EnqueueDisciplineTests(TestCase):
    """DB-only enqueue on journal create/edit; no network in transaction."""

    def test_enqueue_creates_pending_without_provider_call_during_post(self):
        M = get_sowing_model(self)
        get_sowing_provider(self)
        u, f = owner_farm("rec-enq1@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        seed_candidate(f, prop, name="Buza", days_offset=5)
        c = Client()
        c.force_login(u)
        with patch("portal.provider.MetaProviderClient.generate_sowing_comparison") as m:
            r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
            self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
            m.assert_not_called()
        row = M.objects.filter(farm=f).first()
        self.assertIsNotNone(row, "sowing comparison must be enqueued")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.review_state, "pending_review")
        self.assertIsNone(row.dispatch_authorized_at)
        self.assertEqual(row.crop_snapshot, "Buza")

    def test_enqueue_outside_window_creates_nothing(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq2@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        seed_candidate(f, prop, name="Buza", days_offset=60)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 0)

    def test_enqueue_cross_property_creates_nothing(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq3@example.com")
        grant_consent(f, u)
        loc = make_location(f, name="L-xprop")
        pa = make_property(f, loc, name="PA")
        pb = make_property(f, loc, name="PB")
        seed_candidate(f, pb, name="Buza", days_offset=3)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(pa, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 0)

    def test_enqueue_cross_farm_invisible(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq4@example.com")
        u2, f2 = owner_farm("rec-enq4b@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        other_prop = make_property(f2, name="Idegen")
        seed_candidate(f2, other_prop, name="Buza", days_offset=2)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 0)
        self.assertEqual(M.objects.filter(farm=f2).count(), 0)

    def test_enqueue_one_event_at_most_one_plan_nearest(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq5@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        near = seed_candidate(f, prop, name="Buza", days_offset=2)
        far = seed_candidate(f, prop, name="Arpa", days_offset=10)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        rows = list(M.objects.filter(farm=f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].planned_cycle_id, near.pk)

    def test_enqueue_prefers_explicit_linked_valid(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq6@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        linked = seed_candidate(f, prop, name="Buza", days_offset=20)
        nearer = seed_candidate(f, prop, name="Buza", days_offset=2)
        self.assertNotEqual(linked.pk, nearer.pk)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(
            prop, linked, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        row = M.objects.filter(farm=f).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.planned_cycle_id, linked.pk)

    def test_enqueue_invalid_link_falls_back_to_nearest(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq7@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        bad = seed_candidate(f, prop, name="Buza", days_offset=60)
        good = seed_candidate(f, prop, name="Buza", days_offset=4)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(
            prop, bad, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        row = M.objects.filter(farm=f).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.planned_cycle_id, good.pk)

    def test_enqueue_non_sowing_noop(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq8@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        seed_candidate(f, prop, name="Buza", days_offset=3)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, event_type="spraying"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 0)

    def test_enqueue_worker_stays_pending_no_dispatch(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f = owner_farm("rec-enq9@example.com")
        grant_consent(f, u)
        worker = make_member(f, "rec-enq9w@example.com", "worker")
        prop = make_property(f)
        seed_candidate(f, prop, name="Buza", days_offset=3)
        c = Client()
        c.force_login(worker)
        callbacks = []
        with patch("django.db.transaction.on_commit",
                   side_effect=lambda cb, *a, **kw: callbacks.append(cb)):
            r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        row = M.objects.filter(farm=f).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "pending")
        self.assertIsNone(row.dispatch_authorized_at)
        self.assertEqual(callbacks, [], "worker save must not schedule paid work")

    def test_enqueue_blocked_consent_stays_pending_no_dispatch(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq10@example.com")
        prop = make_property(f)
        seed_candidate(f, prop, name="Buza", days_offset=3)
        c = Client()
        c.force_login(u)
        callbacks = []
        with patch("django.db.transaction.on_commit",
                   side_effect=lambda cb, *a, **kw: callbacks.append(cb)):
            r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        row = M.objects.filter(farm=f).first()
        self.assertIsNotNone(row)
        self.assertIsNone(row.dispatch_authorized_at)
        self.assertEqual(callbacks, [], "missing consent must not schedule paid work")

    def test_enqueue_on_edit_after_save(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f = owner_farm("rec-enq11@example.com")
        grant_consent(f, u)
        loc = make_location(f, name="L-edit")
        pa = make_property(f, loc, name="PA")
        pb = make_property(f, loc, name="PB")
        seed_candidate(f, pb, name="Buza", days_offset=3)
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(pa, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 0)
        ev = FarmingEvent.objects.get(farm=f)
        r = c.post(f"/events/{ev.pk}/edit/", event_payload(pb, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        self.assertEqual(M.objects.filter(farm=f).count(), 1)


class IdempotenceStaleTests(TestCase):
    """Unchanged repeats create nothing; stale edits supersede safely."""

    def _seed(self, email):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        return u, f, prop, cand

    def test_identical_edit_no_duplicate_no_new_paid(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-idem1@example.com")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        first = M.objects.get(farm=f)
        key1, hash1 = first.idempotency_key, first.input_hash
        ev = FarmingEvent.objects.get(farm=f)
        r = c.post(f"/events/{ev.pk}/edit/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.idempotency_key, key1)
        self.assertEqual(first.input_hash, hash1)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(first, client=fake)
        self.assertEqual(fake.calls, 1)
        again = svc.execute_sowing_comparison(first, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(again.status, done.status)

    def test_description_only_edit_keeps_hash_no_new_job(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-idem2@example.com")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        first = M.objects.get(farm=f)
        ev = FarmingEvent.objects.get(farm=f)
        r = c.post(f"/events/{ev.pk}/edit/", event_payload(
            prop, crop_name_snapshot="Buza", description="Mas leiras",
            notes="Mas jegyzet", materials="Mas anyag"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(M.objects.filter(farm=f).count(), 1)
        first.refresh_from_db()
        ev.refresh_from_db()
        self.assertEqual(ev.description, "Mas leiras")
        # Only crop snapshot/date feed the hash; free text is ignored.
        self.assertEqual(M.objects.get(farm=f).input_hash, first.input_hash)

    def test_stale_edit_supersedes_pending_old_cannot_mutate(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-stale1@example.com")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        old = M.objects.get(farm=f)
        old_pk = old.pk
        ev = FarmingEvent.objects.get(farm=f)
        r = c.post(f"/events/{ev.pk}/edit/", event_payload(
            prop, crop_name_snapshot="Kukorica"))
        self.assertEqual(r.status_code, 302)
        old.refresh_from_db()
        self.assertEqual(old.status, "superseded")
        self.assertEqual(old.review_state, "superseded")
        rows = list(M.objects.filter(farm=f).order_by("id"))
        self.assertEqual(len(rows), 2)
        new = [x for x in rows if x.pk != old_pk][0]
        self.assertEqual(new.status, "pending")
        self.assertNotEqual(new.input_hash, old.input_hash)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done_old = svc.execute_sowing_comparison(old_pk, client=fake)
        self.assertEqual(fake.calls, 0, "stale result must not call provider")
        self.assertEqual(done_old.status, "superseded")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_prior_match_hidden_not_undone_by_later_edit(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-stale2@example.com")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        row = M.objects.get(farm=f)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(done.decision, "match")
        cand.refresh_from_db()
        self.assertTrue(cand.is_hidden)
        ev = FarmingEvent.objects.get(farm=f)
        r = c.post(f"/events/{ev.pk}/edit/", event_payload(
            prop, crop_name_snapshot="Buza", description="Kesobbi modositas"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        cand.refresh_from_db()
        self.assertTrue(cand.is_hidden, "old edits must not undo hidden decisions")
        # Unchanged hash: no new paid job for the same pair.
        self.assertEqual(M.objects.filter(farm=f).count(), 1)


class ProviderContractTests(TestCase):
    """Existing client method: minimal payload, strict enums, safe rejects."""

    def test_provider_minimal_payload_same_client_no_tools(self):
        import httpx
        ClientCls = get_sowing_provider(self)
        from django.conf import settings
        seen = {}
        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode("utf-8"))
            payload = {"decision": "match", "planned_cycle_id": 42,
                       "reason_code": "crop_date_match"}
            content = json.dumps(payload)
            return httpx.Response(200, json={
                "model": "test-model-x",
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                "choices": [{"message": {"content": content}}],
            })
        transport = httpx.MockTransport(handler)
        client = ClientCls(api_key="k-test", base_url="https://test.example/v1",
                           model="m-test", transport=transport)
        out = client.generate_sowing_comparison(
            "Buza", date.today().isoformat(), 42, "Buza",
            (date.today() + timedelta(days=3)).isoformat(), 3)
        self.assertEqual(out["decision"], "match")
        self.assertEqual(out["planned_cycle_id"], 42)
        self.assertEqual(out["_actual_model"], "test-model-x")
        self.assertEqual(out["_usage_input_tokens"], 5)
        body = seen["body"]
        self.assertEqual(body["model"], "m-test")
        self.assertTrue(seen["url"].startswith("https://test.example/v1/chat/completions"))
        self.assertNotIn("tools", body)
        self.assertNotIn("functions", body)
        self.assertLessEqual(int(body.get("max_tokens", 0)), 600)
        system = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        self.assertIn("data", system.lower())
        user_blob = " ".join(m["content"] for m in body["messages"] if m["role"] == "user")
        self.assertIn("Buza", user_blob)
        self.assertIn("42", user_blob)
        for banned in ("TesztJelszo", "owner@", "Leiras", "jegyzet", "notes", "farm"):
            self.assertNotIn(banned, user_blob)
        # Defaults reuse the configured app client; never Muse credentials.
        default_client = ClientCls()
        self.assertEqual(default_client.base_url, settings.META_API_BASE_URL.rstrip("/"))
        self.assertEqual(default_client.model, settings.META_MODEL)

    def test_provider_strict_validation_rejects_bad(self):
        svc = get_sowing_service(self)
        from portal.provider import ProviderInvalidOutput
        good = {"decision": "match", "planned_cycle_id": 7,
                "reason_code": "crop_date_match"}
        d, r, pid = svc.validate_sowing_parsed(dict(good), 7)
        self.assertEqual((d, r, pid), ("match", "crop_date_match", 7))
        bad_cases = [
            {"decision": True, "planned_cycle_id": 7, "reason_code": "crop_date_match"},
            {"decision": "match", "planned_cycle_id": True, "reason_code": "crop_date_match"},
            {"decision": "match", "planned_cycle_id": 8, "reason_code": "crop_date_match"},
            {"decision": "maybe", "planned_cycle_id": 7, "reason_code": "crop_date_match"},
            {"decision": "match", "planned_cycle_id": 7, "reason_code": "free text ok"},
            {"decision": "match", "planned_cycle_id": 7},
            {"planned_cycle_id": 7, "reason_code": "crop_date_match"},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "hide": True},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "delete": 1},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "sql": "x"},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "execute": "x"},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "_usage_input_tokens": True},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "_usage_input_tokens": 2 ** 100},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "_actual_model": "x" * 200},
            {"decision": "match", "planned_cycle_id": 7,
             "reason_code": "crop_date_match", "_actual_model": 123},
            "not-a-dict",
            None,
        ]
        for i, bad in enumerate(bad_cases):
            with self.assertRaises(ProviderInvalidOutput, msg="case %d: %r" % (i, bad)):
                svc.validate_sowing_parsed(bad, 7)


class ExecutionDecisionTests(TestCase):
    """Fake match/mismatch/uncertain/error with preservation and audit."""

    def _seed(self, email, crop="Buza"):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name=crop, days_offset=4)
        return u, f, prop, cand

    def _enqueued(self, svc, M, f, prop, cand, actor, crop="Buza"):
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot=crop,
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=actor, updated_by=actor)
        row = svc.enqueue_sowing_reconciliation(f, ev, actor)
        self.assertIsNotNone(row)
        return ev, M.objects.get(pk=row.pk)

    def test_match_hides_one_plan_preserving_workitems_journal_audit(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-exec1@example.com")
        other = seed_candidate(f, prop, name="Arpa", days_offset=6)
        w = WorkItem.objects.create(
            farm=f, crop_cycle=cand, property=prop, title="Vetes",
            capability="sowing", start_date=date.today(),
            end_date=date.today() + timedelta(days=2), estimated_hours="2.0")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(done.status, "success")
        self.assertEqual(done.decision, "match")
        self.assertEqual(done.review_state, "resolved_matched")
        cand.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(cand.is_hidden)
        self.assertEqual(cand.hidden_reason, "sowing_match")
        self.assertIsNotNone(cand.hidden_at)
        self.assertFalse(other.is_hidden, "only one plan may hide")
        self.assertTrue(WorkItem.objects.filter(pk=w.pk).exists())
        self.assertTrue(FarmingEvent.objects.filter(pk=ev.pk).exists())
        ev.refresh_from_db()
        self.assertEqual(ev.crop_cycle_id, cand.pk)
        audit = AuditEvent.objects.filter(farm=f, action="sowing.match.hide").first()
        self.assertIsNotNone(audit, "match hide must audit")
        self.assertNotIn("Vetes", json.dumps(audit.meta, ensure_ascii=False))

    def test_mismatch_yellow_review_no_hide(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-exec2@example.com", crop="Buza")
        ev, row = self._enqueued(svc, M, f, prop, cand, u, crop="Kukorica")
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(done.status, "success")
        self.assertEqual(done.decision, "mismatch")
        self.assertEqual(done.review_state, "confirmed_mismatch")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_uncertain_stays_pending_no_hide(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-exec3@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        fake = FakeSowingClient(decision="uncertain", reason="ambiguous")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(done.decision, "uncertain")
        self.assertEqual(done.review_state, "pending_review")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_provider_error_stays_pending_safe_code_no_raw(self):
        from portal.provider import ProviderError, ProviderTimeout, ProviderInvalidOutput
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        for i, exc in enumerate([ProviderError("RAW-SECRET-BOOM"),
                                 ProviderTimeout("RAW-TIMEOUT"),
                                 ProviderInvalidOutput("RAW-BAD")]):
            u, f, prop, cand = self._seed("rec-err%d@example.com" % i)
            ev, row = self._enqueued(svc, M, f, prop, cand, u)
            fake = FakeSowingClient(exc=exc)
            done = svc.execute_sowing_comparison(row, client=fake)
            self.assertEqual(done.status, "failed")
            self.assertEqual(done.review_state, "pending_review")
            self.assertNotIn("RAW", (done.error_message_safe or ""))
            self.assertNotIn("RAW", (done.error_code or ""))
            cand.refresh_from_db()
            self.assertFalse(cand.is_hidden)

    def test_consent_revoked_before_dispatch_no_call(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-rev1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        revoke_consent(f, u)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(done.status, "failed")
        self.assertEqual(done.error_code, "consent_withdrawn_before_call")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_revoked_in_flight_discards_mutation(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-rev2@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        def _revoke():
            revoke_consent(f, u)
        fake = FakeSowingClient(decision="match", reason="crop_date_match",
                                on_call=_revoke)
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(done.status, "failed")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden, "in-flight revoke must discard mutation")
        self.assertEqual(done.review_state, "pending_review")

    def test_worker_foreign_role_blocked_no_call(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-role1@example.com")
        worker = make_member(f, "rec-role1w@example.com", "worker")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=worker, updated_by=worker)
        row = svc.enqueue_sowing_reconciliation(f, ev, worker)
        self.assertIsNotNone(row)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(done.status, "failed")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_cross_tenant_property_tamper_no_mutation(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-scope1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        other_prop = make_property(f, name="Masik")
        FarmingEvent.objects.filter(pk=ev.pk).update(property_id=other_prop.pk)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(done.status, "superseded")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_exactly_once_no_duplicate_concurrent(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-once1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        first = svc.execute_sowing_comparison(row, client=fake)
        second = svc.execute_sowing_comparison(row.pk, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(first.status, second.status)
        self.assertFalse(svc.authorize_sowing_dispatch(second))

    def test_model_usage_valid_bounded_unknown_not_zero(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-use1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch",
                                model=None, usage_in=None, usage_out=None)
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(done.status, "success")
        self.assertIsNone(done.actual_model)
        self.assertIsNone(done.usage_input_tokens)
        self.assertIsNone(done.usage_output_tokens)
        u2, f2, prop2, cand2 = self._seed("rec-use2@example.com")
        ev2, row2 = self._enqueued(svc, M, f2, prop2, cand2, u2)
        fake2 = FakeSowingClient(decision="mismatch", reason="crop_mismatch",
                                 usage_in=True)
        done2 = svc.execute_sowing_comparison(row2, client=fake2)
        self.assertEqual(done2.status, "failed")
        self.assertEqual(done2.error_code, "invalid_structured_output")


class QuotaWorkerRetryTests(TestCase):
    """Shared quota, bounded worker, explicit retry, no GET scheduling."""

    def _seed(self, email):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        return u, f, prop, cand

    def _enqueued(self, svc, M, f, prop, cand, actor):
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=actor, updated_by=actor)
        row = svc.enqueue_sowing_reconciliation(f, ev, actor)
        return ev, M.objects.get(pk=row.pk)

    @override_settings(ADVICE_MAX_PER_HOUR_PER_FARM=1, ADVICE_MAX_PER_DAY_PER_FARM=10,
                       ADVICE_GLOBAL_MAX_PER_DAY=100)
    def test_quota_counts_dispatched_failures_shared(self):
        from portal.models import AdviceRun, FarmProfileSnapshot
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-quota1@example.com")
        snap, _ = FarmProfileSnapshot.objects.get_or_create(
            farm=f, profile_version=f.profile_version,
            defaults={"profile_json": {"name": f.name}, "profile_schema_version": 1})
        AdviceRun.objects.create(
            farm=f, initiated_by=u, profile_snapshot=snap,
            idempotency_key="quota-fill-1", status="failed",
            error_code="provider_error", requested_model="m",
            attempt_started_at=timezone.now())
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(done.status, "failed")
        self.assertEqual(done.error_code, "quota_exceeded")

    @override_settings(ADVICE_MAX_PER_HOUR_PER_FARM=5, ADVICE_MAX_PER_DAY_PER_FARM=50,
                       ADVICE_GLOBAL_MAX_PER_DAY=500)
    def test_pending_undispatched_not_consuming_quota(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-quota2@example.com")
        worker = make_member(f, "rec-quota2w@example.com", "worker")
        for i in range(5):
            ev = FarmingEvent.objects.create(
                farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
                event_date=date.today(), event_type="sowing",
                description="W%d" % i, created_by=worker, updated_by=worker)
            # Worker rows stay undispatched and must not spend quota.
            r = svc.enqueue_sowing_reconciliation(f, ev, worker)
            self.assertIsNotNone(r)
        self.assertEqual(M.objects.filter(
            farm=f, dispatch_authorized_at__isnull=True).count(), 5)
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(row, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(done.status, "success")

    def test_bounded_worker_saturation_stays_pending_no_hide(self):
        # Queued reservation under saturation stays pending and recoverable.
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        w = get_sowing_worker(self)
        u, f, prop, cand = self._seed("rec-work1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        self.assertIsNotNone(row.queued_at)
        self.assertTrue(w._slot.acquire(blocking=False))
        try:
            with patch("portal.sowing_jobs._executor") as exc:
                self.assertFalse(w.schedule_sowing_comparison(row.pk))
                exc.submit.assert_not_called()
        finally:
            w._slot.release()
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.review_state, "pending_review")
        self.assertIsNone(row.queued_at)
        self.assertIsNone(row.current_attempt_id)
        from portal.models import SowingComparisonAttempt
        self.assertEqual(
            SowingComparisonAttempt.objects.filter(
                comparison_id=row.pk).count(), 0)
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_crash_stale_requires_explicit_retry(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, cand = self._seed("rec-retry1@example.com")
        worker = make_member(f, "rec-retry1w@example.com", "worker")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        from portal.provider import ProviderError
        fake_err = FakeSowingClient(exc=ProviderError("boom"))
        failed = svc.execute_sowing_comparison(row, client=fake_err)
        self.assertEqual(failed.status, "failed")
        # Worker retry is forbidden; owner retry reopens the same review
        # identity and schedules a fresh immutable attempt on next execute.
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            svc.retry_sowing_comparison(worker, f, row.pk)
        with patch("portal.sowing_jobs.schedule_sowing_comparison") as sched:
            retried, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
            self.assertTrue(scheduled)
            self.assertEqual(retried.pk, row.pk)
            self.assertEqual(retried.status, "pending")
        fake_ok = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(retried.pk, client=fake_ok)
        self.assertEqual(done.status, "success")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)

    def test_get_never_schedules_or_calls(self):
        M = get_sowing_model(self)
        get_sowing_service(self)
        get_sowing_provider(self)
        u, f, prop, cand = self._seed("rec-get1@example.com")
        FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=u, updated_by=u)
        before = M.objects.filter(farm=f).count()
        c = Client()
        c.force_login(u)
        with patch("portal.provider.MetaProviderClient.generate_sowing_comparison") as m:
            self.assertEqual(c.get("/events/").status_code, 200)
            self.assertEqual(c.get("/cycles/").status_code, 200)
            m.assert_not_called()
        self.assertEqual(M.objects.filter(farm=f).count(), before)


class QuotaFailClosedTests(TestCase):
    """Controller finding 1: sowing quota fails closed and counts dispatch."""

    def _seed(self, email):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        return u, f, prop, cand

    @override_settings(ADVICE_MAX_PER_HOUR_PER_FARM=1, ADVICE_MAX_PER_DAY_PER_FARM=10,
                       ADVICE_GLOBAL_MAX_PER_DAY=100)
    def test_sowing_counts_use_dispatch_timestamp_not_enqueue_age(self):
        M = get_sowing_model(self)
        from portal.quota import QuotaExceeded, check_combined_quotas
        u, f, prop, cand = self._seed("rec-qfc1@example.com")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=u, updated_by=u)
        from portal.crop_sowing import canonical_input_hash
        from portal.models import SowingComparisonAttempt
        # Months-old pending row retried now: enqueued long ago, dispatched now.
        row = M.objects.create(
            farm=f, initiated_by=u, farming_event=ev, planned_cycle=cand,
            property=prop, crop_snapshot="Buza", event_date=ev.event_date,
            planned_crop_snapshot=cand.crop_name, planned_sow_date=cand.sow_date,
            input_hash=canonical_input_hash(
                f.pk, ev.pk, prop.pk, "Buza", ev.event_date,
                cand.pk, cand.crop_name, cand.sow_date),
            idempotency_key="qfc-old-pending", language="hu",
            status="pending", review_state="pending_review",
            requested_model="m-test",
            attempt_started_at=timezone.now() - timedelta(days=90),
            dispatch_authorized_at=timezone.now())
        SowingComparisonAttempt.objects.create(
            comparison=row, farm=f, initiated_by=u,
            attempt_token="qfc-old-pending-token",
            dispatched_at=timezone.now(), requested_model="m-test")
        with self.assertRaises(QuotaExceeded):
            check_combined_quotas(f)

    def test_sowing_count_db_failure_raises_fail_closed(self):
        from django.db.utils import OperationalError
        from portal import quota as quota_mod
        u, f = owner_farm("rec-qfc2@example.com")
        now = timezone.now()
        with patch("portal.models.SowingComparisonAttempt") as MockSowing:
            MockSowing.objects.filter.side_effect = OperationalError("db down")
            with self.assertRaises(OperationalError):
                quota_mod._sowing_counts(
                    f.pk, now - timedelta(hours=1), now - timedelta(hours=24))
            with self.assertRaises(OperationalError):
                quota_mod.check_combined_quotas(f)


class StrictValidationTests(TestCase):
    """Controller finding 2: exact allowlist plus semantic enum consistency."""

    def test_rejects_unknown_extra_keys(self):
        svc = get_sowing_service(self)
        from portal.provider import ProviderInvalidOutput
        base = {"decision": "match", "planned_cycle_id": 7,
                "reason_code": "crop_date_match"}
        for extra in ({"extra_field": 1}, {"note": "hi"},
                      {"decision2": "match"}, {"planned_cycle_id2": 7},
                      {"__proto__": 1}, {"action": "hide"}):
            bad = dict(base)
            bad.update(extra)
            with self.assertRaises(ProviderInvalidOutput, msg=extra):
                svc.validate_sowing_parsed(bad, 7)
        ok = dict(base)
        ok.update({"_actual_model": "m", "_usage_input_tokens": 3,
                   "_usage_output_tokens": 4})
        self.assertEqual(svc.validate_sowing_parsed(ok, 7)[0], "match")

    def test_rejects_semantically_inconsistent_enums(self):
        svc = get_sowing_service(self)
        from portal.provider import ProviderInvalidOutput
        inconsistent = [
            ("match", "crop_mismatch"),
            ("match", "date_mismatch"),
            ("match", "ambiguous"),
            ("match", "provider_uncertain"),
            ("mismatch", "crop_date_match"),
            ("mismatch", "ambiguous"),
            ("mismatch", "provider_uncertain"),
            ("uncertain", "crop_date_match"),
            ("uncertain", "crop_mismatch"),
            ("uncertain", "date_mismatch"),
        ]
        for dec, reason in inconsistent:
            bad = {"decision": dec, "planned_cycle_id": 7,
                   "reason_code": reason}
            with self.assertRaises(ProviderInvalidOutput, msg=(dec, reason)):
                svc.validate_sowing_parsed(bad, 7)
        consistent = [
            ("match", "crop_date_match"),
            ("mismatch", "crop_mismatch"),
            ("mismatch", "date_mismatch"),
            ("uncertain", "ambiguous"),
            ("uncertain", "provider_uncertain"),
        ]
        for dec, reason in consistent:
            good = {"decision": dec, "planned_cycle_id": 7,
                    "reason_code": reason}
            self.assertEqual(
                svc.validate_sowing_parsed(good, 7)[:2], (dec, reason))


class LifecycleIdempotenceTests(TestCase):
    """Controller finding 3: one actual hides at most one plan, ever."""

    def _seed2(self, email):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        near = seed_candidate(f, prop, name="Buza", days_offset=2)
        far = seed_candidate(f, prop, name="Buza", days_offset=9)
        return u, f, prop, near, far

    def test_repeated_save_after_match_consumes_no_second_plan(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, near, far = self._seed2("rec-life1@example.com")
        c = Client()
        c.force_login(u)
        r = c.post("/events/new/", event_payload(prop, crop_name_snapshot="Buza"))
        self.assertEqual(r.status_code, 302)
        row = M.objects.get(farm=f)
        self.assertEqual(row.planned_cycle_id, near.pk)
        done = svc.execute_sowing_comparison(
            row, client=FakeSowingClient(decision="match", reason="crop_date_match"))
        self.assertEqual(done.review_state, "resolved_matched")
        near.refresh_from_db()
        self.assertTrue(near.is_hidden)
        # Later edit with changed identity must not consume the next plan.
        ev = FarmingEvent.objects.get(farm=f)
        r = c.post(f"/events/{ev.pk}/edit/", event_payload(
            prop, crop_name_snapshot="Kukorica", description="Kesobbi modositas"))
        self.assertEqual(r.status_code, 302, r.content[:400] if r.status_code != 302 else b"")
        self.assertEqual(M.objects.filter(farm=f).count(), 1)
        far.refresh_from_db()
        self.assertFalse(far.is_hidden)
        near.refresh_from_db()
        self.assertTrue(near.is_hidden)

    def test_second_row_for_consumed_actual_cannot_hide(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f, prop, near, far = self._seed2("rec-life2@example.com")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=near, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=u, updated_by=u)
        first = svc.enqueue_sowing_reconciliation(f, ev, u)
        self.assertIsNotNone(first)
        self.assertEqual(first.planned_cycle_id, near.pk)
        # Simulate a raced second row for another plan of the same actual.
        second = M.objects.create(
            farm=f, initiated_by=u, farming_event=ev, planned_cycle=far,
            property=prop, crop_snapshot="Buza", event_date=ev.event_date,
            planned_crop_snapshot=far.crop_name, planned_sow_date=far.sow_date,
            input_hash=svc.canonical_input_hash(
                f.pk, ev.pk, prop.pk, "Buza", ev.event_date,
                far.pk, far.crop_name, far.sow_date),
            idempotency_key="race-second-%s" % ev.pk, language="hu",
            status="pending", review_state="pending_review",
            requested_model="m-test", attempt_started_at=timezone.now())
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done1 = svc.execute_sowing_comparison(first.pk, client=fake)
        self.assertEqual(done1.review_state, "resolved_matched")
        done2 = svc.execute_sowing_comparison(second.pk, client=fake)
        self.assertEqual(fake.calls, 1, "consumed actual must not dispatch again")
        self.assertNotEqual(done2.review_state, "resolved_matched")
        far.refresh_from_db()
        self.assertFalse(far.is_hidden)
        near.refresh_from_db()
        self.assertTrue(near.is_hidden)


class RetryActorTests(TestCase):
    """Controller finding 5: owner retry of a worker row adopts the actor."""

    def test_owner_retry_of_worker_row_updates_actor_and_dispatches(self):
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f = owner_farm("rec-retryactor1@example.com")
        grant_consent(f, u)
        worker = make_member(f, "rec-retryactor1w@example.com", "worker")
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=worker, updated_by=worker)
        row = svc.enqueue_sowing_reconciliation(f, ev, worker)
        self.assertIsNotNone(row)
        self.assertEqual(M.objects.get(pk=row.pk).initiated_by_id, worker.pk)
        retried, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
        self.assertTrue(scheduled)
        self.assertEqual(retried.pk, row.pk)
        retried.refresh_from_db()
        self.assertEqual(retried.initiated_by_id, u.pk)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(retried.pk, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(done.status, "success")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)


class NoHttpInTransactionTests(TestCase):
    """Controller finding 4 lock-in: provider call outside any write tx."""

    def test_provider_call_outside_atomic_block(self):
        from django.db import connection
        M = get_sowing_model(self)
        svc = get_sowing_service(self)
        u, f = owner_farm("rec-nohttp1@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=u, updated_by=u)
        with patch("portal.crop_sowing._schedule_on_commit"):
            row = svc.enqueue_sowing_reconciliation(f, ev, u)
        self.assertIsNotNone(row)
        seen = {}

        def _check():
            seen["in_atomic"] = connection.in_atomic_block
            seen["savepoints"] = len(connection.savepoint_ids)

        outer_savepoints = len(connection.savepoint_ids)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch",
                                on_call=_check)
        done = svc.execute_sowing_comparison(row.pk, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertIn("in_atomic", seen)
        # No worker-held transaction may wrap the HTTP call: the savepoint
        # depth during the call must equal the outer test depth.
        self.assertEqual(seen["savepoints"], outer_savepoints)
        self.assertEqual(done.status, "success")
