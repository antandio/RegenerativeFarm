"""Task12 racefix RED: atomic retry, saturation safety, no resurrection.

English comments/docstrings; HU/EN UI preserved. Provider mocked only.
Uses real separate DB connections (threads) or deterministic mocked race.
"""
import threading
import time
from datetime import date, timedelta
from unittest.mock import patch

from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from portal.models import (
    CropCycle, Farm, FarmingEvent, SowingComparison,
    SowingComparisonAttempt,
)
from portal.test_sowing_reconcile import (
    FakeSowingClient, grant_consent, make_property, owner_farm,
    seed_candidate,
)
from portal.test_sowing_ui import confirm_mismatch, seed_pending


def _make_failed_row(svc, f, prop, cand, actor, stale=False):
    """Create one failed comparison with a failed attempt (no worker)."""
    with patch("portal.crop_sowing._schedule_on_commit"):
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand,
            crop_name_snapshot="Buza", event_date=date.today(),
            event_type="sowing", description="Vetes",
            created_by=actor, updated_by=actor)
        row = svc.enqueue_sowing_reconciliation(f, ev, actor)
    assert row is not None
    from portal.provider import ProviderError
    failed = svc.execute_sowing_comparison(
        row.pk, client=FakeSowingClient(exc=ProviderError("boom")))
    assert failed.status == "failed"
    if stale:
        from portal.services import pending_deadline
        old = timezone.now() - pending_deadline() - timedelta(seconds=10)
        SowingComparisonAttempt.objects.filter(
            comparison_id=row.pk).update(dispatched_at=old)
        SowingComparison.objects.filter(pk=row.pk).update(
            dispatch_authorized_at=old)
    return SowingComparison.objects.get(pk=row.pk)


def _wait_for(predicate, timeout=15.0, step=0.05):
    """Poll predicate until True or timeout; return last value."""
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class BlockingProvider:
    """Thread-safe blocking provider double counting calls."""

    def __init__(self, event, decision="mismatch", reason="crop_mismatch"):
        self.event = event
        self.decision = decision
        self.reason = reason
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, actual_crop, actual_date, planned_cycle_id,
                 planned_crop, planned_sow_date, delta_days, language=None):
        with self._lock:
            self.calls += 1
        assert self.event.wait(timeout=20.0), "provider block timed out"
        return {
            "decision": self.decision,
            "planned_cycle_id": planned_cycle_id,
            "reason_code": self.reason,
            "_actual_model": "test-race-model",
            "_usage_input_tokens": 5,
            "_usage_output_tokens": 7,
        }


def _drain_slot(timeout=10.0):
    """Wait until the single sowing slot is free (previous workers done)."""
    from portal import sowing_jobs as jobs
    def _free():
        if jobs._slot.acquire(blocking=False):
            jobs._slot.release()
            return True
        return False
    assert _wait_for(_free, timeout=timeout), "sowing slot stayed busy"


class ConcurrentRetryRaceTests(TestCase):
    """Two retries of one failed row: exactly one winner, one call.

    Deterministic synchronized race: the winner queues while dispatch is
    held back, then the loser retries before any claim. This models two
    managers double-clicking the same failed row without relying on
    SQLite thread timing.
    """

    def test_two_concurrent_retries_single_winner_finalized(self):
        from portal import crop_sowing as svc
        u, f = owner_farm("racefix-conc1@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        row = _make_failed_row(svc, f, prop, cand, u, stale=True)
        first_attempts = SowingComparisonAttempt.objects.filter(
            comparison_id=row.pk).count()
        self.assertEqual(first_attempts, 1)
        # Hold dispatch back so the loser runs before any worker claim.
        with patch("portal.sowing_jobs.schedule_sowing_comparison") as sched:
            winner, winner_flag = svc.retry_sowing_comparison(u, f, row.pk)
            loser, loser_flag = svc.retry_sowing_comparison(u, f, row.pk)
            self.assertTrue(winner_flag, "first retry must queue")
            self.assertFalse(
                loser_flag, "second retry must not report scheduled")
            self.assertEqual(
                sched.call_count, 1,
                "exactly one callback expected, got %d" % sched.call_count)
        fake = FakeSowingClient(decision="mismatch", reason="crop_mismatch")
        done = svc.execute_sowing_comparison(winner.pk, client=fake)
        self.assertEqual(done.status, "success")
        attempts = list(SowingComparisonAttempt.objects.filter(
            comparison_id=row.pk).order_by("dispatched_at"))
        self.assertEqual(
            len(attempts), 2,
            "exactly one fresh attempt expected, got %d" % len(attempts))
        self.assertEqual(fake.calls, 1, "exactly one provider call")


class SlotSaturationTests(TransactionTestCase):
    """Row B retry while row A holds the slot stays recoverable."""

    def test_saturation_leaves_b_pending_and_a_finalizes(self):
        from portal import crop_sowing as svc
        _drain_slot()
        u, f = owner_farm("racefix-sat1@example.com")
        grant_consent(f, u)
        prop_a = make_property(f, name="ParcelA")
        prop_b = make_property(f, name="ParcelB")
        cand_a = seed_candidate(f, prop_a, name="BuzaA", days_offset=4)
        cand_b = seed_candidate(f, prop_b, name="BuzaB", days_offset=4)
        row_a = _make_failed_row(svc, f, prop_a, cand_a, u, stale=True)
        row_b = _make_failed_row(svc, f, prop_b, cand_b, u, stale=True)
        gate = threading.Event()
        provider = BlockingProvider(gate)
        with patch("portal.provider.MetaProviderClient"
                   ".generate_sowing_comparison", side_effect=provider):
            retried_a, queued_a = svc.retry_sowing_comparison(u, f, row_a.pk)
            self.assertTrue(queued_a, "row A retry must queue")
            self.assertTrue(
                _wait_for(lambda: provider.calls >= 1, timeout=10.0),
                "row A never dispatched")
            # Row A holds the slot inside the provider call; row B retry
            # must not fail row B and must not claim a dispatch.
            retried_b, queued_b = svc.retry_sowing_comparison(u, f, row_b.pk)
            row_b.refresh_from_db()
            # No false terminal fail on saturation: B stays pending.
            self.assertEqual(
                row_b.status, "pending",
                "saturated row B must stay pending, got %s" % row_b.status)
            self.assertEqual(row_b.review_state, "pending_review")
            # B gained no fresh attempt and spent no provider call yet.
            b_attempts = SowingComparisonAttempt.objects.filter(
                comparison_id=row_b.pk).count()
            self.assertEqual(b_attempts, 1, "saturated B must not dispatch")
            gate.set()
            done_a = _wait_for(
                lambda: SowingComparison.objects.get(
                    pk=row_a.pk).status == "success",
                timeout=15.0)
            self.assertTrue(done_a, "row A never finalized")
        _drain_slot()
        # Explicit retry of B after the slot frees dispatches truthfully.
        gate2 = threading.Event()
        gate2.set()
        provider2 = BlockingProvider(gate2)
        with patch("portal.provider.MetaProviderClient"
                   ".generate_sowing_comparison", side_effect=provider2):
            retried_b2, queued_b2 = svc.retry_sowing_comparison(
                u, f, row_b.pk)
            self.assertTrue(queued_b2, "explicit retry of B must queue")
            done_b = _wait_for(
                lambda: SowingComparison.objects.get(
                    pk=row_b.pk).status == "success",
                timeout=15.0)
            self.assertTrue(done_b, "row B never finalized after retry")
        _drain_slot()
        self.assertEqual(provider.calls, 1, "row A exactly one call")
        self.assertEqual(provider2.calls, 1, "row B exactly one call")


class DismissRaceTests(TestCase):
    """Retry racing retain/hide never resurrects a dismissed row."""

    def test_retry_losing_to_dismiss_stays_dismissed(self):
        from portal import crop_sowing as svc
        u, f = owner_farm("racefix-dismiss1@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        row = _make_failed_row(svc, f, prop, cand, u, stale=True)
        real_hash = svc._retry_hash_matches

        def _hash_then_dismiss(r):
            ok, event = real_hash(r)
            # Deterministic race: dismiss commits after retry read the
            # failed snapshot but before retry writes the reopen.
            SowingComparison.objects.filter(pk=r.pk).update(
                status="success", review_state="dismissed_retained",
                decision="", reason_code="", completed_at=timezone.now())
            return ok, event

        with patch.object(svc, "_retry_hash_matches",
                          side_effect=_hash_then_dismiss):
            with patch("portal.sowing_jobs.schedule_sowing_comparison") as sched:
                back, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
                self.assertFalse(
                    scheduled, "losing retry must not report scheduled")
                sched.assert_not_called()
        back.refresh_from_db()
        self.assertEqual(back.status, "success")
        self.assertEqual(back.review_state, "dismissed_retained")


class HiddenReviewDisplayTests(TestCase):
    """Stale hidden-plan reviews are excluded with field isolation."""

    def test_hidden_plan_review_excluded_from_queues(self):
        u, f = owner_farm("racefix-hidden1@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        visible_row, _, visible_plan = seed_pending(
            f, u, prop, actual_crop="KukoricaVis", plan_crop="BuzaVis")
        hidden_row, _, hidden_plan = seed_pending(
            f, u, prop, actual_crop="KukoricaHid", plan_crop="BuzaHid")
        confirm_mismatch(hidden_row)
        hidden_plan.is_hidden = True
        hidden_plan.hidden_at = timezone.now()
        hidden_plan.hidden_reason = "deleted"
        hidden_plan.save()
        c = Client()
        c.force_login(u)
        resp = c.get("/cycles/")
        self.assertEqual(resp.status_code, 200)
        pending = list(resp.context["pending_reviews"])
        confirmed = list(resp.context["confirmed_reviews"])
        pending_ids = [r.pk for r in pending]
        confirmed_ids = [r.pk for r in confirmed]
        # Field isolation: compare identities, not blanket name strings.
        self.assertIn(visible_row.pk, pending_ids)
        self.assertNotIn(hidden_row.pk, pending_ids)
        self.assertNotIn(hidden_row.pk, confirmed_ids)
        hidden_plan_ids = [r.planned_cycle_id for r in pending + confirmed]
        self.assertNotIn(hidden_plan.pk, hidden_plan_ids)
        self.assertIn(visible_plan.pk,
                      [r.planned_cycle_id for r in pending])
