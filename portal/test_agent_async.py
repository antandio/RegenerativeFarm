"""Async agent worker lifecycle: enqueue, claim, finish, isolate, scrub.

English comments per project convention. Synthetic provider only; no
real paid calls in this run.
"""
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from portal.agent.models import AgentCall, AgentChangeset, AgentRun
from portal.agent.run import enqueue_run
from portal.models import ConsentEvent, Farm

User = get_user_model()


def _user(email="gazda@pelda.hu"):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(user, name="Tesztgazdasag"):
    return Farm.objects.create(owner=user, name=name,
                               region_text="Szeged kornyeke")


def _grant(farm, user):
    return ConsentEvent.objects.create(
        farm=farm, actor=user, event_type="granted")


class FakeAgentClient:
    """Synthetic HTTP provider double: records prompts, one attempt."""

    def __init__(self, candidate):
        self._candidate = candidate
        self.calls = []
        self.messages_log = []

    def classify_agri_call(self, text):
        self.calls.append("classify")
        return {"verdict": "agri", "input_tokens": 3, "output_tokens": 2,
                "cost_estimate": 0.0001}

    def plan(self, messages, limits):
        self.calls.append("plan")
        self.messages_log.append(("plan", messages))
        return {"candidate": self._candidate, "input_tokens": 10,
                "output_tokens": 20, "cost_estimate": 0.0001}

    def critique(self, messages, limits):
        self.calls.append("critic")
        self.messages_log.append(("critic", messages))
        return {"verdict": "verified", "findings": [],
                "input_tokens": 5, "output_tokens": 5,
                "cost_estimate": 0.0001}


def _good_candidate(source_id="farm:profile"):
    return {
        "agricultural_goal": "Talajkimelo tavaszi vetes",
        "summary": "Rovid osszefoglalo.",
        "steps": [{
            "title": "Talajvizsgalat",
            "action_text": "Talajminta vetel es vizsgalat.",
            "rationale": "Indoklas.",
            "evidence": [{"source_id": source_id,
                           "policy_section": "test",
                           "uncertainty": "alacsony"}],
            "uncertainties": ["idojaras"],
            "risk": "low",
            "commands": [],
        }],
        "risk": "low",
        "unknowns": ["csapadek"],
    }


@override_settings(AGENT_WORKER_LOCK_FILE=str(
    Path(tempfile.mkdtemp()) / "agent-worker-test.lock"))
class AgentAsyncLifecycleTests(TestCase):
    def test_enqueue_is_reserve_only_no_provider_calls(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Vetes",
                          idempotency_key="k1")
        self.assertEqual(run.status, "queued")
        self.assertEqual(AgentCall.objects.filter(run=run).count(), 0)
        self.assertIn("selected_property_ids",
                      run.input_snapshot or {})

    def test_duplicate_http_run_single_provider_run(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        web = Client()
        web.force_login(user)
        web.post("/agent/", {"goal": "Vetes", "cost_consent": "on",
                                     "idempotency_key": "dup1"})
        web.post("/agent/", {"goal": "Vetes", "cost_consent": "on",
                                     "idempotency_key": "dup1"})
        count = AgentRun.objects.filter(farm=farm).count()
        self.assertEqual(count, 1)

    def test_get_detail_status_do_zero_writes(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Vetes",
                          idempotency_key="k2")
        before = list(AgentRun.objects.filter(pk=run.pk).values_list(
            "updated_at", flat=True))
        web = Client()
        web.force_login(user)
        session = web.session
        session["active_farm_id"] = farm.pk
        session.save()
        resp = web.get(f"/agent/{run.pk}/")
        self.assertEqual(resp.status_code, 200)
        resp = web.get(f"/agent/{run.pk}/status/")
        self.assertEqual(resp.status_code, 200)
        after = list(AgentRun.objects.filter(pk=run.pk).values_list(
            "updated_at", flat=True))
        self.assertEqual(before, after)
        self.assertEqual(AgentCall.objects.filter(run=run).count(), 0)

    def test_worker_finish_synthetic_provider_prompt_has_policy_and_context(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Vetes tervezes",
                          idempotency_key="k3")
        from portal.agent.worker import run_worker_once
        client = FakeAgentClient(_good_candidate())
        result = run_worker_once(client=client)
        self.assertIn("processed", result)
        run.refresh_from_db()
        self.assertIn(run.status, ("done", "preview"))
        kinds = [c.kind for c in
                 AgentCall.objects.filter(run=run)]
        self.assertIn("planner", kinds)
        blob = json.dumps(client.messages_log, ensure_ascii=False)
        self.assertIn(run.policy_sha256, blob)
        self.assertIn("farm:profile", blob)

    def test_cross_user_farm_404(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Vetes",
                          idempotency_key="k4")
        other = User.objects.create_user(email="mas@pelda.hu",
                                         password="TesztJelszo123!")
        web = Client()
        web.force_login(other)
        self.assertEqual(web.get(f"/agent/{run.pk}/").status_code, 404)
        self.assertEqual(
            web.get(f"/agent/{run.pk}/status/").status_code, 404)

    def test_stale_claimed_unknown_no_replay_unclaimed_queued_survives(self):
        from datetime import timedelta
        from django.utils import timezone
        from portal.agent.worker import reap_stale

        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        stale = enqueue_run(farm=farm, actor=user, goal="Regi",
                            idempotency_key="k5")
        AgentRun.objects.filter(pk=stale.pk).update(
            status="running", dispatch_authorized_at=timezone.now(),
            lease_expires_at=timezone.now() - timedelta(seconds=1))
        waiting = enqueue_run(farm=farm, actor=user, goal="Varakozo",
                              idempotency_key="k6")
        reaped = reap_stale()
        self.assertEqual(reaped, 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, "outcome_unknown")
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, "queued")

    def test_delete_scrubs_and_late_callback_no_resurrect(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Torlendo kerdes",
                          idempotency_key="k7")
        web = Client()
        web.force_login(user)
        session = web.session
        session["active_farm_id"] = farm.pk
        session.save()
        resp = web.post(f"/agent/{run.pk}/delete/")
        self.assertIn(resp.status_code, (302, 303))
        row = AgentRun.objects.filter(pk=run.pk).values(
            "status", "goal", "context_manifest", "candidate").first()
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(row["goal"], "")
        self.assertEqual(
            AgentRun.objects.filter(pk=run.pk).count(), 1)
        from portal.agent.worker import run_worker_once
        late = run_worker_once(client=FakeAgentClient(_good_candidate()))
        self.assertIn("idle", late)
        scrubbed = AgentRun.objects.filter(pk=run.pk).values(
            "status", "goal", "context_manifest", "candidate").first()
        self.assertEqual(scrubbed["status"], "deleted")
        self.assertEqual(scrubbed["goal"], "")
        self.assertFalse((scrubbed["candidate"] or {}).get("steps"))

    def test_delete_during_provider_inflight_no_resurrect(self):
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Torlendo kerdes",
                          idempotency_key="k7-inflight")
        web = Client()
        web.force_login(user)
        session = web.session
        session["active_farm_id"] = farm.pk
        session.save()
        holder = {"web": web, "run_pk": run.pk}

        class InflightDeleteClient(FakeAgentClient):
            def plan(self, messages, limits):
                holder["web"].post(
                    f"/agent/{holder['run_pk']}/delete/")
                return super().plan(messages, limits)

        from portal.agent.worker import run_worker_once
        result = run_worker_once(
            client=InflightDeleteClient(_good_candidate()))
        self.assertIn("processed", result)
        row = AgentRun.objects.filter(pk=run.pk).values(
            "status", "goal", "context_manifest", "candidate").first()
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(row["goal"], "")
        self.assertFalse((row["candidate"] or {}).get("steps"))
        self.assertEqual(
            AgentChangeset.objects.filter(run_id=run.pk).count(), 0)

    def test_same_farm_manager_cannot_read_others(self):
        from portal.models import Membership
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        run = enqueue_run(farm=farm, actor=user, goal="Vetes",
                          idempotency_key="k8")
        other = User.objects.create_user(email="manager@pelda.hu",
                                         password="TesztJelszo123!")
        Membership.objects.create(
            farm=farm, user=other, role="manager", is_active=True)
        web = Client()
        web.force_login(other)
        session = web.session
        session["active_farm_id"] = farm.pk
        session.save()
        resp_home = web.get("/agent/")
        self.assertEqual(resp_home.status_code, 200)
        self.assertEqual(web.get(f"/agent/{run.pk}/").status_code, 404)
        self.assertEqual(
            web.get(f"/agent/{run.pk}/status/").status_code, 404)
        resp = web.get("/agent/")
        self.assertIn(resp.status_code, (200, 404))
        if resp.status_code == 200:
            self.assertNotIn("Vetes", resp.content.decode())
        self.assertEqual(
            web.post(f"/agent/{run.pk}/delete/").status_code, 404)
        self.assertEqual(web.post(
            f"/agent/{run.pk}/changeset/999999/approve/",
            {"token": "x", "payload_hash": "y"}).status_code, 404)
