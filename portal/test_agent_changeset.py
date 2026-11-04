"""Changeset approval executor: strict RED/GREEN tests (isolated DB).

Preview creates no domain records; approval with the specific token
writes; revoke, cross-farm, double-confirm, rollback and stale paths
are all exercised. Equipment and action cover full positive/negative
CRUD; work items cover the historical-done block.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.agent.models import AgentChangeset, AgentRun
from portal.models import (ConsentEvent, Equipment, Farm, FarmAction,
                           Membership, WorkItem)

User = get_user_model()


def _user(email="cs@pelda.hu"):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(user, name="Csfarm"):
    return Farm.objects.create(owner=user, name=name,
                               region_text="Szeged kornyeke")


def _grant(farm, user):
    return ConsentEvent.objects.create(
        farm=farm, actor=user, event_type="granted")


def _revoke(farm, user):
    return ConsentEvent.objects.create(
        farm=farm, actor=user, event_type="revoked")


def _location(farm, name="Kozpont"):
    from portal.models import FarmLocation
    return FarmLocation.objects.create(
        farm=farm, name=name, latitude="46.250000",
        longitude="20.150000", timezone="Europe/Budapest")


def _property(farm, location=None, name="Tabla"):
    from portal.models import FarmProperty
    location = location or _location(farm)
    return FarmProperty.objects.create(
        farm=farm, location=location, name=name, kind="field",
        area_hectares="5.00")


def _cycle(farm, prop=None, crop="Buza"):
    from portal.models import CropCycle
    prop = prop or _property(farm)
    return CropCycle.objects.create(
        farm=farm, property=prop, crop_name=crop, state="current")


def _verified_run(farm, actor):
    from portal.agent import policy_gate
    policy = policy_gate.ensure_policy()
    run = AgentRun(
        farm=farm, initiated_by=actor, idempotency_key="run-cs-1",
        payload_hash="p" * 64, goal="Buza vetes elokeszites",
        language="hu", status="preview", stage=7,
        context_manifest={"items": []},
        policy_version=policy["version"],
        policy_sha256=policy["sha256"],
        candidate={"steps": []}, critic_findings=[])
    run.save()
    return run


def _prep(actor, farm, commands, run=None, key=None, **kw):
    from portal.agent import changeset as cs
    kw.setdefault("allow_manual", run is None)
    if run is not None:
        # The verified run proposes exactly the prepared commands.
        run.refresh_from_db()
        cand = dict(run.candidate or {})
        steps = [s for s in (cand.get("steps") or [])
                 if not (isinstance(s, dict) and s.get("commands"))]
        steps.append({"title": "Tervlépés", "action_text": "Végrehajtás",
                      "rationale": "Teszt", "evidence": [],
                      "uncertainties": [],
                      "commands": [dict(c) for c in commands]})
        cand.update({
            "agricultural_goal": cand.get("agricultural_goal") or "Tesztcél",
            "summary": cand.get("summary") or "Tesztösszefoglaló",
            "steps": steps, "risk": cand.get("risk") or "low",
            "unknowns": cand.get("unknowns")
            if isinstance(cand.get("unknowns"), list) else []})
        run.candidate = cand
        run.save(update_fields=["candidate", "updated_at"])
    return cs.prepare_changeset(
        actor, farm, commands, run=run,
        idempotency_key=key or f"k-{abs(hash(repr(commands))) % 10**8}",
        **kw)


class PreviewTests(TestCase):
    def test_preview_creates_no_domain_records(self):
        actor = _user()
        farm = _farm(actor)
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        n_action = FarmAction.objects.count()
        n_eq = Equipment.objects.count()
        n_wi = WorkItem.objects.count()
        cs, token = _prep(actor, farm, [
            {"target": "action", "op": "create",
             "fields": {"text": "Talajmegfigyeles"}}], run=run, key="p1")
        self.assertEqual(cs.state, "preview")
        self.assertEqual(FarmAction.objects.count(), n_action)
        self.assertEqual(Equipment.objects.count(), n_eq)
        self.assertEqual(WorkItem.objects.count(), n_wi)
        self.assertTrue(token)

    def test_unknown_target_rejected(self):
        actor = _user(email="u2@pelda.hu")
        farm = _farm(actor, name="F2")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        with self.assertRaises(ValueError):
            _prep(actor, farm, [
                {"target": "tractor", "op": "create", "fields": {}}],
                run=run, key="p2")

    def test_unknown_keys_rejected(self):
        actor = _user(email="u3@pelda.hu")
        farm = _farm(actor, name="F3")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        with self.assertRaises(ValueError):
            _prep(actor, farm, [
                {"target": "action", "op": "create", "fields": {},
                 "MODEL": "x"}], run=run, key="p3")
        with self.assertRaises(ValueError):
            _prep(actor, farm, [
                {"target": "action", "op": "create",
                 "fields": {"text": "Ok", "UNKNOWN": 1}}],
                run=run, key="p4")

    def test_blocked_run_never_previews(self):
        from portal.agent import changeset as cs
        from portal.agent import policy_gate
        actor = _user(email="u4@pelda.hu")
        farm = _farm(actor, name="F4")
        _grant(farm, actor)
        policy = policy_gate.ensure_policy()
        run = AgentRun(
            farm=farm, initiated_by=actor, idempotency_key="blocked-1",
            payload_hash="q" * 64, goal="x", status="blocked", stage=7,
            context_manifest={"items": []},
            policy_version=policy["version"],
            policy_sha256=policy["sha256"],
            candidate={"steps": [{"commands": [
                {"target": "action", "op": "delete"}]}]},
            critic_findings=[{"severity": "critical", "step": 1,
                              "code": "high-cost-command",
                              "reason": "delete"}])
        run.save()
        with self.assertRaises(Exception):
            cs.prepare_changeset(
                actor, farm, [{"target": "action", "op": "create",
                               "fields": {"text": "x"}}],
                run=run, idempotency_key="p5")

    def test_direct_prepare_without_run_rejected(self):
        from portal.agent import changeset as cs
        actor = _user(email="u5@pelda.hu")
        farm = _farm(actor, name="F5")
        _grant(farm, actor)
        with self.assertRaises(Exception):
            cs.prepare_changeset(
                actor, farm, [{"target": "action", "op": "create",
                               "fields": {"text": "x"}}],
                run=None, idempotency_key="p6")


class ApproveTests(TestCase):
    def test_approve_token_creates_action_verified(self):
        from portal.agent import changeset as cs
        actor = _user(email="a1@pelda.hu")
        farm = _farm(actor, name="A1")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        cmds = [{"target": "action", "op": "create",
                 "fields": {"text": "Ontozesi terv"}}]
        obj, token = _prep(actor, farm, cmds, run=run, key="a1")
        receipt = cs.approve_changeset(
            actor, farm, obj.pk, token, obj.payload_hash)
        self.assertTrue(receipt["verified"])
        self.assertEqual(FarmAction.objects.filter(farm=farm).count(), 1)
        line = AgentChangeset.objects.get(pk=obj.pk)
        self.assertEqual(line.state, "verified")
        # Run findings preserved, run still done at stage 7.
        run.refresh_from_db()
        self.assertEqual(run.stage, 7)
        self.assertEqual(run.critic_findings, [])

    def test_approve_wrong_actor_rejected(self):
        from portal.agent import changeset as cs
        actor = _user(email="a2@pelda.hu")
        farm = _farm(actor, name="A2")
        _grant(farm, actor)
        other = _user(email="a2b@pelda.hu")
        Membership.objects.create(farm=farm, user=other, role="manager")
        run = _verified_run(farm, actor)
        obj, token = _prep(actor, farm, [
            {"target": "action", "op": "create",
             "fields": {"text": "x"}}], run=run, key="a2")
        with self.assertRaises(Exception):
            cs.approve_changeset(
                other, farm, obj.pk, token, obj.payload_hash)
        self.assertEqual(FarmAction.objects.filter(farm=farm).count(), 0)

    def test_crossfarm_delete_protected(self):
        actor = _user(email="a3@pelda.hu")
        farm = _farm(actor, name="A3")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        outsider = _user(email="a3o@pelda.hu")
        ofarm = _farm(outsider, name="A3o")
        _grant(ofarm, outsider)
        eq = Equipment.objects.create(
            farm=ofarm, name="Traktor", capabilities=["tillage"],
            available=True, hours_per_day="8.0")
        with self.assertRaises(Exception):
            _prep(actor, farm, [
                {"target": "equipment", "op": "delete", "id": eq.pk}],
                run=run, key="a3")
        self.assertTrue(Equipment.objects.filter(pk=eq.pk).exists())

    def test_double_confirmation_returns_receipt_no_write(self):
        from portal.agent import changeset as cs
        actor = _user(email="a4@pelda.hu")
        farm = _farm(actor, name="A4")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        obj, token = _prep(actor, farm, [
            {"target": "action", "op": "create",
             "fields": {"text": "Ketszer"}}], run=run, key="a4")
        first = cs.approve_changeset(
            actor, farm, obj.pk, token, obj.payload_hash)
        n_actions = FarmAction.objects.filter(farm=farm).count()
        n_audits = farm.audit_events.count()
        second = cs.approve_changeset(
            actor, farm, obj.pk, token, obj.payload_hash)
        self.assertEqual(first, second)
        self.assertEqual(FarmAction.objects.filter(farm=farm).count(),
                         n_actions)
        self.assertEqual(farm.audit_events.count(), n_audits)

    def test_stale_object_rolls_back(self):
        from portal.agent import changeset as cs
        actor = _user(email="a5@pelda.hu")
        farm = _farm(actor, name="A5")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        obj, token = _prep(actor, farm, [
            {"target": "action", "op": "create",
             "fields": {"text": "Stale"}}], run=run, key="a5")
        # Concurrent write after preview invalidates the snapshot.
        FarmAction.objects.create(farm=farm, text="Versenyzo",
                                  status="open")
        with self.assertRaises(Exception):
            cs.approve_changeset(
                actor, farm, obj.pk, token, obj.payload_hash)
        self.assertFalse(FarmAction.objects.filter(
            text="Stale").exists())
        self.assertEqual(
            AgentChangeset.objects.get(pk=obj.pk).state, "preview")

    def test_revoke_consent_blocks_apply(self):
        from portal.agent import changeset as cs
        actor = _user(email="a6@pelda.hu")
        farm = _farm(actor, name="A6")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        obj, token = _prep(actor, farm, [
            {"target": "action", "op": "create",
             "fields": {"text": "Consent"}}], run=run, key="a6")
        _revoke(farm, actor)
        with self.assertRaises(Exception):
            cs.approve_changeset(
                actor, farm, obj.pk, token, obj.payload_hash)
        self.assertEqual(FarmAction.objects.filter(farm=farm).count(), 0)

    def test_worker_cannot_prepare_equipment(self):
        actor = _user(email="a7@pelda.hu")
        farm = _farm(actor, name="A7")
        _grant(farm, actor)
        worker = _user(email="a7w@pelda.hu")
        Membership.objects.create(farm=farm, user=worker, role="worker")
        run = _verified_run(farm, actor)
        with self.assertRaises(Exception):
            _prep(worker, farm, [
                {"target": "equipment", "op": "create",
                 "fields": {"name": "Gep"}}], run=run, key="a7")

    def test_reject_changeset(self):
        from portal.agent import changeset as cs
        actor = _user(email="a8@pelda.hu")
        farm = _farm(actor, name="A8")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        obj, token = _prep(actor, farm, [
            {"target": "action", "op": "create",
             "fields": {"text": "Elvetve"}}], run=run, key="a8")
        cs.reject_changeset(actor, farm, obj.pk)
        self.assertEqual(
            AgentChangeset.objects.get(pk=obj.pk).state, "rejected")
        with self.assertRaises(Exception):
            cs.approve_changeset(
                actor, farm, obj.pk, token, obj.payload_hash)
        self.assertEqual(FarmAction.objects.filter(farm=farm).count(), 0)


class EquipmentCrudTests(TestCase):
    def test_equipment_full_crud(self):
        from portal.agent import changeset as cs
        actor = _user(email="e1@pelda.hu")
        farm = _farm(actor, name="E1")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        obj, token = _prep(actor, farm, [
            {"target": "equipment", "op": "create",
             "fields": {"name": "Vetogep", "capabilities": ["sowing"]}}],
            run=run, key="e1")
        receipt = cs.approve_changeset(
            actor, farm, obj.pk, token, obj.payload_hash)
        self.assertTrue(receipt["verified"])
        eq = Equipment.objects.get(farm=farm, name="Vetogep")
        obj2, token2 = _prep(actor, farm, [
            {"target": "equipment", "op": "update", "id": eq.pk,
             "fields": {"name": "Vetogep 2"}}], run=run, key="e2")
        cs.approve_changeset(
            actor, farm, obj2.pk, token2, obj2.payload_hash)
        eq.refresh_from_db()
        self.assertEqual(eq.name, "Vetogep 2")
        obj3, token3 = _prep(actor, farm, [
            {"target": "equipment", "op": "delete", "id": eq.pk}],
            run=run, key="e3")
        cs.approve_changeset(
            actor, farm, obj3.pk, token3, obj3.payload_hash)
        self.assertFalse(Equipment.objects.filter(pk=eq.pk).exists())

    def test_equipment_bad_capability_rejected(self):
        actor = _user(email="e2@pelda.hu")
        farm = _farm(actor, name="E2")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        with self.assertRaises(Exception):
            _prep(actor, farm, [
                {"target": "equipment", "op": "create",
                 "fields": {"name": "Rossz", "capabilities": ["warp"]}}],
                run=run, key="e4")

    def test_equipment_crossfarm_property_rejected(self):
        actor = _user(email="e3@pelda.hu")
        farm = _farm(actor, name="E3")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        outsider = _user(email="e3o@pelda.hu")
        ofarm = _farm(outsider, name="E3o")
        _grant(ofarm, outsider)
        prop = _property(ofarm)
        with self.assertRaises(Exception):
            _prep(actor, farm, [
                {"target": "equipment", "op": "create",
                 "fields": {"name": "Gep", "properties": [prop.pk]}}],
                run=run, key="e5")

    def test_equipment_delete_keeps_work_item_set_null(self):
        from portal.agent import changeset as cs
        actor = _user(email="e4@pelda.hu")
        farm = _farm(actor, name="E4")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        prop = _property(farm)
        cyc = _cycle(farm, prop)
        eq = Equipment.objects.create(
            farm=farm, name="Traktor", capabilities=["tillage"],
            available=True, hours_per_day="8.0")
        wi = WorkItem.objects.create(
            farm=farm, crop_cycle=cyc, property=prop, title="Szantas",
            capability="tillage", start_date="2026-04-01",
            end_date="2026-04-02", estimated_hours="2.0", equipment=eq)
        obj, token = _prep(actor, farm, [
            {"target": "equipment", "op": "delete", "id": eq.pk}],
            run=run, key="e6")
        cs.approve_changeset(actor, farm, obj.pk, token, obj.payload_hash)
        self.assertFalse(Equipment.objects.filter(pk=eq.pk).exists())
        wi.refresh_from_db()
        self.assertIsNone(wi.equipment)


class ActionCrudTests(TestCase):
    def test_action_update_and_delete(self):
        from portal.agent import changeset as cs
        actor = _user(email="c1@pelda.hu")
        farm = _farm(actor, name="C1")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        base = FarmAction.objects.create(
            farm=farm, text="Regi", status="open")
        obj, token = _prep(actor, farm, [
            {"target": "action", "op": "update", "id": base.pk,
             "fields": {"text": "Uj"}}], run=run, key="c1")
        receipt = cs.approve_changeset(
            actor, farm, obj.pk, token, obj.payload_hash)
        self.assertTrue(receipt["verified"])
        base.refresh_from_db()
        self.assertEqual(base.text, "Uj")
        obj2, token2 = _prep(actor, farm, [
            {"target": "action", "op": "delete", "id": base.pk}],
            run=run, key="c2")
        cs.approve_changeset(
            actor, farm, obj2.pk, token2, obj2.payload_hash)
        self.assertFalse(FarmAction.objects.filter(pk=base.pk).exists())

    def test_action_empty_text_rejected(self):
        actor = _user(email="c2@pelda.hu")
        farm = _farm(actor, name="C2")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        with self.assertRaises(Exception):
            _prep(actor, farm, [
                {"target": "action", "op": "create",
                 "fields": {"text": "  "}}], run=run, key="c3")


class WorkItemTests(TestCase):
    def test_work_item_create_positive(self):
        from portal.agent import changeset as cs
        actor = _user(email="w1@pelda.hu")
        farm = _farm(actor, name="W1")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        prop = _property(farm)
        cyc = _cycle(farm, prop)
        obj, token = _prep(actor, farm, [
            {"target": "work_item", "op": "create",
             "fields": {"title": "Vetes", "capability": "sowing",
                        "start_date": "2026-04-01",
                        "end_date": "2026-04-03",
                        "estimated_hours": "4.0",
                        "property": prop.pk, "crop_cycle": cyc.pk}}],
            run=run, key="w1")
        receipt = cs.approve_changeset(
            actor, farm, obj.pk, token, obj.payload_hash)
        self.assertTrue(receipt["verified"])
        self.assertEqual(WorkItem.objects.filter(farm=farm).count(), 1)

    def test_work_item_done_history_blocked(self):
        actor = _user(email="w2@pelda.hu")
        farm = _farm(actor, name="W2")
        _grant(farm, actor)
        run = _verified_run(farm, actor)
        prop = _property(farm)
        cyc = _cycle(farm, prop)
        wi = WorkItem.objects.create(
            farm=farm, crop_cycle=cyc, property=prop, title="Regi",
            capability="sowing", start_date="2026-04-01",
            end_date="2026-04-02", estimated_hours="1.0")
        WorkItem.objects.filter(pk=wi.pk).update(status="done")
        with self.assertRaises(Exception):
            _prep(actor, farm, [
                {"target": "work_item", "op": "update", "id": wi.pk,
                 "fields": {"title": "Mas"}}], run=run, key="w2")
        with self.assertRaises(Exception):
            _prep(actor, farm, [
                {"target": "work_item", "op": "delete", "id": wi.pk}],
                run=run, key="w3")
        wi.refresh_from_db()
        self.assertEqual(wi.title, "Regi")
