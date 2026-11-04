"""Agent UI tests: translated shell, original plan text, before/after, hashes.

English comments per project convention; user-visible strings stay HU/EN.
Renders the real detail page built through the real changeset boundary
(prepare_changeset); no provider, network or secret access.
The receipt row below is synthetic preview metadata
used only to prove the template renders receipt values when present; the
views currently supply preview changesets only.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.agent.models import AgentChangeset, AgentRun
from portal.models import ConsentEvent, Farm, FarmAction

User = get_user_model()

STEP_TITLE = "Talajmegfigyelés"
STEP_ACTION = "Heti talajmegfigyelés rögzítése."
STEP_RATIONALE = "A profil talajjegyzeteire épül."
STEP_RISK = "low"
UNKNOWN_ITEM = "Csapadék alakulása"


def _user(email):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(user, name="Agent UI farm"):
    farm = Farm.objects.create(owner=user, name=name,
                               region_text="Szeged környéke")
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return farm


def _login(client, user, farm):
    client.force_login(user)
    session = client.session
    session["active_farm_id"] = farm.pk
    session.save()


def _verified_run(farm, actor, key):
    from portal.agent import policy_gate
    policy = policy_gate.ensure_policy()
    run = AgentRun(
        farm=farm, initiated_by=actor, idempotency_key=key,
        payload_hash="p" * 64, goal="Búza vetés előkészítés",
        language="hu", status="preview", stage=7,
        context_manifest={"items": []},
        policy_version=policy["version"],
        policy_sha256=policy["sha256"],
        candidate={"steps": []}, critic_findings=[])
    run.save()
    return run


def _prep(actor, farm, commands, run, key):
    from portal.agent import changeset as cs
    run.refresh_from_db()
    step = {"title": STEP_TITLE, "action_text": STEP_ACTION,
            "rationale": STEP_RATIONALE, "evidence": ["farm:profile"],
            "uncertainties": ["Időjárás"], "risk": STEP_RISK,
            "commands": [dict(c) for c in commands]}
    cand = dict(run.candidate or {})
    cand.update({
        "agricultural_goal": "Tesztcél",
        "summary": "Tesztösszefoglaló",
        "steps": [step], "risk": STEP_RISK,
        "unknowns": [UNKNOWN_ITEM]})
    run.candidate = cand
    run.save(update_fields=["candidate", "updated_at"])
    return cs.prepare_changeset(
        actor, farm, commands, run=run, idempotency_key=key)


class AgentDetailUiTests(TestCase):
    def _setup_preview(self, key="agent-ui-1"):
        user = _user("agent-ui@pelda.hu")
        farm = _farm(user)
        action = FarmAction.objects.create(
            farm=farm, text="Régi szöveg", status="open")
        run = _verified_run(farm, user, key)
        commands = [{"target": "action", "op": "update", "id": action.pk,
                     "fields": {"text": "Új szöveg"}}]
        cs, _token = _prep(user, farm, commands, run, key + "-cs")
        return user, farm, run, cs

    def test_hungarian_detail_shows_steps_before_after_hashes(self):
        user, farm, run, cs = self._setup_preview()
        _login(self.client, user, farm)
        response = self.client.get(f"/agent/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Original plan step text is retained verbatim.
        self.assertIn(STEP_TITLE, body)
        self.assertIn(STEP_ACTION, body)
        self.assertIn(STEP_RATIONALE, body)
        self.assertIn(UNKNOWN_ITEM, body)
        # Stored before snapshot and requested after values.
        self.assertIn("Régi szöveg", body)
        self.assertIn("Új szöveg", body)
        # Integrity hashes and expiry checks are rendered.
        self.assertIn(cs.payload_hash, body)
        self.assertIn(cs.before_hash, body)
        # No raw JSON blob rendering and no script-side HTML injection.
        self.assertNotIn("innerHTML", body)

    def test_english_cookie_translates_shell_keeps_original(self):
        user, farm, run, _cs = self._setup_preview(key="agent-ui-en")
        _login(self.client, user, farm)
        self.client.cookies.load({"django_language": "en"})
        response = self.client.get(f"/agent/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('<html lang="en">', body)
        self.assertIn("Agent run", body)
        self.assertNotIn("Ügynökfutás", body)
        # Original Hungarian plan content is data, never translated.
        self.assertIn(STEP_TITLE, body)
        self.assertIn(STEP_ACTION, body)

    def test_receipt_values_render_when_present(self):
        user, farm, run, cs = self._setup_preview(key="agent-ui-rc")
        AgentChangeset.objects.filter(pk=cs.pk).update(receipt={
            "csid": cs.pk, "payload_hash": cs.payload_hash,
            "before_hash": cs.before_hash, "after_hash": "a" * 64,
            "approved_by": user.pk, "verified": True, "audits": []})
        _login(self.client, user, farm)
        response = self.client.get(f"/agent/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("after_hash", body)
        self.assertIn("a" * 64, body)

    def test_approved_changeset_renders_verified_receipt_on_detail(self):
        # Approved changesets render the verified receipt, not the form.
        from django.urls import reverse
        from portal.agent import changeset as cs_mod
        user, farm, run, cs = self._setup_preview(key="agent-ui-appr")
        token = cs_mod.make_token(cs)
        receipt = cs_mod.approve_changeset(
            user, farm, cs.pk, token, cs.payload_hash)
        _login(self.client, user, farm)
        response = self.client.get(f"/agent/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(str(receipt["after_hash"]), body)
        self.assertIn("verified", body)
        approve_url = reverse(
            "agent_changeset_approve", args=[run.pk, cs.pk])
        self.assertNotIn(approve_url, body)

    def test_other_user_run_is_hidden(self):
        user, farm, run, _cs = self._setup_preview(key="agent-ui-priv")
        other = _user("agent-ui-masik@pelda.hu")
        other_farm = _farm(other, name="Agent UI masik farm")
        _login(self.client, other, other_farm)
        response = self.client.get(f"/agent/{run.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Régi szöveg",
                         response.content.decode(errors="ignore"))
