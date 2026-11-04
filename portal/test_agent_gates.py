"""Gates: actor/consent/critic/budget/idempotency (RED first, then GREEN).

Isolated synthetic DB, mocked model transport only. No live provider.
"""
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from portal.agent import budgets
from portal.domain_models import Document, FarmDocument
from portal.models import ConsentEvent, Farm

User = get_user_model()


def _user(email="kapu@pelda.hu"):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(user, name="Kapufarm"):
    return Farm.objects.create(owner=user, name=name,
                               region_text="Szeged környéke")


def _grant(farm, user):
    return ConsentEvent.objects.create(
        farm=farm, actor=user, event_type="granted")


def _revoke(farm, user):
    return ConsentEvent.objects.create(
        farm=farm, actor=user, event_type="revoked")


def _good_candidate(source_id="farm:profile", with_commands=False):
    step = {"title": "Megfigyelés",
            "action_text": "Heti talajmegfigyelés rögzítése.",
            "rationale": "A profil talajjegyzeteire épül.",
            "evidence": [{"source_id": source_id,
                          "policy_section": "4.2",
                          "uncertainty": "alacsony"}],
            "uncertainties": ["csapadék alakulása"],
            "risk": "low",
            "commands": []}
    if with_commands:
        step["commands"] = [{"target": "action", "op": "create",
                             "fields": {"text": "Talajmegfigyelés feljegyzése"}}]
    return {"agricultural_goal": "Talajkímélő tavaszi vetés előkészítése",
            "summary": "Rövid összefoglaló a profil alapján.",
            "steps": [step], "risk": "low",
            "unknowns": ["pontos parcellaméret"]}


class FakeClient:
    def __init__(self, candidate, verdict="verified", findings="default",
                 agri="agri", calls=None, on_plan=None, on_critique=None):
        self._candidate = candidate
        self._verdict = verdict
        self._findings = findings
        self._agri = agri
        self.calls = calls if calls is not None else []
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
        return {"verdict": self._verdict, "findings": findings,
                "input_tokens": 5, "output_tokens": 5}

    def classify_agri(self, text):
        self.calls.append("classify")
        return self._agri

    def classify_agri_call(self, text):
        self.calls.append("classify")
        return {"verdict": self._agri, "input_tokens": 3,
                "output_tokens": 2}


class TempPrivateRootMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory(prefix="priv-")
        cls._override = override_settings(
            PRIVATE_DOCUMENT_ROOT=str(Path(cls._tmp.name)))
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        cls._tmp.cleanup()
        super().tearDownClass()


class BudgetDefaultTests(TestCase):
    def test_defaults_match_spec(self):
        limits = budgets.get_limits()
        self.assertEqual(limits["max_calls"], 7)
        self.assertEqual(limits["max_revisions"], 2)
        self.assertEqual(limits["max_tokens"], 60000)
        self.assertEqual(limits["max_wall_seconds"], 300)
        self.assertEqual(limits["max_cost"], 0.25)


class ActorGateTests(TestCase):
    def test_revoked_actor_makes_no_calls(self):
        from portal.agent import run as runmod
        from portal.agent.models import AgentCall
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        user.is_active = False
        user.save(update_fields=["is_active"])
        fake = FakeClient(_good_candidate())
        with self.assertRaises((PermissionError, ValueError)):
            runmod.start_run(
                farm=farm, actor=user,
                goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
                idempotency_key="revoked-actor-1", client=fake,
                limits_overrides={"allow_unknown_cost": True})
        self.assertEqual(AgentCall.objects.count(), 0)
        self.assertEqual(fake.calls, [])


class ConsentGateTests(TestCase):
    def test_revoke_callback_before_critic_stops(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        holder = {}

        def revoke_now():
            holder["fake_ref"].calls.append("revoke-cb")
            _revoke(farm, user)

        fake = FakeClient(_good_candidate(), on_plan=revoke_now)
        holder["fake_ref"] = fake
        run = runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="revoke-mid-1", client=fake,
            limits_overrides={"allow_unknown_cost": True})
        self.assertEqual(run.status, "blocked")
        self.assertEqual(run.error_code, "consent_missing")
        self.assertNotIn("critic", fake.calls)


class CriticContentTests(TestCase):
    def test_critic_receives_source_contents_and_full_policy(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake = FakeClient(_good_candidate())
        runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="critic-content-1", client=fake,
            limits_overrides={"allow_unknown_cost": True})
        critics = [m for k, m in fake.messages_log if k == "critic"]
        self.assertTrue(critics)
        blob = str(critics[0])
        self.assertIn("farm:profile", blob)
        # Full source text + full canonical policy travel, not only IDs.
        self.assertIn("Canonical policy", blob)
        self.assertGreater(len(blob), 500)

    def test_critic_malformed_findings_block(self):
        from portal.provider import ProviderInvalidOutput
        from portal.agent.client import AgentClient

        class BadTransport:
            pass

        client = AgentClient(api_key="x", transport=BadTransport())
        client._post = lambda messages, cap, timeout_seconds=None: (
            '{"verdict": "verified", "findings": "not-a-list"}',
            5, 5, "muse-spark", None)
        with self.assertRaises(ProviderInvalidOutput):
            client.critique([{"role": "user", "content": "hi"}], {})


class BudgetCeilingTests(TestCase):
    def test_token_wall_cost_ceilings_reserve_deny(self):
        from portal.agent import run as runmod
        from portal.agent.models import AgentCall
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake = FakeClient(_good_candidate())
        run = runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="ceil-1", client=fake,
            limits_overrides={"allow_unknown_cost": True,
                               "max_calls": 1})
        # Only the classifier reservation fits; planner must be denied.
        self.assertEqual(run.status, "outcome_unknown")
        kinds = list(AgentCall.objects.filter(run=run).values_list(
            "kind", flat=True))
        self.assertIn("agri_classify", kinds)
        self.assertNotIn("critic", kinds)


class IdempotencyTests(TestCase):
    def test_same_key_same_payload_returns_same_row(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        goal = "Tavaszi vetés talajkímélő előkészítése 5 hektáron"
        fake = FakeClient(_good_candidate())
        run1 = runmod.start_run(
            farm=farm, actor=user, goal=goal, idempotency_key="idem-1",
            client=fake, limits_overrides={"allow_unknown_cost": True})
        fake2 = FakeClient(_good_candidate())
        run2 = runmod.start_run(
            farm=farm, actor=user, goal=goal, idempotency_key="idem-1",
            client=fake2, limits_overrides={"allow_unknown_cost": True})
        self.assertEqual(run1.pk, run2.pk)
        self.assertEqual(fake2.calls, [])

    def test_same_key_different_payload_rejected(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake = FakeClient(_good_candidate())
        runmod.start_run(
            farm=farm, actor=user, goal="Tavaszi vetés 5 hektáron",
            idempotency_key="idem-2", client=fake,
            limits_overrides={"allow_unknown_cost": True})
        fake2 = FakeClient(_good_candidate())
        with self.assertRaises(ValueError):
            runmod.start_run(
                farm=farm, actor=user, goal="Egészen más cél",
                idempotency_key="idem-2", client=fake2,
                limits_overrides={"allow_unknown_cost": True})


class PreviewStatusTests(TestCase):
    def test_commands_yield_preview_readonly_yields_done(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        fake_cmd = FakeClient(_good_candidate(with_commands=True))
        run_cmd = runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="preview-1", client=fake_cmd,
            limits_overrides={"allow_unknown_cost": True})
        self.assertEqual(run_cmd.status, "preview")
        fake_ro = FakeClient(_good_candidate(with_commands=False))
        run_ro = runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="preview-2", client=fake_ro,
            limits_overrides={"allow_unknown_cost": True})
        self.assertEqual(run_ro.status, "done")

    def test_invalid_command_blocks_candidate(self):
        # Unknown command targets never reach preview; the run blocks.
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)
        candidate = _good_candidate()
        candidate["steps"][0]["commands"] = [
            {"target": "observation", "op": "create",
             "fields": {"note": "x"}}]
        fake = FakeClient(candidate)
        run = runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="preview-invalid-1", client=fake,
            limits_overrides={"allow_unknown_cost": True})
        self.assertEqual(run.status, "blocked")


class DeletedRowTests(TestCase):
    def test_late_callback_does_not_reset_deleted(self):
        from portal.agent import run as runmod
        user = _user()
        farm = _farm(user)
        _grant(farm, user)

        def delete_now():
            runmod.AgentRun.objects.filter(
                idempotency_key="del-1", farm=farm).update(status="deleted")

        fake = FakeClient(_good_candidate(), on_critique=delete_now)
        run = runmod.start_run(
            farm=farm, actor=user,
            goal="Tavaszi vetés talajkímélő előkészítése 5 hektáron",
            idempotency_key="del-1", client=fake,
            limits_overrides={"allow_unknown_cost": True})
        run.refresh_from_db()
        self.assertEqual(run.status, "deleted")
