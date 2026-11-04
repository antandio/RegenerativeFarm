"""Task13 focused backend regression: advice job boundaries.

English comments per project convention; user-visible strings stay HU/EN.
Uses the real schema (Farm/Membership/AdviceRun/ConsentEvent) with a normal
TestCase and an isolated mocked executor ONLY: background threads never run
here (portal.advice_jobs._executor.submit is patched wherever scheduling is
involved, and execute_reserved_run is driven with an explicit fake client).

Characterization note: these tests were written against the current,
already-reviewed implementation and are GREEN immediately; they lock the
fixed behavior in place rather than inventing new RED failures.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from portal.advice_jobs import _fail_safe, _slot, schedule_advice_run
from portal.models import AdviceRun, ConsentEvent, Farm, Membership
from portal.services import (
    _fail,
    _reserve_advice_run,
    execute_reserved_run,
    is_pending_overdue,
)

User = get_user_model()


def make_owner(email="jobs-reg@pelda.hu"):
    """Create an owner with a farm and granted cloud consent."""
    user = User.objects.create_user(email=email, password="TesztJelszo123!")
    farm = Farm.objects.create(owner=user, name="Jobs farm")
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return user, farm


def make_manager(email, farm, role="manager", active=True):
    """Create a member user with a (possibly inactive) membership."""
    user = User.objects.create_user(email=email, password="TesztJelszo123!")
    Membership.objects.create(farm=farm, user=user, role=role, is_active=active)
    return user


class FakeClient:
    """Minimal provider double returning valid parsed advice."""

    def __init__(self):
        self.calls = 0

    def generate_advice(self, profile, language=None):
        self.calls += 1
        steps = [
            {
                "title": f"Lepes {i}",
                "action_text": f"Teendo {i}",
                "rationale": f"Indoklas {i}",
                "assumptions": [f"Felteteles {i}"],
                "risk_note": f"Kockazat {i}",
            }
            for i in (1, 2, 3)
        ]
        return {
            "steps": steps,
            "assumptions": ["Kozos feltetel"],
            "focused_questions": ["Milyen a talaj?"],
            "is_general": False,
            "_actual_model": "test-model",
            "_usage_input_tokens": 10,
            "_usage_output_tokens": 20,
        }


class DuplicateExecuteTests(TestCase):
    """Re-executing a reserved run never double-calls the provider."""

    def test_duplicate_execute_reserved_run_provider_called_once(self):
        user, farm = make_owner()
        run, created = _reserve_advice_run(user, farm, "jobs-dup-1")
        self.assertTrue(created)
        client = FakeClient()
        first = execute_reserved_run(run, client=client)
        self.assertEqual(first.status, "success")
        self.assertEqual(client.calls, 1)
        self.assertEqual(first.steps.count(), 3)
        second = execute_reserved_run(run, client=client)
        self.assertEqual(second.status, "success")
        self.assertEqual(client.calls, 1)
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).steps.count(), 3)


class ConsentRevokedAfterReserveTests(TestCase):
    """Revocation between reserve and dispatch blocks the provider call."""

    def test_consent_revoked_after_reserve_no_provider_call(self):
        user, farm = make_owner(email="jobs-rev@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-rev-1")
        ConsentEvent.objects.create(farm=farm, actor=user, event_type="revoked")
        client = FakeClient()
        done = execute_reserved_run(run, client=client)
        self.assertEqual(done.status, "failed")
        self.assertEqual(done.error_code, "consent_withdrawn_before_call")
        self.assertEqual(client.calls, 0)
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).steps.count(), 0)


class SaturationTests(TestCase):
    """INTENTIONAL CONTRACT CHANGE (Task1 GREEN): no worker slot exists.

    Reservation never fails a run on saturation; the DB queue holds it
    pending until the dedicated worker claims it.
    """

    def test_held_slot_leaves_reservation_pending(self):
        user, farm = make_owner(email="jobs-sat@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-sat-1")
        self.assertTrue(_slot.acquire(blocking=False))
        try:
            run2, created2 = _reserve_advice_run(user, farm, "jobs-sat-2")
        finally:
            _slot.release()
        self.assertFalse(created2)
        self.assertEqual(run2.pk, run.pk)
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).status, "pending")


class ConstructorFailureTests(TestCase):
    """INTENTIONAL CONTRACT CHANGE (Task1 GREEN): no executor submit.

    The request path never submits to a thread executor; terminal safety
    for worker crashes lives in advice_worker via _fail_safe.
    """

    def test_fail_safe_marks_run_terminal_without_leak(self):
        user, farm = make_owner(email="jobs-ctor@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-ctor-1")
        _fail_safe(run.pk)
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "failed")
        self.assertIsNotNone(done.completed_at)


class UnknownFailureCodeTests(TestCase):
    """Unknown codes fall back to a safe message, never leaking internals."""

    def test_unknown_failure_code_safe_fallback(self):
        user, farm = make_owner(email="jobs-unk@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-unk-1")
        _fail(run, "no_such_code_xyz")
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "failed")
        self.assertTrue(done.error_message_safe)
        self.assertNotIn("no_such_code_xyz", done.error_message_safe)

    def test_worker_unknown_failure_code_safe_fallback(self):
        user, farm = make_owner(email="jobs-unk2@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-unk2-1")
        _fail_safe(run.pk, code="no_such_code_xyz")
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "failed")
        self.assertTrue(done.error_message_safe)
        self.assertNotIn("no_such_code_xyz", done.error_message_safe)


class StatusCrossFarmTests(TestCase):
    """Another farm's status URL is 404 with no provider and no writes."""

    def test_status_crossfarm_404_no_provider_no_writes(self):
        user_a, farm_a = make_owner(email="jobs-xa@pelda.hu")
        user_b, _ = make_owner(email="jobs-xb@pelda.hu")
        run, _ = _reserve_advice_run(user_a, farm_a, "jobs-xa-1")
        runs_before = AdviceRun.objects.count()
        started_at = AdviceRun.objects.get(pk=run.pk).attempt_started_at
        self.client.force_login(user_b)
        with patch("portal.services.MetaProviderClient") as mock_provider:
            response = self.client.get(f"/tanacsok/{run.id}/allapot/")
            self.assertEqual(response.status_code, 404)
            mock_provider.assert_not_called()
        self.assertEqual(AdviceRun.objects.count(), runs_before)
        fresh = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.steps.count(), 0)
        self.assertEqual(fresh.attempt_started_at, started_at)


class RemovedMembershipTests(TestCase):
    """Deactivated membership denies reservation and dispatch."""

    def test_removed_membership_post_denies_without_run(self):
        owner, farm = make_owner(email="jobs-mm@pelda.hu")
        member = make_manager("jobs-member@pelda.hu", farm)
        Membership.objects.filter(farm=farm, user=member).update(is_active=False)
        self.client.force_login(member)
        before = AdviceRun.objects.count()
        response = self.client.post(
            "/tanacs/keres/",
            {"idempotency_key": "jobs-mm-1"},
            HTTP_ACCEPT="application/json",
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(AdviceRun.objects.count(), before)

    def test_removed_membership_execute_denies_without_provider(self):
        owner, farm = make_owner(email="jobs-mm2@pelda.hu")
        member = make_manager("jobs-member2@pelda.hu", farm)
        run, _ = _reserve_advice_run(member, farm, "jobs-mm2-1")
        Membership.objects.filter(farm=farm, user=member).update(is_active=False)
        client = FakeClient()
        done = execute_reserved_run(run, client=client)
        self.assertEqual(done.status, "failed")
        self.assertEqual(done.error_code, "invalid_input")
        self.assertEqual(client.calls, 0)


class PendingStaleBoundaryTests(TestCase):
    """Overdue derivation honors the safe deadline, never the raw setting."""

    @override_settings(ADVICE_PENDING_STALE_MINUTES=15, ADVICE_REQUEST_TIMEOUT_SECONDS=30)
    def test_pending_stale_boundary(self):
        """INTENTIONAL CONTRACT CHANGE (Task1 GREEN): only AUTHORIZED
        runs can be overdue; unclaimed queue never is."""
        from portal.services import authorize_dispatch, pending_deadline

        user, farm = make_owner(email="jobs-st@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-st-1")
        deadline = pending_deadline()
        self.client.force_login(user)
        # An old reservation without authorization is never overdue.
        AdviceRun.objects.filter(pk=run.pk).update(
            attempt_started_at=timezone.now() - deadline - timedelta(seconds=60)
        )
        run.refresh_from_db()
        self.assertFalse(is_pending_overdue(run))
        response = self.client.get(f"/tanacsok/{run.id}/allapot/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["phase"], "queued")
        # Authorized but fresh: still not overdue.
        self.assertTrue(authorize_dispatch(run))
        run.refresh_from_db()
        self.assertFalse(is_pending_overdue(run))
        # Authorized and past the dispatch deadline: derived failed label,
        # DB row untouched.
        AdviceRun.objects.filter(pk=run.pk).update(
            dispatch_authorized_at=timezone.now() - deadline - timedelta(seconds=60)
        )
        run.refresh_from_db()
        self.assertTrue(is_pending_overdue(run))
        response = self.client.get(f"/tanacsok/{run.id}/allapot/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["phase"], "error")  # overdue fallback: phase=error agrees with failed
        self.assertIn("html", payload)
        db_run = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(db_run.status, "pending")

    @override_settings(ADVICE_PENDING_STALE_MINUTES=1, ADVICE_REQUEST_TIMEOUT_SECONDS=300)
    def test_timeout300_extends_deadline(self):
        from portal.services import pending_deadline

        self.assertGreaterEqual(pending_deadline(), timedelta(seconds=360))
        user, farm = make_owner(email="jobs-st2@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-st2-1")
        # Older than the raw 1-minute setting but younger than timeout + 60s.
        AdviceRun.objects.filter(pk=run.pk).update(
            attempt_started_at=timezone.now() - timedelta(seconds=120)
        )
        run.refresh_from_db()
        self.assertFalse(is_pending_overdue(run))


@override_settings(
    ADVICE_MAX_PER_HOUR_PER_FARM=1,
    ADVICE_MAX_PER_DAY_PER_FARM=10,
    ADVICE_GLOBAL_MAX_PER_DAY=1000,
)
class QuotaIdempotencyTests(TestCase):
    """Quota rejects, idempotency replays, scheduling defers to on_commit."""

    def test_quota_rejects_new_reservation_after_terminal(self):
        """INTENTIONAL CONTRACT CHANGE (Task1 GREEN): a distinct key while
        a run is pending dedupes to it, so quota rejects only apply once
        no pending run exists."""
        user, farm = make_owner(email="jobs-q@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "jobs-q-1")
        execute_reserved_run(run, client=FakeClient())
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).status, "success")
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "jobs-q-2"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 429)
            mock_provider.assert_not_called()
        self.assertFalse(
            AdviceRun.objects.filter(farm=farm, idempotency_key="jobs-q-2").exists()
        )

    def test_idempotency_replay_no_schedule_reserve_only(self):
        """INTENTIONAL CONTRACT CHANGE (Task1 GREEN): reserve-only POST."""
        user, farm = make_owner(email="jobs-id@pelda.hu")
        self.client.force_login(user)
        with patch("portal.views.transaction.on_commit") as mock_commit, \
                patch("portal.services.MetaProviderClient") as mock_provider:
            first = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "jobs-id-1"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(first.status_code, 202)
            mock_commit.assert_not_called()
            second = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "jobs-id-1"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(second.status_code, 202)
            mock_commit.assert_not_called()
            mock_provider.assert_not_called()
            self.assertEqual(
                AdviceRun.objects.filter(
                    farm=farm, idempotency_key="jobs-id-1"
                ).count(),
                1,
            )

    def test_terminal_replay_returns_html_without_provider(self):
        from portal.services import request_advice

        user, farm = make_owner(email="jobs-tr@pelda.hu")
        run = request_advice(user, farm, "jobs-tr-1", client=FakeClient())
        self.assertEqual(run.status, "success")
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider, \
                patch("portal.views.schedule_advice_run", create=True) as mock_schedule:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "jobs-tr-1"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["status"], "success")
            for pos in (1, 2, 3):
                self.assertIn(f"Lepes {pos}", payload["html"])
            mock_provider.assert_not_called()
            mock_schedule.assert_not_called()
