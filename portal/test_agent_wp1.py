"""WP1 sourced planner: focused RED->GREEN tests (no full suite here).

Uses an isolated synthetic DB, mocked model transport and the shared
weather cache only (no network). Real provider calls never happen here.
"""
import hashlib
import json

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from portal.models import (
    Farm, ConsentEvent, WorkItem, AgentRun, AgentCall, AgentDocGrant,
)
from portal.domain_models import Document, FarmDocument

User = get_user_model()


def _user(email="gazda@pelda.hu"):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(user, name="Tesztgazdaság"):
    return Farm.objects.create(owner=user, name=name,
                               region_text="Szeged környéke")


def _grant(farm, user):
    return ConsentEvent.objects.create(
        farm=farm, actor=user, event_type="granted")


class FakeAgentClient:
    """Injectable stand-in for the bounded model client."""

    def __init__(self, candidate, critic_verdict="verified", calls=None,
                 agri="agri", findings="default", on_plan=None,
                 on_critique=None):
        self._candidate = candidate
        self._critic_verdict = critic_verdict
        self.calls = calls if calls is not None else []
        self._agri = agri
        self._findings = findings
        self.on_plan = on_plan
        self.on_critique = on_critique
        self.messages_log = []

    def plan(self, messages, limits):
        self.calls.append("plan")
        self.messages_log.append(("plan", messages))
        if self.on_plan:
            self.on_plan()
        return {"candidate": self._candidate, "input_tokens": 10,
                "output_tokens": 20}

    def critique(self, messages, limits):
        self.calls.append("critic")
        self.messages_log.append(("critic", messages))
        if self.on_critique:
            self.on_critique()
        findings = [] if self._findings == "default" else self._findings
        return {"verdict": self._critic_verdict, "findings": findings,
                "input_tokens": 5, "output_tokens": 5}

    def classify_agri(self, text):
        self.calls.append("classify")
        return self._agri

    def classify_agri_call(self, text):
        self.calls.append("classify")
        return {"verdict": self._agri, "input_tokens": 3,
                "output_tokens": 2}


def _good_candidate(source_id="farm:profile"):
    return {
        "agricultural_goal": "Talajkímélő tavaszi vetés előkészítése",
        "summary": "Rövid összefoglaló a profil alapján.",
        "steps": [
            {"title": "Megfigyelés",
             "action_text": "Heti talajmegfigyelés rögzítése.",
             "rationale": "A profil talajjegyzeteire épül.",
             "evidence": [{"source_id": source_id,
                           "policy_section": "4.2",
                           "uncertainty": "alacsony"}],
             "uncertainties": ["csapadék alakulása"],
             "risk": "low",
             "commands": []},
        ],
        "risk": "low",
        "unknowns": ["pontos parcellaméret"],
    }


class AgentBudgetTests(TestCase):
    def test_limits_have_nonzero_defaults(self):
        from portal.agent import budgets
        limits = budgets.get_limits()
        self.assertGreaterEqual(limits["max_calls"], 1)
        self.assertGreaterEqual(limits["max_revisions"], 1)
        self.assertLessEqual(limits["max_revisions"], 2)
        self.assertGreater(limits["max_cost"], 0)
        self.assertIsNot(limits["unknown_cost"], 0)


class AgentGateTests(TestCase):
    def test_agri_text_passes_both_gates(self):
        from portal.agent import agri_gate
        text = ("A búza tavaszi vetéséhez milyen talajkímélő "
                "előkészítést javasolsz 5 hektáron?")
        self.assertEqual(agri_gate.local_screen(text), "agri")
        fake = FakeAgentClient(_good_candidate(), agri="agri")
        self.assertTrue(agri_gate.is_agricultural_request(text, client=fake))

    def test_nonagri_text_blocked(self):
        from portal.agent import agri_gate
        text = "How do I pick numbers for the weekend casino jackpot?"
        self.assertEqual(agri_gate.local_screen(text), "nonagri")
        fake = FakeAgentClient(_good_candidate(), agri="nonagri")
        self.assertFalse(agri_gate.is_agricultural_request(text, client=fake))


class AgentPolicyTests(TestCase):
    def test_missing_policy_blocks(self):
        from portal.agent import policy_gate
        from unittest.mock import patch
        with patch("portal.agent.policy_gate._load_raw",
                   side_effect=OSError("gone")):
            with self.assertRaises(policy_gate.PolicyBlocked):
                policy_gate.ensure_policy()


class TempPrivateRootMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        cls._tmp = tempfile.TemporaryDirectory(prefix="priv-")
        cls._override = override_settings(
            PRIVATE_DOCUMENT_ROOT=str(Path(cls._tmp.name)))
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        cls._tmp.cleanup()
        super().tearDownClass()


class AgentContextTests(TempPrivateRootMixin, TestCase):
    def test_manifest_has_farm_profile_and_weather_unknown(self):
        from portal.agent import context
        user = _user()
        farm = _farm(user)
        manifest = context.build_manifest(
            farm=farm, actor=user,
            goal="Tavaszi vetés előkészítése",
            selected_doc_ids=[])
        ids = [i["source_id"] for i in manifest["items"]]
        self.assertIn("farm:profile", ids)
        weather = [i for i in manifest["items"]
                   if i["source_id"].startswith("weather:")]
        self.assertTrue(weather)
        self.assertIn("unknown", weather[0]["uncertainty"].lower())
        profile = next(i for i in manifest["items"]
                       if i["source_id"] == "farm:profile")
        for key in ("hash", "locator", "trust", "relevance",
                    "uncertainty", "timestamp"):
            self.assertIn(key, profile)

    def test_ungranted_doc_excluded_and_listed_in_omissions(self):
        from portal.agent import context
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        doc = Document.objects.create(
            owner=user, name="Magánjegyzet",
            original_filename="jegyzet.md", storage_name="a" * 32 + ".md",
            extension=".md", content_type="text/markdown", size_bytes=10,
            sha256="b" * 64)
        FarmDocument.objects.create(
            farm=farm, document=doc, added_by=user, is_active=True)
        manifest = context.build_manifest(
            farm=farm, actor=user, goal="Vetés", selected_doc_ids=[])
        ids = [i["source_id"] for i in manifest["items"]]
        self.assertNotIn(f"document:{doc.pk}", ids)
        self.assertTrue(
            any("document" in str(o).lower()
                for o in manifest["omissions"]))

    def test_private_storage_symlink_rejected(self):
        from portal.agent import storage_sections
        from portal.document_storage import private_root
        import os
        root = private_root()
        link = os.path.join(os.fspath(root), "c" * 32 + ".md")
        try:
            os.symlink(os.devnull, link)
        except FileExistsError:
            pass
        res = storage_sections.read_text_section("c" * 32 + ".md")
        self.assertFalse(res["available"])
        try:
            os.unlink(link)
        except OSError:
            pass


class AgentSchemaTests(TestCase):
    def test_candidate_without_evidence_rejected(self):
        from portal.agent import schemas
        manifest = {"items": [{"source_id": "farm:profile"}],
                    "omissions": []}
        bad = {"agricultural_goal": "g", "summary": "s",
               "steps": [{"title": "t"}], "risk": "low", "unknowns": []}
        self.assertTrue(schemas.validate_candidate(bad, manifest))


    def test_delete_command_without_fields_accepted(self):
        from portal.agent import schemas
        manifest = {"items": [{"source_id": "farm:profile"}],
                    "omissions": []}
        candidate = _good_candidate()
        candidate["steps"][0]["commands"] = [
            {"target": "equipment", "op": "delete", "id": 1}]
        self.assertEqual(schemas.validate_candidate(candidate, manifest), [])


class AgentCriticTests(TestCase):
    def test_unsupported_claim_flagged(self):
        from portal.agent import critic
        manifest = {"items": [{"source_id": "farm:profile"}],
                    "omissions": []}
        candidate = _good_candidate(source_id="document:9999")
        result = critic.review_candidate(
            candidate, manifest,
            {"version": "1.0.0", "sha256": "x" * 64})
        self.assertNotEqual(result["verdict"], "verified")
        self.assertTrue(result["findings"])


class AgentRunTests(TestCase):
    def _run_kwargs(self, user, farm, fake, **extra):
        kwargs = dict(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="wp1-key-1", client=fake,
            limits_overrides={"allow_unknown_cost": True})
        kwargs.update(extra)
        return kwargs

    def test_run_identity_unique_and_durable(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake = FakeAgentClient(_good_candidate())
        run1 = runmod.start_run(**self._run_kwargs(user, farm, fake))
        self.assertIsNotNone(run1)
        self.assertEqual(run1.status, "done")
        run2 = runmod.start_run(**self._run_kwargs(user, farm, fake))
        self.assertEqual(run1.pk, run2.pk)

    def test_manifest_immutable_after_set(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake = FakeAgentClient(_good_candidate())
        run1 = runmod.start_run(**self._run_kwargs(user, farm, fake))
        run1.context_manifest = {"items": [], "omissions": ["tampered"]}
        with self.assertRaises(ValueError):
            run1.save()

    def test_revisions_bounded_and_unchanged_candidate_blocks(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        bad = _good_candidate(source_id="document:9999")
        fake = FakeAgentClient(bad, critic_verdict="needs_revision")
        run1 = runmod.start_run(**self._run_kwargs(user, farm, fake))
        self.assertEqual(run1.status, "blocked")
        plans = [c for c in fake.calls if c == "plan"]
        self.assertLessEqual(len(plans), 3)
        critics = [c for c in fake.calls if c == "critic"]
        self.assertGreaterEqual(len(critics), 1)

    def test_usage_ledger_records_every_call_unknown_not_zero(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake = FakeAgentClient(_good_candidate())
        run1 = runmod.start_run(**self._run_kwargs(user, farm, fake))
        calls = AgentCall.objects.filter(run=run1)
        self.assertGreaterEqual(calls.count(), 2)
        for call in calls:
            self.assertNotEqual(call.cost_estimate, 0)
            self.assertIn(call.status, ("success", "failed", "unknown"))

    def test_no_domain_writes_in_wp1(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        before = WorkItem.objects.filter(farm=farm).count()
        fake = FakeAgentClient(_good_candidate())
        runmod.start_run(**self._run_kwargs(user, farm, fake))
        after = WorkItem.objects.filter(farm=farm).count()
        self.assertEqual(before, after)
