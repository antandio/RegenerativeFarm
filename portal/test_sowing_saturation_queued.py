"""Saturation guard regression: queued never-dispatched retry stays pending.

English comments/docstrings; HU/EN UI preserved. Provider mocked only.
"""
import threading
from datetime import date
from unittest.mock import patch

from django.test import TransactionTestCase
from django.utils import timezone

from portal.models import (
    FarmingEvent, SowingComparison, SowingComparisonAttempt,
)
from portal.test_sowing_racefix import (
    _drain_slot, _wait_for, BlockingProvider,
)
from portal.test_sowing_reconcile import (
    grant_consent, make_property, owner_farm, seed_candidate,
)


def _make_never_dispatched_row(svc, f, prop, cand, actor):
    """Create a raw pending row with no attempt and no reservation."""
    with patch("portal.crop_sowing._schedule_on_commit"):
        ev = FarmingEvent.objects.create(
            farm=f, property=prop, crop_cycle=cand,
            crop_name_snapshot="Buza", event_date=date.today(),
            event_type="sowing", description="Vetes",
            created_by=actor, updated_by=actor)
        row = svc.enqueue_sowing_reconciliation(f, ev, actor)
    assert row is not None
    # Enqueue leaves a fresh reservation when eligible; retry would treat
    # that as a duplicate no-op, so reset to the raw never-queued state.
    SowingComparison.objects.filter(pk=row.pk).update(queued_at=None)
    row = SowingComparison.objects.get(pk=row.pk)
    assert row.status == "pending"
    assert row.current_attempt_id is None
    assert row.dispatch_authorized_at is None
    assert row.queued_at is None
    assert SowingComparisonAttempt.objects.filter(
        comparison_id=row.pk).count() == 0
    return row


class QueuedRetrySaturationTests(TransactionTestCase):
    """Queued retry under saturation must never false-fail the row."""

    def test_never_dispatched_retry_stays_pending_and_recovers(self):
        from portal import crop_sowing as svc
        from portal import sowing_jobs as jobs
        _drain_slot()
        u, f = owner_farm("sat-queued1@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        row = _make_never_dispatched_row(svc, f, prop, cand, u)
        self.assertTrue(
            jobs._slot.acquire(blocking=False), "slot must be free")
        try:
            retried, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
            self.assertFalse(
                scheduled, "saturated retry must report scheduled=False")
            retried.refresh_from_db()
            self.assertEqual(
                retried.status, "pending",
                "queued retry must stay pending, got %s" % retried.status)
            self.assertEqual(retried.review_state, "pending_review")
            self.assertIsNone(retried.queued_at)
            self.assertIsNone(retried.current_attempt_id)
            self.assertIsNone(retried.dispatch_authorized_at)
            self.assertEqual(
                SowingComparisonAttempt.objects.filter(
                    comparison_id=row.pk).count(), 0)
        finally:
            jobs._slot.release()
        gate = threading.Event()
        gate.set()
        provider = BlockingProvider(gate)
        with patch("portal.provider.MetaProviderClient"
                   ".generate_sowing_comparison", side_effect=provider):
            retried2, scheduled2 = svc.retry_sowing_comparison(u, f, row.pk)
            self.assertTrue(scheduled2, "retry after free slot must queue")
            done = _wait_for(
                lambda: SowingComparison.objects.get(
                    pk=row.pk).status == "success",
                timeout=15.0)
            self.assertTrue(done, "row never finalized after retry")
        _drain_slot()
        self.assertEqual(provider.calls, 1, "exactly one provider call")

    def test_saturation_failed_attemptless_row_recovers(self):
        from portal import crop_sowing as svc
        from portal import sowing_jobs as jobs
        _drain_slot()
        u, f = owner_farm("sat-queued2@example.com")
        grant_consent(f, u)
        prop = make_property(f)
        cand = seed_candidate(f, prop, name="Buza", days_offset=4)
        row = _make_never_dispatched_row(svc, f, prop, cand, u)
        # Simulate the historical saturation false-fail: failed with no
        # attempt, no dispatch, and no reservation.
        SowingComparison.objects.filter(pk=row.pk).update(
            status="failed", review_state="pending_review",
            error_code="provider_error", error_message_safe="test",
            completed_at=timezone.now(), queued_at=None)
        self.assertTrue(
            jobs._slot.acquire(blocking=False), "slot must be free")
        try:
            retried, scheduled = svc.retry_sowing_comparison(u, f, row.pk)
            self.assertFalse(
                scheduled, "saturated retry must report scheduled=False")
            retried.refresh_from_db()
            self.assertEqual(
                retried.status, "pending",
                "recovered row must stay pending, got %s" % retried.status)
            self.assertEqual(retried.review_state, "pending_review")
            self.assertIsNone(retried.queued_at)
            self.assertEqual(
                SowingComparisonAttempt.objects.filter(
                    comparison_id=row.pk).count(), 0)
        finally:
            jobs._slot.release()
        gate = threading.Event()
        gate.set()
        provider = BlockingProvider(gate)
        with patch("portal.provider.MetaProviderClient"
                   ".generate_sowing_comparison", side_effect=provider):
            retried2, scheduled2 = svc.retry_sowing_comparison(u, f, row.pk)
            self.assertTrue(scheduled2, "retry after free slot must queue")
            done = _wait_for(
                lambda: SowingComparison.objects.get(
                    pk=row.pk).status == "success",
                timeout=15.0)
            self.assertTrue(done, "row never finalized after retry")
        _drain_slot()
        self.assertEqual(provider.calls, 1, "exactly one provider call")
