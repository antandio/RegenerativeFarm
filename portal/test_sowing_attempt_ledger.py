"""Task12 review-fix RED: immutable attempt ledger + guards.

English comments/docstrings; HU/EN UI preserved. Provider mocked only.
These tests encode the new contract and must FAIL before the fix.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from portal.models import (
    ConsentEvent, CropCycle, Farm, FarmingEvent,
    FarmLocation, FarmProperty, Membership,
)
from portal.test_sowing_reconcile import FakeSowingClient

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


def make_member(farm, email, role):
    u = make_user(email)
    Membership.objects.create(farm=farm, user=u, role=role, is_active=True)
    return u


def seed_candidate(farm, prop, name="Buza", days_offset=5):
    return CropCycle.objects.create(
        farm=farm, property=prop, crop_name=name, state="planned",
        sow_date=date.today() + timedelta(days=days_offset))


def get_attempt_model(tc):
    try:
        m = apps.get_model("portal", "SowingComparisonAttempt")
    except LookupError:
        tc.fail("SowingComparisonAttempt model missing (RED)")
    tc.assertIsNotNone(m, "SowingComparisonAttempt missing (RED)")
    return m


class AttemptLedgerQuotaTests(TestCase):
    def _seed(self, email):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop)
        return u, f, prop, cand

    def _enqueued(self, svc, M, f, prop, cand, actor, crop="Buza"):
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot=crop,
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=actor, updated_by=actor)
        row = svc.enqueue_sowing_reconciliation(f, ev, actor)
        self.assertIsNotNone(row)
        return ev, M.objects.get(pk=row.pk)

    @override_settings(ADVICE_MAX_PER_HOUR_PER_FARM=10, ADVICE_MAX_PER_DAY_PER_FARM=50,
                       ADVICE_GLOBAL_MAX_PER_DAY=500)
    def test_failed_then_retry_counts_two_attempts_preserves_original(self):
        A = get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        from portal.provider import ProviderError
        u, f, prop, cand = self._seed("ledger-quota1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        failed = svc.execute_sowing_comparison(row, client=FakeSowingClient(exc=ProviderError("x")))
        self.assertEqual(failed.status, "failed")
        first = A.objects.filter(comparison_id=row.pk).order_by("dispatched_at").first()
        self.assertIsNotNone(first, "first dispatched attempt must exist")
        ts1, consent1 = first.dispatched_at, first.dispatch_consent_event_id
        self.assertIsNotNone(ts1)
        self.assertIsNotNone(consent1)
        with patch("portal.sowing_jobs.schedule_sowing_comparison"):
            retried, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
        self.assertTrue(scheduled, "failed row retry must schedule")
        done = svc.execute_sowing_comparison(retried.pk, client=FakeSowingClient(
            decision="mismatch", reason="crop_mismatch"))
        self.assertEqual(done.status, "success")
        attempts = list(A.objects.filter(comparison_id=row.pk).order_by("dispatched_at"))
        self.assertEqual(len(attempts), 2, "retry must create a fresh attempt, never reuse")
        attempts[0].refresh_from_db()
        self.assertEqual(attempts[0].dispatched_at, ts1)
        self.assertEqual(attempts[0].dispatch_consent_event_id, consent1)
        self.assertEqual(attempts[0].status, "failed")
        self.assertIsNotNone(attempts[0].error_code)
        # Quota counts both dispatched attempts.
        from portal.quota import usage_counts
        h, d = usage_counts(f)
        self.assertGreaterEqual(h, 2, "quota must count both attempts, got %s" % h)
        self.assertGreaterEqual(d, 2)

    @override_settings(ADVICE_MAX_PER_HOUR_PER_FARM=2, ADVICE_MAX_PER_DAY_PER_FARM=10,
                       ADVICE_GLOBAL_MAX_PER_DAY=100)
    def test_two_attempts_then_third_blocked(self):
        get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        from portal.provider import ProviderError
        u, f, prop, cand = self._seed("ledger-quota2@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        svc.execute_sowing_comparison(row, client=FakeSowingClient(exc=ProviderError("x")))
        with patch("portal.sowing_jobs.schedule_sowing_comparison"):
            retried, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
        self.assertTrue(scheduled)
        svc.execute_sowing_comparison(retried.pk, client=FakeSowingClient(
            decision="mismatch", reason="crop_mismatch"))
        # Third dispatch on a new pair must be quota-blocked (limit 2).
        cand2 = seed_candidate(f, prop, name="Arpa", days_offset=6)
        ev2 = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand2, crop_name_snapshot="Arpa",
            event_date=date.today(), event_type="sowing", description="V2",
            created_by=u, updated_by=u)
        row2 = svc.enqueue_sowing_reconciliation(f, ev2, u)
        self.assertIsNotNone(row2)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done2 = svc.execute_sowing_comparison(row2.pk, client=fake)
        self.assertEqual(fake.calls, 0, "third dispatch must be quota-blocked")
        self.assertEqual(done2.error_code, "quota_exceeded")

    def test_90day_enqueue_immediate_retry_noop(self):
        get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f, prop, cand = self._seed("ledger-stale1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        # Simulate a row enqueued 90 days ago but dispatched now.
        M.objects.filter(pk=row.pk).update(
            attempt_started_at=timezone.now() - timedelta(days=90))
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(row.pk, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(done.status, "success")
        # Success mismatch is terminal, so use a fresh pending-dispatched case:
        # failed attempt keeps row retryable; here verify live attempt no-op below.
        # For the stale-clock check use an in-flight pending row instead.
        cand2 = seed_candidate(f, prop, name="Zab", days_offset=7)
        ev2, row2 = self._enqueued(svc, M, f, prop, cand2, u)
        M.objects.filter(pk=row2.pk).update(
            attempt_started_at=timezone.now() - timedelta(days=90))
        # Dispatch row2 but keep it in-flight (simulate crash before finalize
        # by authorizing only). Then immediate retry must be a no-op.
        self.assertTrue(svc.authorize_sowing_dispatch(row2.pk))
        row2.refresh_from_db()
        with patch("portal.sowing_jobs.schedule_sowing_comparison") as sched:
            back, scheduled = svc.retry_sowing_comparison(u, f, row2.pk)
        self.assertFalse(scheduled, "live attempt must make retry a no-op")
        sched.assert_not_called()
        self.assertEqual(back.pk, row2.pk)

    def test_superseded_late_completion_cannot_mutate(self):
        A = get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f, prop, cand = self._seed("ledger-late1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        self.assertTrue(svc.authorize_sowing_dispatch(row.pk))
        first = A.objects.filter(comparison_id=row.pk).order_by("dispatched_at").first()
        token1 = first.attempt_token
        # Age the first attempt beyond the safe deadline, then retry.
        A.objects.filter(pk=first.pk).update(
            dispatched_at=timezone.now() - timedelta(minutes=120))
        M.objects.filter(pk=row.pk).update(
            dispatch_authorized_at=timezone.now() - timedelta(minutes=120))
        with patch("portal.sowing_jobs.schedule_sowing_comparison"):
            back, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
        self.assertTrue(scheduled, "stale attempt must allow a fresh retry")
        svc.execute_sowing_comparison(back.pk, client=FakeSowingClient(
            decision="mismatch", reason="crop_mismatch"))
        row.refresh_from_db()
        self.assertEqual(row.review_state, "confirmed_mismatch")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden)
        # Late completion of the superseded first attempt must not mutate.
        parsed = {"decision": "match", "planned_cycle_id": cand.pk,
                  "reason_code": "crop_date_match",
                  "_actual_model": "late-model",
                  "_usage_input_tokens": 1, "_usage_output_tokens": 1}
        out = svc._finalize_decision(row.pk, "match", "crop_date_match", parsed,
                                     attempt_token=token1)
        self.assertIsNotNone(out)
        row.refresh_from_db()
        self.assertEqual(row.review_state, "confirmed_mismatch")
        self.assertEqual(row.decision, "mismatch")
        cand.refresh_from_db()
        self.assertFalse(cand.is_hidden, "late superseded match must not hide")

    def test_uncertain_retry_schedules_fresh_attempt(self):
        A = get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f, prop, cand = self._seed("ledger-unc1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        done = svc.execute_sowing_comparison(row, client=FakeSowingClient(
            decision="uncertain", reason="ambiguous"))
        self.assertEqual(done.decision, "uncertain")
        self.assertEqual(done.review_state, "pending_review")
        # Retry schedules via transaction.on_commit, which TestCase never
        # fires; run callbacks inline so the scheduled-once check is real.
        with patch("portal.sowing_jobs.schedule_sowing_comparison") as sched, \
                patch("django.db.transaction.on_commit",
                      side_effect=lambda cb: cb()):
            back, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
        self.assertTrue(scheduled, "uncertain must be retryable")
        sched.assert_called_once()
        self.assertEqual(A.objects.filter(comparison_id=row.pk).count(), 1)
        done2 = svc.execute_sowing_comparison(back.pk, client=FakeSowingClient(
            decision="mismatch", reason="crop_mismatch"))
        self.assertEqual(done2.decision, "mismatch")
        self.assertEqual(A.objects.filter(comparison_id=row.pk).count(), 2)

    def test_repeated_retry_idempotence_single_attempt(self):
        A = get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        from portal.provider import ProviderError
        u, f, prop, cand = self._seed("ledger-idem1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        svc.execute_sowing_comparison(row, client=FakeSowingClient(exc=ProviderError("x")))
        with patch("portal.sowing_jobs.schedule_sowing_comparison"):
            svc.retry_sowing_comparison(u, f, row.pk)
            svc.execute_sowing_comparison(row.pk, client=FakeSowingClient(
                decision="uncertain", reason="ambiguous"))
        # Double-submit retry for the uncertain row: only one new dispatch.
        with patch("portal.sowing_jobs.schedule_sowing_comparison"):
            svc.retry_sowing_comparison(u, f, row.pk)
            back2, scheduled2 = svc.retry_sowing_comparison(u, f, row.pk)
        # Second retry while the fresh attempt is already scheduled live
        # must be a no-op OR at least not create a second in-flight attempt.
        # Execute once: exactly one additional provider call total.
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        svc.execute_sowing_comparison(back2.pk, client=fake)
        svc.execute_sowing_comparison(back2.pk, client=fake)
        self.assertEqual(fake.calls, 1)
        self.assertLessEqual(A.objects.filter(comparison_id=row.pk).count(), 3)

    def test_owner_retry_of_worker_row_adopts_actor(self):
        A = get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f = owner_farm("ledger-actor1@example.com")
        grant_consent(f, u)
        worker = make_member(f, "ledger-actor1w@example.com", "worker")
        prop = make_property(f)
        cand = seed_candidate(f, prop)
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=worker, updated_by=worker)
        row = svc.enqueue_sowing_reconciliation(f, ev, worker)
        self.assertEqual(M.objects.get(pk=row.pk).initiated_by_id, worker.pk)
        with patch("portal.sowing_jobs.schedule_sowing_comparison"):
            back, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
        self.assertTrue(scheduled)
        back.refresh_from_db()
        self.assertEqual(back.initiated_by_id, u.pk)
        done = svc.execute_sowing_comparison(back.pk, client=FakeSowingClient(
            decision="mismatch", reason="crop_mismatch"))
        self.assertEqual(done.status, "success")
        att = A.objects.filter(comparison_id=row.pk).order_by("-dispatched_at").first()
        self.assertEqual(att.initiated_by_id, u.pk)

    def test_provider_payload_uses_authorized_snapshot(self):
        get_attempt_model(self)
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f, prop, cand = self._seed("ledger-snap1@example.com")
        ev, row = self._enqueued(svc, M, f, prop, cand, u)
        row.refresh_from_db()
        orig_crop, orig_date = row.crop_snapshot, row.event_date.isoformat()
        orig_plan_crop = row.planned_crop_snapshot
        orig_plan_date = row.planned_sow_date.isoformat()
        self.assertTrue(svc.authorize_sowing_dispatch(row.pk))
        # Mutate mutable rows after authorization, before execution.
        FarmingEvent.objects.filter(pk=ev.pk).update(crop_name_snapshot="TAMPERED")
        CropCycle.objects.filter(pk=cand.pk).update(crop_name="TAMPERED2")
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        # Execute a fresh authorized view: payload must be snapshot or no call.
        # Re-enqueue path is not used here; execute on a second comparison
        # would re-authorize, so call execute on the same row via a new
        # comparison is not possible. Instead verify execute never sends
        # tampered values: it must abort (supersede) or send snapshots.
        out = svc.execute_sowing_comparison(row.pk, client=fake)
        if fake.calls:
            self.assertEqual(fake.last["actual_crop"], orig_crop)
            self.assertEqual(fake.last["actual_date"], orig_date)
            self.assertEqual(fake.last["planned_crop"], orig_plan_crop)
            self.assertEqual(fake.last["planned_sow_date"], orig_plan_date)
            self.assertNotIn("TAMPERED", str(fake.last))
        else:
            out.refresh_from_db()
            self.assertIn(out.status, ("superseded", "failed", "pending"))


class PerEventConsumeTests(TestCase):
    def _seed2(self, email):
        u, f = owner_farm(email)
        grant_consent(f, u)
        prop = make_property(f)
        near = seed_candidate(f, prop, name="Buza", days_offset=2)
        far = seed_candidate(f, prop, name="Buza", days_offset=9)
        return u, f, prop, near, far

    def test_sequential_two_confirmed_hides_consume_once(self):
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f, prop, near, far = self._seed2("ledger-hide1@example.com")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=near, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=u, updated_by=u)
        first = svc.enqueue_sowing_reconciliation(f, ev, u)
        second = M.objects.create(
            farm=f, initiated_by=u, farming_event=ev, planned_cycle=far,
            property=prop, crop_snapshot="Buza", event_date=ev.event_date,
            planned_crop_snapshot=far.crop_name, planned_sow_date=far.sow_date,
            input_hash=svc.canonical_input_hash(
                f.pk, ev.pk, prop.pk, "Buza", ev.event_date,
                far.pk, far.crop_name, far.sow_date),
            idempotency_key="hide-race-%s" % ev.pk, language="hu",
            status="success", decision="mismatch", reason_code="crop_mismatch",
            review_state="confirmed_mismatch",
            requested_model="m-test", attempt_started_at=timezone.now())
        M.objects.filter(pk=first.pk).update(
            status="success", decision="mismatch", reason_code="crop_mismatch",
            review_state="confirmed_mismatch")
        c = Client()
        c.force_login(u)
        r1 = c.post("/cycles/sowing/%d/hide/" % first.pk, {})
        self.assertEqual(r1.status_code, 302)
        r2 = c.post("/cycles/sowing/%d/hide/" % second.pk, {})
        self.assertEqual(r2.status_code, 302)
        near.refresh_from_db()
        far.refresh_from_db()
        hidden = [x for x in (near, far) if x.is_hidden]
        self.assertEqual(len(hidden), 1, "one actual consumes at most one plan")
        states = set(M.objects.filter(pk__in=[first.pk, second.pk]).values_list(
            "review_state", flat=True))
        self.assertIn("dismissed_deleted", states)
        self.assertTrue(states & {"superseded", "confirmed_mismatch", "dismissed_deleted"})

    def test_manual_hide_then_model_match_consumes_once(self):
        from portal.models import SowingComparison as M
        from portal import crop_sowing as svc
        u, f, prop, near, far = self._seed2("ledger-mix1@example.com")
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=near, crop_name_snapshot="Buza",
            event_date=date.today(), event_type="sowing", description="Vetes",
            created_by=u, updated_by=u)
        first = svc.enqueue_sowing_reconciliation(f, ev, u)
        M.objects.filter(pk=first.pk).update(
            status="success", decision="mismatch", reason_code="crop_mismatch",
            review_state="confirmed_mismatch")
        c = Client()
        c.force_login(u)
        r = c.post("/cycles/sowing/%d/hide/" % first.pk, {})
        self.assertEqual(r.status_code, 302)
        near.refresh_from_db()
        self.assertTrue(near.is_hidden)
        # Model match for the same event on another plan must not hide again.
        row2 = M.objects.create(
            farm=f, initiated_by=u, farming_event=ev, planned_cycle=far,
            property=prop, crop_snapshot="Buza", event_date=ev.event_date,
            planned_crop_snapshot=far.crop_name, planned_sow_date=far.sow_date,
            input_hash=svc.canonical_input_hash(
                f.pk, ev.pk, prop.pk, "Buza", ev.event_date,
                far.pk, far.crop_name, far.sow_date),
            idempotency_key="mix-race-%s" % ev.pk, language="hu",
            status="pending", review_state="pending_review",
            requested_model="m-test", attempt_started_at=timezone.now())
        fake = FakeSowingClient(decision="match", reason="crop_date_match")
        done = svc.execute_sowing_comparison(row2.pk, client=fake)
        far.refresh_from_db()
        self.assertFalse(far.is_hidden, "consumed event must not hide a second plan")
        self.assertNotEqual(done.review_state, "resolved_matched")


class RetainedIndicatorTests(TestCase):
    def test_retain_shows_distinct_indicator_active_farm_only(self):
        from portal.models import SowingComparison as M
        from portal.test_sowing_ui import seed_pending, confirm_mismatch
        u, f = owner_farm("ledger-ret1@example.com")
        prop = make_property(f)
        row, _, _ = seed_pending(f, u, prop, actual_crop="Kukorica", plan_crop="Buza")
        confirm_mismatch(row)
        c = Client()
        c.force_login(u)
        b = c.get("/attekintes/").content.decode("utf-8")
        self.assertIn("nav-badge", b)
        r = c.post("/cycles/sowing/%d/retain/" % row.pk, {})
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.review_state, "dismissed_retained")
        b = c.get("/attekintes/").content.decode("utf-8")
        # NEW badge cleared but a distinct retained indicator remains.
        self.assertNotIn("nav-badge", b)
        self.assertTrue(
            "nav-badge-retained" in b or "retained" in b.lower(),
            "retained discrepancy must keep a distinct navigation indicator")
        # Other farm sees neither indicator.
        u2, f2 = owner_farm("ledger-ret1b@example.com")
        c2 = Client()
        c2.force_login(u2)
        b2 = c2.get("/attekintes/").content.decode("utf-8")
        self.assertNotIn("nav-badge", b2)
        self.assertNotIn("nav-badge-retained", b2)


class MismatchSectionConditionalTests(TestCase):
    def test_pending_only_hides_confirmed_section(self):
        from portal.test_sowing_ui import seed_pending
        u, f = owner_farm("ledger-cond1@example.com")
        prop = make_property(f)
        seed_pending(f, u, prop, actual_crop="Kukorica", plan_crop="Buza")
        c = Client()
        c.force_login(u)
        b = c.get("/cycles/").content.decode("utf-8")
        self.assertIn("Ellen\u0151rz\u00e9s alatt", b)
        for bad in ("Meger\u0151s\u00edtett elt\u00e9r\u00e9s",
                    "Confirmed mismatch", "contradiction"):
            self.assertNotIn(bad, b)
