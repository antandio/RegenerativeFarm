"""Agent lifecycle: HTTP enqueue, worker preview, HTTP approve.

English comments per project convention. Reuses the synthetic
provider from portal.test_agent_async (header only).
"""
from portal.test_agent_async import FakeAgentClient, _farm, _grant, _user

import tempfile
from pathlib import Path

from django.test import Client, TestCase, override_settings

from portal.agent.models import AgentChangeset, AgentRun
from portal.models import FarmAction


def _action_candidate(source_id="farm:profile"):
    return {
        "agricultural_goal": "Talajkimelo tavaszi vetes",
        "summary": "Rovid osszefoglalo.",
        "steps": [{
            "title": "Talajmegfigyeles",
            "action_text": "Talajminta vetel es vizsgalat.",
            "rationale": "Indoklas.",
            "evidence": [{"source_id": source_id,
                          "policy_section": "test",
                          "uncertainty": "alacsony"}],
            "uncertainties": ["idojaras"],
            "risk": "low",
            "commands": [{"target": "action", "op": "create",
                          "fields": {"text": "Talajmegfigyeles"}}],
        }],
        "risk": "low",
        "unknowns": ["csapadek"],
    }


@override_settings(AGENT_WORKER_LOCK_FILE=str(
    Path(tempfile.mkdtemp()) / "agent-lifecycle-test.lock"))
class AgentLifecycleTests(TestCase):
    def test_http_enqueue_worker_preview_http_approve(self):
        from portal.agent.worker import run_worker_once
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        web = Client()
        web.force_login(user)
        session = web.session
        session["active_farm_id"] = farm.pk
        session.save()
        resp = web.post("/agent/", {"goal": "Vetes",
                                    "cost_consent": "on",
                                    "idempotency_key": "life-1"})
        self.assertIn(resp.status_code, (200, 302, 303))
        run = AgentRun.objects.filter(farm=farm).first()
        self.assertIsNotNone(run)
        result = run_worker_once(
            client=FakeAgentClient(_action_candidate()))
        self.assertIn("processed", result)
        run.refresh_from_db()
        self.assertEqual(run.status, "preview")
        self.assertEqual(run.stage, 5)
        css = list(AgentChangeset.objects.filter(
            run=run, state="preview"))
        self.assertEqual(len(css), 1)
        cs = css[0]
        from portal.agent import changeset as cs_mod
        token = cs_mod.make_token(cs)
        before_actions = FarmAction.objects.filter(farm=farm).count()
        resp = web.post(
            f"/agent/{run.pk}/changeset/{cs.pk}/approve/",
            {"token": token, "payload_hash": cs.payload_hash})
        self.assertIn(resp.status_code, (200, 302, 303))
        run.refresh_from_db()
        self.assertEqual(run.status, "done")
        self.assertEqual(run.stage, 7)
        stages = [e.get("stage") for e in (run.stage_events or [])]
        self.assertIn(6, stages)
        self.assertIn(7, stages)
        cs.refresh_from_db()
        self.assertTrue((cs.receipt or {}).get("verified"))
        self.assertEqual(
            FarmAction.objects.filter(farm=farm).count(),
            before_actions + 1)

    def test_deleted_preview_scrub(self):
        from portal.agent.worker import run_worker_once
        user = _user(email="torolt@pelda.hu")
        farm = _farm(user, name="Toroltfarm")
        _grant(farm, user)
        web = Client()
        web.force_login(user)
        session = web.session
        session["active_farm_id"] = farm.pk
        session.save()
        web.post("/agent/", {"goal": "Vetes", "cost_consent": "on",
                             "idempotency_key": "life-2"})
        run = AgentRun.objects.filter(farm=farm).first()
        run_worker_once(client=FakeAgentClient(_action_candidate()))
        run.refresh_from_db()
        cs = AgentChangeset.objects.filter(run=run).first()
        self.assertIsNotNone(cs)
        phash = cs.payload_hash
        resp = web.post(f"/agent/{run.pk}/delete/")
        self.assertIn(resp.status_code, (302, 303))
        cs.refresh_from_db()
        self.assertEqual(cs.state, "rejected")
        self.assertEqual(cs.payload_hash, phash)
        self.assertEqual(cs.commands, [])
        self.assertEqual(cs.before_snapshot, {})
        self.assertEqual(cs.after_snapshot, {})
        self.assertEqual(cs.source_vector, [])
        self.assertEqual(cs.receipt, {})

    def test_missing_critic_denies_stage5(self):
        from portal.agent import changeset as cs_mod
        from portal.agent import policy_gate
        user = _user(email="kritika@pelda.hu")
        farm = _farm(user, name="Kritikafarm")
        _grant(farm, user)
        policy = policy_gate.ensure_policy()
        candidate = _action_candidate()
        import hashlib
        import json
        sha = hashlib.sha256(json.dumps(
            candidate, ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()
        run = AgentRun(
            farm=farm, initiated_by=user, idempotency_key="life-3",
            payload_hash="p" * 64, goal="Vetes", language="hu",
            status="preview", stage=5,
            context_manifest={"items": []},
            policy_version=policy["version"],
            policy_sha256=policy["sha256"], candidate=candidate,
            critic_findings=[],
            revalidation={"critic_verified_candidate_sha": sha})
        run.save()
        with self.assertRaises(Exception):
            cs_mod.prepare_changeset(
                user, farm, [{"target": "action", "op": "create",
                              "fields": {"text": "Talajmegfigyeles"}}],
                run=run, idempotency_key="life-3-cs")
