"""Task1 durable worker slice RED: reserve-only web POST + DB-backed worker.

English comments per project convention; user-visible strings stay HU/EN.
Strict vertical TDD: every behavior below is asserted here BEFORE the app
edit. Provider contact happens only through httpx.MockTransport (no paid
app calls, no network).
"""
import importlib.util
import json
from datetime import timedelta
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from portal.models import AdviceRun, ConsentEvent, Farm

try:
    from portal import advice_worker as _worker_module
except ImportError:
    _worker_module = None

User = get_user_model()


def make_owner(email="slice-owner@pelda.hu"):
    """Create an owner with a farm and granted cloud consent."""
    user = User.objects.create_user(email=email, password="TesztJelszo123!")
    farm = Farm.objects.create(owner=user, name="Slice farm")
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return user, farm


def valid_parsed():
    """Provider-shaped payload accepted by validate_parsed."""
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
    }


def mock_transport_client():
    """Real MetaProviderClient over httpx.MockTransport (no network)."""
    from portal.provider import MetaProviderClient

    body = {
        "model": "mock-model",
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        "choices": [{"message": {"content": json.dumps(valid_parsed())}}],
    }

    def handler(request):
        return httpx.Response(200, json=body)

    return MetaProviderClient(
        api_key="test-key", transport=httpx.MockTransport(handler)
    )


class ReserveOnlyWebPostTests(TestCase):
    """Web POST reserves only: no provider, no executor, no on_commit."""

    def test_json_post_reserve_only_no_executor(self):
        from portal import views

        user, farm = make_owner(email="slice-web@pelda.hu")
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider, patch(
            "portal.views.transaction.on_commit"
        ) as mock_on_commit:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "slice-web-1"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload.get("status"), "pending")
            self.assertIn("status_url", payload)
            mock_provider.assert_not_called()
            # No executor scheduling from the request path at all.
            self.assertFalse(hasattr(views, "schedule_advice_run"))
            mock_on_commit.assert_not_called()
        run = AdviceRun.objects.get(farm=farm, idempotency_key="slice-web-1")
        self.assertEqual(run.status, "pending")
        self.assertIsNone(run.dispatch_authorized_at)

    def test_html_post_reserve_only_redirect(self):
        user, farm = make_owner(email="slice-html@pelda.hu")
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider, patch(
            "portal.views.transaction.on_commit"
        ) as mock_on_commit:
            response = self.client.post(
                "/tanacs/keres/", {"idempotency_key": "slice-html-1"}
            )
            self.assertEqual(response.status_code, 302)
            mock_provider.assert_not_called()
            mock_on_commit.assert_not_called()
        run = AdviceRun.objects.get(farm=farm, idempotency_key="slice-html-1")
        self.assertTrue(response["Location"].endswith(f"/tanacsok/{run.id}/"))


class WorkerModulePresenceTests(TestCase):
    """The durable worker module must exist (RED gate, assertion not error)."""

    def test_worker_module_present(self):
        self.assertIsNotNone(
            importlib.util.find_spec("portal.advice_worker"),
            "portal.advice_worker missing",
        )
        self.assertIsNotNone(_worker_module, "portal.advice_worker missing")


class WorkerClaimEndToEndTests(TestCase):
    """Worker claims an unclaimed pending run through the real command."""

    def setUp(self):
        # Per-test private lock: the deployed production worker holds
        # BASE_DIR/advice-worker.lock, so the default path would collide.
        import tempfile
        from pathlib import Path

        self._lock_tmp = tempfile.TemporaryDirectory(prefix="advice-lock-")
        self.addCleanup(self._lock_tmp.cleanup)
        lock_file = str(Path(self._lock_tmp.name) / "advice-worker.lock")
        self._lock_override = override_settings(
            ADVICE_WORKER_LOCK_FILE=lock_file
        )
        self._lock_override.enable()
        self.addCleanup(self._lock_override.disable)

    def test_worker_once_claims_unclaimed_run_end_to_end(self):
        self.assertIsNotNone(_worker_module, "portal.advice_worker missing")
        from django.core.management import call_command

        from portal.services import _reserve_advice_run

        user, farm = make_owner(email="slice-e2e@pelda.hu")
        run, created = _reserve_advice_run(user, farm, "slice-e2e-1")
        self.assertTrue(created)
        client = mock_transport_client()
        with patch.object(
            _worker_module, "build_worker_client", return_value=client
        ):
            call_command("run_advice_worker", "--once")
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "success")
        self.assertEqual(done.steps.count(), 3)
        self.assertIsNotNone(done.dispatch_authorized_at)

    def test_worker_reaps_stale_authorized_orphan_without_provider(self):
        self.assertIsNotNone(_worker_module, "portal.advice_worker missing")
        from portal.services import (
            _reserve_advice_run,
            authorize_dispatch,
            pending_deadline,
        )

        user, farm = make_owner(email="slice-orphan@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "slice-orphan-1")
        self.assertTrue(authorize_dispatch(run))
        # Overdue is measured from the dispatch claim timestamp.
        AdviceRun.objects.filter(pk=run.pk).update(
            dispatch_authorized_at=timezone.now()
            - pending_deadline()
            - timedelta(seconds=60)
        )

        calls = []

        class CountingClient:
            def generate_advice(self, profile, language=None):
                calls.append(1)
                return valid_parsed()

        _worker_module.run_worker_once(client=CountingClient())
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "failed")
        self.assertEqual(calls, [])
        self.assertEqual(done.steps.count(), 0)


class PendingDedupeNoQuotaChargeTests(TestCase):
    """Same-key pending replay returns the run even when the quota is full."""

    @override_settings(
        ADVICE_MAX_PER_HOUR_PER_FARM=1,
        ADVICE_MAX_PER_DAY_PER_FARM=10,
        ADVICE_GLOBAL_MAX_PER_DAY=1000,
    )
    def test_same_key_pending_replay_no_quota_charge(self):
        from portal.services import _reserve_advice_run

        user, farm = make_owner(email="slice-dedupe@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "slice-dedupe-1")
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "slice-dedupe-1"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload.get("status"), "pending")
            self.assertEqual(
                AdviceRun.objects.filter(
                    farm=farm, idempotency_key="slice-dedupe-1"
                ).count(),
                1,
            )
            mock_provider.assert_not_called()
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).status, "pending")


class StaleUnclaimedRestartTests(TestCase):
    """Stale UNCLAIMED run must survive restart and process afterwards."""

    def setUp(self):
        # Per-test private lock: the deployed production worker holds
        # BASE_DIR/advice-worker.lock, so the default path would collide.
        import tempfile
        from pathlib import Path

        self._lock_tmp = tempfile.TemporaryDirectory(prefix="advice-lock-")
        self.addCleanup(self._lock_tmp.cleanup)
        lock_file = str(Path(self._lock_tmp.name) / "advice-worker.lock")
        self._lock_override = override_settings(
            ADVICE_WORKER_LOCK_FILE=lock_file
        )
        self._lock_override.enable()
        self.addCleanup(self._lock_override.disable)

    def test_stale_unclaimed_survives_restart_then_processes(self):
        self.assertIsNotNone(_worker_module, "portal.advice_worker missing")
        from datetime import timedelta as _td

        from django.core.management import call_command

        from portal.services import _reserve_advice_run, pending_deadline

        user, farm = make_owner(email="slice-restart@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "slice-restart-1")
        # Age the reservation past the deadline WITHOUT authorization.
        AdviceRun.objects.filter(pk=run.pk).update(
            attempt_started_at=timezone.now()
            - pending_deadline()
            - _td(seconds=60)
        )
        client = mock_transport_client()
        # Simulated restart: fresh command invocation must NOT fail it.
        with patch.object(
            _worker_module, "build_worker_client", return_value=client
        ):
            call_command("run_advice_worker", "--once")
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "success")
        self.assertEqual(done.steps.count(), 3)
        self.assertIsNotNone(done.dispatch_authorized_at)


class DistinctKeyPendingDedupeTests(TestCase):
    """Distinct key while a run is pending returns it; quota charged once."""

    @override_settings(
        ADVICE_MAX_PER_HOUR_PER_FARM=1,
        ADVICE_MAX_PER_DAY_PER_FARM=10,
        ADVICE_GLOBAL_MAX_PER_DAY=1000,
    )
    def test_distinct_key_while_pending_returns_existing_run(self):
        from portal.services import _reserve_advice_run

        user, farm = make_owner(email="slice-dedup2@pelda.hu")
        run, created = _reserve_advice_run(user, farm, "slice-dedup2-1")
        self.assertTrue(created)
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "slice-dedup2-2"},
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload.get("status"), "pending")
            self.assertEqual(payload.get("phase"), "queued")
            self.assertIn(str(run.id), payload.get("status_url", ""))
            self.assertEqual(AdviceRun.objects.filter(farm=farm).count(), 1)
            mock_provider.assert_not_called()


class DerivedPhaseContractTests(TestCase):
    """Status JSON keeps pending/success/failed and adds a derived phase."""

    def test_status_json_carries_queued_phase(self):
        from portal.services import _reserve_advice_run

        user, farm = make_owner(email="slice-phase@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "slice-phase-1")
        self.client.force_login(user)
        response = self.client.get(f"/tanacsok/{run.id}/allapot/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "pending")
        self.assertEqual(payload.get("phase"), "queued")

    def test_status_json_carries_running_phase(self):
        from portal.services import _reserve_advice_run, authorize_dispatch

        user, farm = make_owner(email="slice-running@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "slice-running-1")
        self.assertTrue(authorize_dispatch(run))
        self.client.force_login(user)
        response = self.client.get(f"/tanacsok/{run.id}/allapot/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "pending")
        self.assertEqual(payload.get("phase"), "running")
