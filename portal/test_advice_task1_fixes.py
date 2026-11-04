"""Task1 review regressions: two-claimant, reaper, STOP, phase, detail, i18n, client.

English comments per project convention. Strict RED/GREEN: each test
fails before its fix and passes after. No network, no credentials output.
"""
from datetime import timedelta
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from portal.models import AdviceRun, ConsentEvent, Farm

User = get_user_model()


def make_owner(email):
    user = User.objects.create_user(email=email, password="TesztJelszlo123!")
    farm = Farm.objects.create(owner=user, name="Task1 farm")
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return user, farm


def valid_parsed():
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


def mock_client():
    from portal.provider import MetaProviderClient

    import json

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


class TwoClaimantTests(TestCase):
    def test_loser_pending_claim_never_calls_provider(self):
        from portal.services import (
            _reserve_advice_run,
            authorize_dispatch,
            execute_reserved_run,
        )

        user, farm = make_owner("two-claim@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "two-claim-1")
        self.assertTrue(authorize_dispatch(run))
        calls = []

        class CountingClient:
            def generate_advice(self, profile, language=None):
                calls.append(1)
                return valid_parsed()

        loser = execute_reserved_run(run, client=CountingClient())
        self.assertEqual(calls, [])
        self.assertEqual(loser.status, "pending")
        self.assertEqual(loser.steps.count(), 0)
        winner = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(winner.status, "pending")
        self.assertIsNotNone(winner.dispatch_authorized_at)


class FreshLiveNotReapedTests(TestCase):
    def test_fresh_authorized_not_reaped_by_concurrent_reaper(self):
        from portal import advice_worker as worker
        from portal.services import _reserve_advice_run, authorize_dispatch

        user, farm = make_owner("fresh-live@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "fresh-live-1")
        self.assertTrue(authorize_dispatch(run))
        reaped = worker.reap_orphans()
        self.assertEqual(reaped, 0)
        fresh = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(fresh.status, "pending")


class StopResetTests(TestCase):
    def setUp(self):
        # Per-test private lock: the deployed production worker holds
        # BASE_DIR/advice-worker.lock, so the default path would collide.
        import tempfile
        from pathlib import Path

        from django.test import override_settings as _override

        self._lock_tmp = tempfile.TemporaryDirectory(prefix="advice-lock-")
        self.addCleanup(self._lock_tmp.cleanup)
        lock_file = str(Path(self._lock_tmp.name) / "advice-worker.lock")
        self._lock_override = _override(ADVICE_WORKER_LOCK_FILE=lock_file)
        self._lock_override.enable()
        self.addCleanup(self._lock_override.disable)

    def test_loop_command_twice_in_one_process(self):
        import os
        import signal

        from django.core.management import call_command

        from portal import advice_worker as worker
        from portal.management.commands import run_advice_worker as cmd_mod

        # No module-global STOP may exist: stop state is per-invocation.
        self.assertFalse(hasattr(cmd_mod, "STOP"))
        user, farm = make_owner("stop-twice@pelda.hu")
        from portal.services import _reserve_advice_run

        run, _ = _reserve_advice_run(user, farm, "stop-twice-1")
        client = mock_client()
        with patch.object(
            worker, "build_worker_client", return_value=client
        ):
            call_command("run_advice_worker", "--once")
        done = AdviceRun.objects.get(pk=run.pk)
        self.assertEqual(done.status, "success")
        # Each loop invocation installs its own handler, stops on SIGTERM,
        # and restores the previous handlers; a second invocation in the
        # same process must still run (no poisoned global state).
        calls = {"n": 0}
        orig = worker.run_worker_once

        def counting(*a, **k):
            calls["n"] += 1
            os.kill(os.getpid(), signal.SIGTERM)
            return orig(*a, **k)

        before_term = signal.getsignal(signal.SIGTERM)
        before_int = signal.getsignal(signal.SIGINT)
        for _ in range(2):
            calls["n"] = 0
            with patch.object(worker, "run_worker_once", side_effect=counting):
                call_command("run_advice_worker")
            self.assertGreaterEqual(calls["n"], 1)
            self.assertEqual(signal.getsignal(signal.SIGTERM), before_term)
            self.assertEqual(signal.getsignal(signal.SIGINT), before_int)


class WorkerSingletonLockTests(TestCase):
    def test_second_process_lock_rejected(self):
        import fcntl
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "worker.lock"
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                root = Path(__file__).resolve().parents[1]
                proc = subprocess.run(
                    [sys.executable, "manage.py", "run_advice_worker",
                     "--once", "--lock-file", str(lock_path)],
                    cwd=root, capture_output=True, text=True, timeout=60,
                )
            finally:
                os.close(fd)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("already running", proc.stderr + proc.stdout)

    def test_lock_released_after_once(self):
        import tempfile
        from pathlib import Path

        from django.core.management import call_command

        from portal import advice_worker as worker

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = str(Path(tmp) / "worker.lock")
            with patch.object(
                worker, "run_worker_once", return_value="idle"
            ) as once:
                call_command("run_advice_worker", "--once",
                             "--lock-file", lock_path)
                # Second invocation in the same process also acquires.
                call_command("run_advice_worker", "--once",
                             "--lock-file", lock_path)
            self.assertEqual(once.call_count, 2)
            self.assertEqual(Path(lock_path).stat().st_mode & 0o777, 0o600)


class OverduePhaseTests(TestCase):
    def test_overdue_fallback_phase_error_agrees(self):
        from portal.services import (
            _reserve_advice_run,
            authorize_dispatch,
            pending_deadline,
        )

        user, farm = make_owner("overdue-phase@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "overdue-phase-1")
        self.assertTrue(authorize_dispatch(run))
        AdviceRun.objects.filter(pk=run.pk).update(
            dispatch_authorized_at=timezone.now()
            - pending_deadline()
            - timedelta(seconds=60)
        )
        self.client.force_login(user)
        # Active farm session required by _active_or_404.
        session = self.client.session
        session["active_farm_id"] = farm.pk
        session.save()
        response = self.client.get(
            f"/tanacsok/{run.pk}/allapot/", HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "failed")
        self.assertEqual(payload.get("phase"), "error")
        self.assertIn('data-phase="error"', payload.get("html", ""))


class DetailPhaseTests(TestCase):
    def test_detail_carries_phase_and_status_url(self):
        from portal.services import _reserve_advice_run, authorize_dispatch

        for key, email, want_phase in (
            ("detail-q-1", "detail-q@pelda.hu", "queued"),
            ("detail-r-1", "detail-r@pelda.hu", "running"),
        ):
            user, farm = make_owner(email)
            run, _ = _reserve_advice_run(user, farm, key)
            if want_phase == "running":
                self.assertTrue(authorize_dispatch(run))
            self.client.force_login(user)
            session = self.client.session
            session["active_farm_id"] = farm.pk
            session.save()
            response = self.client.get(f"/tanacsok/{run.pk}/")
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertIn(f'data-phase="{want_phase}"', html)
            self.assertIn(f"/tanacsok/{run.pk}/allapot/", html)


class WorkerClientEndpointTests(TransactionTestCase):
    # Synthetic test-only settings: no real private key is required, and
    # the localhost endpoint is never contacted (MockTransport below).
    @override_settings(
        META_API_KEY="synthetic-test-only-key-task1",
        META_API_BASE_URL="http://localhost:18099/v1",
        META_MODEL="synthetic-test-model",
    )
    def test_default_client_uses_configured_endpoint_no_tx(self):
        import json as _json

        from portal.advice_worker import build_worker_client

        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = _json.loads(request.content.decode())
            # The provider call must stay outside any DB transaction.
            seen["in_atomic"] = connection.in_atomic_block
            body = {
                "model": "synthetic-test-model",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [
                    {"message": {"content": _json.dumps(valid_parsed())}}
                ],
            }
            return httpx.Response(200, json=body)

        real_client = httpx.Client

        def client_wrapper(*args, **kwargs):
            # Transport wrapper only: the client constructed by
            # build_worker_client() is the real object under test.
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        client = build_worker_client()
        self.assertEqual(client.api_key, "synthetic-test-only-key-task1")
        self.assertEqual(client.base_url, "http://localhost:18099/v1")
        self.assertEqual(client.model, "synthetic-test-model")
        self.assertFalse(connection.in_atomic_block)
        with patch.object(httpx, "Client", client_wrapper):
            parsed = client.generate_advice({"talaj": "homok"})
        self.assertEqual(
            seen["url"], "http://localhost:18099/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer synthetic-test-only-key-task1")
        self.assertEqual(seen["body"].get("model"), "synthetic-test-model")
        self.assertFalse(seen["in_atomic"])
        self.assertIn("steps", parsed)
        self.assertFalse(connection.in_atomic_block)


class DetailStalePhaseTests(TestCase):
    def test_detail_overdue_shows_phase_error(self):
        from portal.services import _reserve_advice_run, authorize_dispatch, pending_deadline
        user, farm = make_owner("detail-stale@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "detail-stale-1")
        self.assertTrue(authorize_dispatch(run))
        AdviceRun.objects.filter(pk=run.pk).update(dispatch_authorized_at=timezone.now()-pending_deadline()-timedelta(seconds=60))
        self.client.force_login(user); s=self.client.session; s["active_farm_id"]=farm.pk; s.save()
        html=self.client.get(f"/tanacsok/{run.pk}/").content.decode()
        self.assertIn('data-phase="error"', html)
        self.assertIn('data-status="failed"', html)


class RequestPageRecoveryTests(TestCase):
    def test_request_page_resumes_existing_pending(self):
        from portal.services import _reserve_advice_run
        user, farm = make_owner("req-recovery@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "req-recovery-1")
        user2, farm2 = make_owner("req-other@pelda.hu")
        run2, _ = _reserve_advice_run(user2, farm2, "req-other-1")
        self.client.force_login(user); s=self.client.session; s["active_farm_id"]=farm.pk; s.save()
        html=self.client.get("/tanacs/keres/").content.decode()
        self.assertIn("data-advice-poll", html)
        self.assertIn(f"/tanacsok/{run.pk}/allapot/", html)
        self.assertNotIn(f"/tanacsok/{run2.pk}/allapot/", html)
        import pathlib as _pl
        js=_pl.Path("portal/static/portal/advice.js").read_text()
        self.assertNotIn("localStorage", js)


class EnglishQueuedLabelTests(TestCase):
    def test_english_queued_label(self):
        from portal.services import _reserve_advice_run
        user, farm = make_owner("en-queued@pelda.hu")
        run, _ = _reserve_advice_run(user, farm, "en-queued-1")
        self.client.force_login(user); s=self.client.session; s["active_farm_id"]=farm.pk; s.save()
        payload=self.client.get(f"/tanacsok/{run.pk}/allapot/", HTTP_ACCEPT="application/json", HTTP_ACCEPT_LANGUAGE="en").json()
        self.assertEqual(payload.get("phase"), "queued")
        self.assertIn("queued", payload.get("html",""))
