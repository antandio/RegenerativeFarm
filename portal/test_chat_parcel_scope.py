"""Task 3 slice 3a: selected-parcel validation, scoped context, snapshot.

Counting fakes / MockTransport only; no paid provider calls.
"""
import hashlib
import json
import time
from datetime import date

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from portal.chat import (
    ChatRejected,
    MetaChatClient,
    build_chat_context,
    confirm_proposal,
    execute_chat_attempt,
    request_chat,
    validate_selected_property_ids,
    _reserve_chat_attempt,
)
from portal.chat_models import ChatAttempt, ChatProposal
from portal.domain_models import (
    CropCycle,
    Equipment,
    EquipmentProperty,
    FarmLocation,
    FarmProperty,
    LivestockGroup,
    WorkItem,
)
from portal.models import AuditEvent, ConsentEvent, Farm, FarmAction, Membership
from portal.quota import usage_counts

User = get_user_model()


def make_user(email):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


def good_payload(answer="Scoped valasz."):
    return {"answer": answer, "actions": [], "links": [],
            "_actual_model": "fake-model", "_usage_input_tokens": 3,
            "_usage_output_tokens": 4}


class CountingFake:
    def __init__(self, payload=None):
        self.payload = payload or good_payload()
        self.calls = 0
        self.contexts = []

    def generate_chat(self, message, context, language=None):
        self.calls += 1
        self.contexts.append(context)
        return dict(self.payload)


def seed_weather(farm_id, loc, tag):
    from portal.weather import _ck
    cache.set(_ck("forecast", farm_id, loc),
              {"payload": {"current": {"temperature": 20, "tag": tag}, "daily": [],
                           "source": "Open-Meteo",
                           "fetched_at": "2026-01-01T00:00:00+00:00"},
               "stored_at": time.time()}, 24 * 3600)
    cache.set(_ck("climate", farm_id, loc),
              {"payload": {"period": "2015-2024", "monthly": [], "tag": tag,
                           "fetched_at": "2026-01-01T00:00:00+00:00"},
               "stored_at": time.time()}, 24 * 3600)


class ParcelFixture(TestCase):
    def setUp(self):
        self.u = make_user("scope@pelda.hu")
        self.farm = Farm.objects.create(
            owner=self.u, name="ScopeFarm",
            crops_livestock_notes="SECRET_WHOLE_FARM_CROPS",
            soil_notes="SECRET_SOIL")
        grant(self.farm, self.u)
        self.loc_a = FarmLocation.objects.create(
            farm=self.farm, name="LocAlpha", latitude="46.250000",
            longitude="20.140000", timezone="Europe/Budapest")
        self.loc_b = FarmLocation.objects.create(
            farm=self.farm, name="LocBeta", latitude="46.260000",
            longitude="20.150000", timezone="Europe/Budapest")
        self.loc_c = FarmLocation.objects.create(
            farm=self.farm, name="LocGamma", latitude="46.270000",
            longitude="20.160000", timezone="Europe/Budapest")
        self.prop_a = FarmProperty.objects.create(
            farm=self.farm, location=self.loc_a, name="ParcelAlpha",
            kind="field", area_hectares="5.00")
        self.prop_b = FarmProperty.objects.create(
            farm=self.farm, location=self.loc_b, name="ParcelBeta",
            kind="field", area_hectares="3.00")
        self.prop_c = FarmProperty.objects.create(
            farm=self.farm, location=self.loc_c, name="ParcelGamma",
            kind="orchard", area_hectares="2.00")
        # Foreign farm parcel.
        self.other_u = make_user("scope-other@pelda.hu")
        self.farm2 = Farm.objects.create(owner=self.other_u, name="OtherFarm")
        grant(self.farm2, self.other_u)
        self.loc_x = FarmLocation.objects.create(
            farm=self.farm2, name="LocForeign", latitude="47.000000",
            longitude="19.000000", timezone="Europe/Budapest")
        self.prop_x = FarmProperty.objects.create(
            farm=self.farm2, location=self.loc_x, name="ParcelForeign",
            kind="field", area_hectares="9.00")


class ValidationTests(ParcelFixture):
    def test_valid_sorted_deduped_and_empty_is_whole(self):
        self.assertEqual(
            validate_selected_property_ids(
                self.farm, [self.prop_b.id, str(self.prop_a.id), self.prop_b.id]),
            sorted([self.prop_a.id, self.prop_b.id]))
        self.assertEqual(validate_selected_property_ids(self.farm, []), [])
        self.assertEqual(validate_selected_property_ids(self.farm, None), [])
        self.assertEqual(validate_selected_property_ids(self.farm, ""), [])

    def test_malformed_rejected_invalid_input(self):
        bads = [
            ["x"], ["1.0"], ["-3"], [-3], [0], ["0"], [True], [1.0],
            [None], [""], ["  "], ["+1"], ["1 "[:0] + "a1"],
        ]
        for raw in bads:
            with self.assertRaises(ChatRejected, msg=repr(raw)) as cm:
                validate_selected_property_ids(self.farm, raw)
            self.assertEqual(cm.exception.code, "invalid_input", msg=repr(raw))

    def test_dup_after_int_conversion_rejected(self):
        with self.assertRaises(ChatRejected) as cm:
            validate_selected_property_ids(
                self.farm, ["0" + str(self.prop_a.id), str(self.prop_a.id)])
        self.assertEqual(cm.exception.code, "invalid_input")

    def test_foreign_and_nonexistent_generic_invalid_input(self):
        for pid in (self.prop_x.id, 99999999):
            with self.assertRaises(ChatRejected) as cm:
                validate_selected_property_ids(self.farm, [pid])
            self.assertEqual(cm.exception.code, "invalid_input")

    def test_raw_cap_before_db(self):
        raw = [str(self.prop_a.id)] * 501
        with self.assertRaises(ChatRejected) as cm:
            validate_selected_property_ids(self.farm, raw)
        self.assertEqual(cm.exception.code, "invalid_input")
        # Exactly at cap with duplicates dedupes without error.
        self.assertEqual(
            validate_selected_property_ids(self.farm, [str(self.prop_a.id)] * 500),
            [self.prop_a.id])


class ContextTests(ParcelFixture):
    def setUp(self):
        super().setUp()
        self.eq_shared = Equipment.objects.create(
            farm=self.farm, name="SharedTractor", capabilities=["tillage"])
        EquipmentProperty.objects.create(equipment=self.eq_shared, property=self.prop_a)
        EquipmentProperty.objects.create(equipment=self.eq_shared, property=self.prop_b)
        self.eq_b_only = Equipment.objects.create(
            farm=self.farm, name="BetaOnlyTractor", capabilities=["sowing"])
        EquipmentProperty.objects.create(equipment=self.eq_b_only, property=self.prop_b)
        self.cyc_a = CropCycle.objects.create(
            farm=self.farm, property=self.prop_a, crop_name="CropAlpha", state="current")
        self.cyc_b = CropCycle.objects.create(
            farm=self.farm, property=self.prop_b, crop_name="CropBeta", state="current")
        WorkItem.objects.create(
            farm=self.farm, crop_cycle=self.cyc_a, property=self.prop_a,
            title="WorkAlpha", capability="tillage",
            start_date=date(2026, 3, 1), end_date=date(2026, 3, 5))
        WorkItem.objects.create(
            farm=self.farm, crop_cycle=self.cyc_b, property=self.prop_b,
            title="WorkBeta", capability="tillage",
            start_date=date(2026, 3, 1), end_date=date(2026, 3, 5))
        grp = LivestockGroup.objects.create(
            farm=self.farm, name="FlockAlpha", species="sheep", headcount=3,
            housing_property=self.prop_a)
        WorkItem.objects.create(
            farm=self.farm, livestock_group=grp, livestock_kind="grazing",
            title="LivestockWorkAlpha", capability="other",
            start_date=date(2026, 3, 1), end_date=date(2026, 3, 5))
        FarmAction.objects.create(farm=self.farm, text="OpenActionAlpha", status="open")
        seed_weather(self.farm.id, self.loc_a, "WXALPHA")
        seed_weather(self.farm.id, self.loc_b, "WXBETA")

    def test_subset_context_only_selected_parcel(self):
        ctx, s = build_chat_context(self.farm, selected_property_ids=[self.prop_a.id])
        self.assertEqual(ctx["scope"],
                         {"mode": "subset", "selected_property_ids": [self.prop_a.id]})
        self.assertEqual(ctx["profile"], {"name": "ScopeFarm"})
        self.assertEqual([p["name"] for p in ctx["properties"]], ["ParcelAlpha"])
        self.assertEqual([lo["name"] for lo in ctx["locations"]], ["LocAlpha"])
        self.assertEqual(ctx["open_actions"], [])
        for needle in ("ParcelAlpha", "CropAlpha", "WorkAlpha", "SharedTractor"):
            self.assertIn(needle, s)
        for banned in ("ParcelBeta", "ParcelGamma", "CropBeta", "WorkBeta",
                       "BetaOnlyTractor", "LocBeta", "LocGamma",
                       "LivestockWorkAlpha", "OpenActionAlpha",
                       "SECRET_WHOLE_FARM_CROPS", "SECRET_SOIL", "WXBETA"):
            self.assertNotIn(banned, s, msg=banned)
        shared = [e for e in ctx["equipment"] if e["name"] == "SharedTractor"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["assigned_property_ids"], [self.prop_a.id])
        self.assertEqual([c["crop_name"] for c in ctx["crop_cycles"]], ["CropAlpha"])
        self.assertEqual([w["title"] for w in ctx["work_items"]], ["WorkAlpha"])
        self.assertEqual([w["location_id"] for w in ctx["weather"]], [self.loc_a.id])

    def test_subset_context_foreign_id_rejected_not_partial(self):
        with self.assertRaises(ChatRejected) as cm:
            build_chat_context(self.farm,
                               selected_property_ids=[self.prop_a.id, self.prop_x.id])
        self.assertEqual(cm.exception.code, "invalid_input")

    def test_empty_and_none_whole_farm_backcompat(self):
        for sel in (None, []):
            ctx, s = build_chat_context(self.farm, selected_property_ids=sel)
            self.assertNotIn("scope", ctx)
            self.assertIn("SECRET_WHOLE_FARM_CROPS", s)
            self.assertEqual({p["name"] for p in ctx["properties"]},
                             {"ParcelAlpha", "ParcelBeta", "ParcelGamma"})
            self.assertTrue(ctx["open_actions"])


class SnapshotAndServiceTests(ParcelFixture):
    def test_request_chat_subset_snapshot_and_trusted_context(self):
        fake = CountingFake()
        run = request_chat(
            self.u, self.farm, "mit tegyek?", "scope-1", language="hu",
            client=fake, context_str="CALLER_WHOLE_FARM_LEAK",
            selected_property_ids=[self.prop_b.id, self.prop_a.id])
        self.assertEqual(run.status, "success")
        self.assertEqual(fake.calls, 1)
        ctx_sent = fake.contexts[0]
        self.assertNotIn("CALLER_WHOLE_FARM_LEAK", ctx_sent)
        self.assertIn("ParcelAlpha", ctx_sent)
        self.assertIn("ParcelBeta", ctx_sent)
        self.assertNotIn("ParcelGamma", ctx_sent)
        self.assertNotIn("SECRET_WHOLE_FARM_CROPS", ctx_sent)
        snap = run.selection_snapshot
        self.assertEqual(snap["mode"], "subset")
        self.assertEqual(snap["property_ids"],
                         sorted([self.prop_a.id, self.prop_b.id]))
        by_id = {p["id"]: p for p in snap["properties"]}
        self.assertEqual(by_id[self.prop_a.id]["name"], "ParcelAlpha")
        self.assertEqual(by_id[self.prop_a.id]["kind"], "field")
        self.assertEqual(by_id[self.prop_a.id]["area_hectares"], "5.00")
        self.assertEqual(by_id[self.prop_a.id]["location_id"], self.loc_a.id)
        canonical = json.dumps(
            {"mode": "subset", "property_ids": snap["property_ids"],
             "properties": snap["properties"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(snap["sha256"],
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def test_request_chat_empty_is_whole_snapshot(self):
        fake = CountingFake()
        run = request_chat(self.u, self.farm, "kerdes", "scope-whole",
                           language="hu", client=fake)
        self.assertEqual(run.status, "success")
        self.assertEqual(run.selection_snapshot, {"mode": "all", "property_ids": []})
        self.assertIn("ParcelGamma", fake.contexts[0])

    def test_invalid_selection_rejected_before_quota_and_provider(self):
        before = usage_counts(self.farm)
        fake = CountingFake()
        with self.assertRaises(ChatRejected) as cm:
            request_chat(self.u, self.farm, "kerdes", "scope-bad",
                         language="hu", client=fake,
                         selected_property_ids=[self.prop_x.id])
        self.assertEqual(cm.exception.code, "invalid_input")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(ChatAttempt.objects.filter(farm=self.farm).count(), 0)
        self.assertEqual(usage_counts(self.farm), before)

    def test_idempotency_changed_scope_rejected_no_second_call(self):
        fake = CountingFake()
        run = request_chat(self.u, self.farm, "kerdes", "scope-idem",
                           language="hu", client=fake,
                           selected_property_ids=[self.prop_a.id])
        self.assertEqual(run.status, "success")
        with self.assertRaises(ChatRejected) as cm:
            request_chat(self.u, self.farm, "kerdes", "scope-idem",
                         language="hu", client=fake,
                         selected_property_ids=[self.prop_b.id])
        self.assertEqual(cm.exception.code, "invalid_input")
        self.assertEqual(fake.calls, 1)
        self.assertEqual(ChatAttempt.objects.filter(farm=self.farm).count(), 1)
        # Same scope replay returns existing without a second call.
        again = request_chat(self.u, self.farm, "kerdes", "scope-idem",
                             language="hu", client=fake,
                             selected_property_ids=[self.prop_a.id])
        self.assertEqual(again.pk, run.pk)
        self.assertEqual(fake.calls, 1)

    def test_snapshot_immutable_on_existing_row(self):
        fake = CountingFake()
        run = request_chat(self.u, self.farm, "kerdes", "scope-immut",
                           language="hu", client=fake,
                           selected_property_ids=[self.prop_a.id])
        run.selection_snapshot = {"mode": "all", "property_ids": []}
        with self.assertRaises((ValueError, ChatRejected)):
            run.save()
        fresh = ChatAttempt.objects.get(pk=run.pk)
        self.assertEqual(fresh.selection_snapshot["mode"], "subset")
        # Unchanged save (as execute_chat_attempt performs) still works.
        fresh.answer_text = "ok"
        fresh.save()
        self.assertEqual(ChatAttempt.objects.get(pk=run.pk).answer_text, "ok")

    def test_mock_transport_real_client_scoped_context(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json={
                "model": "fake-model",
                "choices": [{"message": {"content": json.dumps(
                    {"answer": "ok", "actions": [], "links": []})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

        client = MetaChatClient(api_key="test-key", transport=httpx.MockTransport(handler))
        run = request_chat(self.u, self.farm, "kerdes", "scope-mock",
                           language="hu", client=client,
                           context_str="CALLER_LEAK",
                           selected_property_ids=[self.prop_a.id])
        self.assertEqual(run.status, "success")
        self.assertIn("ParcelAlpha", seen["body"])
        self.assertNotIn("ParcelBeta", seen["body"])
        self.assertNotIn("CALLER_LEAK", seen["body"])


def malicious_payload(toggle_id):
    return {"answer": "Scoped valasz.", "_actual_model": "fake-model",
            "_usage_input_tokens": 1, "_usage_output_tokens": 1,
            "actions": [
                {"kind": "action_create", "text": "INJECTED_CREATE"},
                {"kind": "action_toggle", "action_id": toggle_id},
                {"kind": "plans_generate"}],
            "links": ["location_list"]}


class ProposalScopeTests(ParcelFixture):
    def _subset_run(self, key="scope-mal-1"):
        self.open_action = FarmAction.objects.create(
            farm=self.farm, text="OpenOwn", status="open")
        fake = CountingFake(payload=malicious_payload(self.open_action.id))
        run = request_chat(self.u, self.farm, "kerdes", key, language="hu",
                           client=fake, selected_property_ids=[self.prop_a.id])
        self.assertEqual(run.status, "success")
        return run

    def test_subset_malicious_model_drops_all_global_kinds_keeps_navigate(self):
        run = self._subset_run()
        kinds = [p.kind for p in ChatProposal.objects.filter(attempt=run).order_by("position")]
        self.assertEqual(kinds, ["navigate"])
        self.assertEqual(
            {p.payload_link for p in ChatProposal.objects.filter(attempt=run)},
            {"location_list"})
        # Dropped kinds never mutated anything.
        self.assertFalse(FarmAction.objects.filter(text="INJECTED_CREATE").exists())
        self.open_action.refresh_from_db()
        self.assertEqual(self.open_action.status, "open")
        self.assertEqual(WorkItem.objects.filter(farm=self.farm).count(), 0)

    def test_subset_navigate_confirm_fresh_succeeds_no_domain_write(self):
        run = self._subset_run("scope-nav-ok")
        prop = ChatProposal.objects.get(attempt=run, payload_link="location_list")
        audits = AuditEvent.objects.filter(farm=self.farm).count()
        confirm_proposal(self.u, self.farm, prop.id)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "done")
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits + 1)
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 1)  # only pre-seeded

    def test_subset_stored_malicious_global_proposal_confirm_refused(self):
        # Stored directly, bypassing execute filtering (legacy/injected row).
        run = self._subset_run("scope-stored-1")
        evil = ChatProposal.objects.create(
            attempt=run, farm=self.farm, position=90, kind="action_create",
            payload_text="STORED_EVIL", status="pending")
        audits = AuditEvent.objects.filter(farm=self.farm).count()
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(self.u, self.farm, evil.id)
        self.assertEqual(cm.exception.code, "invalid_input")
        evil.refresh_from_db()
        self.assertEqual(evil.status, "pending")
        self.assertFalse(FarmAction.objects.filter(text="STORED_EVIL").exists())
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits)

    def test_subset_stale_metadata_fields_refuse_navigate_no_mutation(self):
        run = self._subset_run("scope-stale-1")
        prop = ChatProposal.objects.get(attempt=run, payload_link="location_list")
        orig = (self.prop_a.name, self.prop_a.kind,
                str(self.prop_a.area_hectares), self.prop_a.location_id)
        mutations = [
            {"name": "ParcelAlphaRenamed"},
            {"kind": "orchard"},
            {"area_hectares": "9.99"},
            {"location_id": self.loc_b.id},
        ]
        for mut in mutations:
            FarmProperty.objects.filter(pk=self.prop_a.pk).update(**mut)
            audits = AuditEvent.objects.filter(farm=self.farm).count()
            with self.assertRaises(ChatRejected, msg=repr(mut)) as cm:
                confirm_proposal(self.u, self.farm, prop.id)
            self.assertEqual(cm.exception.code, "invalid_input", msg=repr(mut))
            prop.refresh_from_db()
            self.assertEqual(prop.status, "pending", msg=repr(mut))
            self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits)
            FarmProperty.objects.filter(pk=self.prop_a.pk).update(
                name=orig[0], kind=orig[1], area_hectares=orig[2], location_id=orig[3])
        # Restored metadata confirms cleanly.
        confirm_proposal(self.u, self.farm, prop.id)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "done")

    def test_subset_deleted_parcel_refuses_navigate_no_mutation(self):
        run = self._subset_run("scope-stale-del")
        prop = ChatProposal.objects.get(attempt=run, payload_link="location_list")
        audits = AuditEvent.objects.filter(farm=self.farm).count()
        FarmProperty.objects.filter(pk=self.prop_a.pk).delete()
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(self.u, self.farm, prop.id)
        self.assertEqual(cm.exception.code, "invalid_input")
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits)

    def test_subset_tampered_snapshot_hash_refuses(self):
        run = self._subset_run("scope-stale-hash")
        prop = ChatProposal.objects.get(attempt=run, payload_link="location_list")
        ChatAttempt.objects.filter(pk=run.pk).update(
            selection_snapshot={**run.selection_snapshot, "sha256": "0" * 64})
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(self.u, self.farm, prop.id)
        self.assertEqual(cm.exception.code, "invalid_input")
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")

    def test_confirm_attempt_farm_mismatch_refused(self):
        run = self._subset_run("scope-mismatch")
        nav = ChatProposal.objects.get(attempt=run, payload_link="location_list")
        # Proposal claims this farm but its attempt belongs to another farm.
        other_run = ChatAttempt.objects.create(
            farm=self.farm2, initiated_by=self.other_u, idempotency_key="mm-1",
            language="hu", status="success", source="live", requested_model="m",
            user_message="x", answer_text="ok")
        evil = ChatProposal.objects.create(
            attempt=other_run, farm=self.farm, position=91, kind="navigate",
            payload_link="location_list", status="pending")
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(self.u, self.farm, evil.id)
        self.assertEqual(cm.exception.code, "invalid_input")
        evil.refresh_from_db()
        self.assertEqual(evil.status, "pending")
        self.assertEqual(nav.status, "pending")

    def test_subset_confirm_consent_and_role_refreshed(self):
        run = self._subset_run("scope-refresh")
        prop = ChatProposal.objects.get(attempt=run, payload_link="location_list")
        ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="revoked")
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(self.u, self.farm, prop.id)
        self.assertEqual(cm.exception.code, "consent_required")
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")
        grant(self.farm, self.u)
        viewer = make_user("scope-viewer@pelda.hu")
        Membership.objects.create(farm=self.farm, user=viewer, role="viewer", is_active=True)
        with self.assertRaises(ChatRejected) as cm:
            confirm_proposal(viewer, self.farm, prop.id)
        self.assertEqual(cm.exception.code, "forbidden")
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")

    def test_wholefarm_toggle_in_context_preserved_and_confirmable(self):
        target = FarmAction.objects.create(farm=self.farm, text="WholeOpen", status="open")
        fake = CountingFake(payload=malicious_payload(target.id))
        run = request_chat(self.u, self.farm, "kerdes", "whole-toggle-ok",
                           language="hu", client=fake)
        self.assertEqual(run.status, "success")
        kinds = [p.kind for p in ChatProposal.objects.filter(attempt=run).order_by("position")]
        self.assertIn("action_toggle", kinds)
        self.assertIn("action_create", kinds)
        toggle = ChatProposal.objects.get(attempt=run, kind="action_toggle")
        confirm_proposal(self.u, self.farm, toggle.id)
        target.refresh_from_db()
        self.assertEqual(target.status, "done")

    def test_wholefarm_toggle_not_in_actual_context_dropped_early(self):
        # Supplied context is not authoritative: a closed (non-open) own action
        # is not in the server-rebuilt trusted context, and a foreign id never is.
        closed = FarmAction.objects.create(farm=self.farm, text="ClosedCtx", status="done")
        foreign = FarmAction.objects.create(farm=self.farm2, text="ForeignAct", status="open")
        for label, tid in (("closed", closed.id), ("cross-farm", foreign.id)):
            run, created = _reserve_chat_attempt(
                self.u, self.farm, f"whole-drop-{label}", "kerdes", "hu")
            self.assertTrue(created)
            fake = CountingFake(payload=malicious_payload(tid))
            ctx = json.dumps({"open_actions": [{"id": tid}]})
            result = execute_chat_attempt(run, client=fake, context_str=ctx)
            self.assertEqual(result.status, "success", msg=label)
            self.assertEqual(
                ChatProposal.objects.filter(attempt=run, kind="action_toggle").count(),
                0, msg=label)

    def test_wholefarm_toggle_in_supplied_context_via_execute_kept(self):
        target = FarmAction.objects.create(farm=self.farm, text="InCtx", status="open")
        run, _ = _reserve_chat_attempt(self.u, self.farm, "whole-keep-1", "kerdes", "hu")
        fake = CountingFake(payload=malicious_payload(target.id))
        ctx = json.dumps({"open_actions": [{"id": target.id, "text": "InCtx"}]})
        execute_chat_attempt(run, client=fake, context_str=ctx)
        self.assertEqual(
            ChatProposal.objects.filter(attempt=run, kind="action_toggle").count(), 1)

    def test_idempotency_all_vs_subset_conflict_both_directions(self):
        fake = CountingFake()
        request_chat(self.u, self.farm, "kerdes", "idem-all-first", language="hu",
                     client=fake)
        with self.assertRaises(ChatRejected) as cm:
            request_chat(self.u, self.farm, "kerdes", "idem-all-first", language="hu",
                         client=fake, selected_property_ids=[self.prop_a.id])
        self.assertEqual(cm.exception.code, "invalid_input")
        request_chat(self.u, self.farm, "kerdes", "idem-sub-first", language="hu",
                     client=fake, selected_property_ids=[self.prop_a.id])
        with self.assertRaises(ChatRejected) as cm:
            request_chat(self.u, self.farm, "kerdes", "idem-sub-first", language="hu",
                         client=fake)
        self.assertEqual(cm.exception.code, "invalid_input")
        self.assertEqual(fake.calls, 2)
        self.assertNotIn("ParcelForeign", "".join(fake.contexts))

    def test_oversized_subset_context_rejected_before_quota(self):
        ids = []
        for i in range(40):
            p = FarmProperty.objects.create(
                farm=self.farm, location=self.loc_a,
                name=f"BigParcel{i:02d}" + "N" * 100, kind="field",
                area_hectares="1.00", notes="x" * 500)
            ids.append(p.id)
        before = usage_counts(self.farm)
        fake = CountingFake()
        with self.assertRaises(ChatRejected) as cm:
            request_chat(self.u, self.farm, "kerdes", "scope-too-big",
                         language="hu", client=fake, selected_property_ids=ids)
        self.assertEqual(cm.exception.code, "invalid_input")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(ChatAttempt.objects.filter(
            farm=self.farm, idempotency_key="scope-too-big").count(), 0)
        self.assertEqual(usage_counts(self.farm), before)


class ParcelPickerUiTests(ParcelFixture):
    """Task 3 UI slice: rendered picker, view validation, history snapshot."""

    def _client(self, user):
        from django.test import Client
        c = Client()
        c.force_login(user)
        return c

    def _form_html(self, content):
        import re
        m = re.search(r'<form[^>]*action="/chat/"[^>]*>(.*?)</form>', content, re.S)
        self.assertIsNotNone(m, msg="send form not found")
        return m.group(0)

    def test_get_picker_checkboxes_inside_send_form_with_kind_area(self):
        c = self._client(self.u)
        r = c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        form = self._form_html(html)
        self.assertIn('name="selected_property_ids"', form)
        self.assertIn(f'value="{self.prop_a.id}"', form)
        self.assertIn(f'value="{self.prop_b.id}"', form)
        self.assertIn(f'value="{self.prop_c.id}"', form)
        for needle in ("ParcelAlpha", "ParcelBeta", "ParcelGamma"):
            self.assertContains(r, needle)
        # Kind and area shown next to names.
        self.assertContains(r, "5,00")
        self.assertContains(r, "3,00")
        # Explicit empty means whole farm.
        self.assertContains(r, "egész gazdaság")
        # Compact quota/context summary retained.
        self.assertContains(r, "Keret")
        self.assertContains(r, "Környezet mérete")
        # Accessible selected count works without JS.
        self.assertContains(r, 'id="parcel-count"')
        # Scoped CSS with cache-busting, not global app.css edit.
        self.assertContains(r, "chat_parcels.css")

    def test_get_picker_english_labels(self):
        c = self._client(self.u)
        c.cookies["django_language"] = "en"
        r = c.get("/chat/", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Whole farm")
        self.assertContains(r, "Select parcels")

    def test_get_viewer_picker_disabled_no_submit(self):
        from portal.models import Membership
        viewer = make_user("scope-ui-viewer@pelda.hu")
        Membership.objects.create(farm=self.farm, user=viewer, role="viewer", is_active=True)
        c = self._client(viewer)
        c.post("/aktiv-farm/", {"farm_id": self.farm.id})
        r = c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode("utf-8")
        form = self._form_html(html)
        self.assertIn("disabled", form)
        self.assertNotIn('type="submit"', form)
        self.assertContains(r, "chat_parcels.css")

    def test_post_subset_renders_snapshot_names_and_subset_notice(self):
        from unittest.mock import patch
        c = self._client(self.u)
        fake = CountingFake()
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            r = c.post("/chat/", {"message": "mit tegyek Alpha es Beta?",
                                  "idempotency_key": "ui-subset-1",
                                  "cost_consent": "on", "language": "hu",
                                  "selected_property_ids": [str(self.prop_a.id), str(self.prop_b.id)]},
                       follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fake.calls, 1)
        run = ChatAttempt.objects.get(farm=self.farm, idempotency_key="ui-subset-1")
        self.assertEqual(run.selection_snapshot["mode"], "subset")
        # History shows attempt snapshot parcel names.
        self.assertContains(r, "ParcelAlpha")
        self.assertContains(r, "ParcelBeta")
        # Subset notice: only plan suggestions/navigation, no farm-global writes.
        self.assertContains(r, "szöveges tervjavaslat")
        self.assertNotContains(r, "végrehajtja a kiválasztott parcellákon")

    def test_post_foreign_selection_400_no_provider_preserves_input(self):
        from unittest.mock import patch
        from portal.quota import usage_counts
        c = self._client(self.u)
        before = usage_counts(self.farm)
        fake = CountingFake()
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            r = c.post("/chat/", {"message": "hello own choice",
                                  "idempotency_key": "ui-foreign-1",
                                  "cost_consent": "on", "language": "hu",
                                  "selected_property_ids": [str(self.prop_a.id), str(self.prop_x.id)]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(fake.calls, 0)
        self.assertFalse(ChatAttempt.objects.filter(farm=self.farm, idempotency_key="ui-foreign-1").exists())
        self.assertEqual(usage_counts(self.farm), before)
        # Message prefill preserved and own choice stays checked.
        self.assertContains(r, "hello own choice", status_code=400)
        html = r.content.decode("utf-8")
        self.assertIn(f'value="{self.prop_a.id}" checked', html)

    def test_post_malformed_selection_400_no_provider(self):
        from unittest.mock import patch
        from portal.quota import usage_counts
        c = self._client(self.u)
        before = usage_counts(self.farm)
        fake = CountingFake()
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            r = c.post("/chat/", {"message": "hello malformed",
                                  "idempotency_key": "ui-malformed-1",
                                  "cost_consent": "on", "language": "hu",
                                  "selected_property_ids": ["abc"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(fake.calls, 0)
        self.assertFalse(ChatAttempt.objects.filter(farm=self.farm, idempotency_key="ui-malformed-1").exists())
        self.assertEqual(usage_counts(self.farm), before)
        self.assertContains(r, "hello malformed", status_code=400)


class ReviewFixRedTests(ParcelFixture):
    """Strict RED assertions for the mandatory Task 3 review fixes."""

    def test_malformed_snapshot_execute_fails_closed_no_provider(self):
        target = FarmAction.objects.create(farm=self.farm, text="MalOpen", status="open")
        bad_snapshots = [
            {"mode": "all", "property_ids": [self.prop_a.id]},
            {"mode": "bogus", "property_ids": []},
            {"mode": "subset", "property_ids": "not-a-list"},
            {"mode": "subset", "property_ids": []},
        ]
        for i, snap in enumerate(bad_snapshots):
            run, created = _reserve_chat_attempt(
                self.u, self.farm, f"malformed-{i}", "kerdes", "hu")
            self.assertTrue(created)
            ChatAttempt.objects.filter(pk=run.pk).update(selection_snapshot=snap)
            fake = CountingFake(payload=malicious_payload(target.id))
            result = execute_chat_attempt(
                run, client=fake,
                context_str=json.dumps({"open_actions": [{"id": target.id}]}))
            result.refresh_from_db()
            self.assertEqual(fake.calls, 0, msg=repr(snap))
            self.assertEqual(result.status, "failed", msg=repr(snap))
            self.assertEqual(
                ChatProposal.objects.filter(attempt=run).count(), 0, msg=repr(snap))
            self.assertFalse(FarmAction.objects.filter(text="INJECTED_CREATE").exists())

    def test_wholefarm_toggle_not_in_rebuilt_trusted_context_dropped(self):
        closed = FarmAction.objects.create(farm=self.farm, text="ClosedAct", status="done")
        run, _ = _reserve_chat_attempt(self.u, self.farm, "trusted-closed-1", "kerdes", "hu")
        fake = CountingFake(payload=malicious_payload(closed.id))
        ctx = json.dumps({"open_actions": [{"id": closed.id, "text": "ClosedAct"}]})
        result = execute_chat_attempt(run, client=fake, context_str=ctx)
        self.assertEqual(result.status, "success")
        self.assertEqual(
            ChatProposal.objects.filter(attempt=run, kind="action_toggle").count(), 0)
        closed.refresh_from_db()
        self.assertEqual(closed.status, "done")

    def test_subset_snapshot_changed_after_reserve_fails_before_provider(self):
        run, _ = _reserve_chat_attempt(
            self.u, self.farm, "toctou-rename-1", "kerdes", "hu",
            selected_property_ids=[self.prop_a.id])
        FarmProperty.objects.filter(pk=self.prop_a.pk).update(name="ParcelAlphaRenamed")
        fake = CountingFake(payload=malicious_payload(1))
        result = execute_chat_attempt(run, client=fake, context_str="x")
        result.refresh_from_db()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(result.status, "failed")
        self.assertEqual(ChatProposal.objects.filter(attempt=run).count(), 0)

    def test_subset_parcel_reassigned_after_reserve_fails_before_provider(self):
        run, _ = _reserve_chat_attempt(
            self.u, self.farm, "toctou-reassign-1", "kerdes", "hu",
            selected_property_ids=[self.prop_a.id])
        FarmProperty.objects.filter(pk=self.prop_a.pk).update(farm_id=self.farm2.pk)
        fake = CountingFake(payload=malicious_payload(1))
        result = execute_chat_attempt(run, client=fake, context_str="x")
        result.refresh_from_db()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(result.status, "failed")
        self.assertEqual(ChatProposal.objects.filter(attempt=run).count(), 0)

    def test_request_chat_context_matches_snapshot_despite_change_before_reserve(self):
        from unittest.mock import patch as _patch
        import portal.chat as chat_mod
        original = chat_mod.build_chat_context
        state = {"calls": 0}

        def mutating_build(farm, selected_property_ids=None):
            out = original(farm, selected_property_ids=selected_property_ids)
            state["calls"] += 1
            if state["calls"] == 1:
                FarmProperty.objects.filter(pk=self.prop_a.pk).update(
                    name="RenamedBeforeReserve")
            return out

        fake = CountingFake()
        with _patch.object(chat_mod, "build_chat_context", mutating_build):
            run = request_chat(self.u, self.farm, "kerdes", "toctou-frozen-1",
                               language="hu", client=fake,
                               selected_property_ids=[self.prop_a.id])
        self.assertEqual(run.status, "success")
        self.assertIn("RenamedBeforeReserve", fake.contexts[0])
        snap_names = [p["name"] for p in run.selection_snapshot["properties"]]
        self.assertEqual(snap_names, ["RenamedBeforeReserve"])

    def test_work_items_each_nonnull_anchor_rule(self):
        cyc_a = CropCycle.objects.create(
            farm=self.farm, property=self.prop_a, crop_name="AnchorCropA", state="current")
        cyc_b = CropCycle.objects.create(
            farm=self.farm, property=self.prop_b, crop_name="AnchorCropB", state="current")
        # Legacy/direct rows may carry only one anchor; create validly, then
        # clear one anchor at queryset level (bypasses clean(), as legacy rows do).
        direct = WorkItem.objects.create(
            farm=self.farm, property=self.prop_a, crop_cycle=cyc_a, title="DirectOnly",
            capability="tillage", start_date=date(2026, 3, 1), end_date=date(2026, 3, 2))
        WorkItem.objects.filter(pk=direct.pk).update(crop_cycle_id=None)
        cycle_only = WorkItem.objects.create(
            farm=self.farm, property=self.prop_a, crop_cycle=cyc_a, title="CycleOnly",
            capability="tillage", start_date=date(2026, 3, 1), end_date=date(2026, 3, 2))
        WorkItem.objects.filter(pk=cycle_only.pk).update(property_id=None)
        WorkItem.objects.create(
            farm=self.farm, property=self.prop_a, crop_cycle=cyc_a, title="BothAnchors",
            capability="tillage", start_date=date(2026, 3, 1), end_date=date(2026, 3, 2))
        WorkItem.objects.create(
            farm=self.farm, property=self.prop_a, crop_cycle=cyc_b, title="CrossMismatch",
            capability="tillage", start_date=date(2026, 3, 1), end_date=date(2026, 3, 2))
        no_anchor = WorkItem.objects.create(
            farm=self.farm, property=self.prop_a, crop_cycle=cyc_a, title="NoAnchor",
            capability="tillage", start_date=date(2026, 3, 1), end_date=date(2026, 3, 2))
        WorkItem.objects.filter(pk=no_anchor.pk).update(property_id=None, crop_cycle_id=None)
        ctx, _s = build_chat_context(self.farm, selected_property_ids=[self.prop_a.id])
        titles = {w["title"] for w in ctx["work_items"]}
        self.assertEqual(titles, {"DirectOnly", "CycleOnly", "BothAnchors"})

    def test_post_oversized_raw_selection_error_path_batched_queries(self):
        from unittest.mock import patch
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from portal.quota import usage_counts
        from django.test import Client
        c = Client()
        c.force_login(self.u)
        before = usage_counts(self.farm)
        fake = CountingFake()
        raw = [str(self.prop_a.id)] + [str(900000 + i) for i in range(599)]
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            with CaptureQueriesContext(connection) as captured:
                r = c.post("/chat/", {"message": "cap path",
                                      "idempotency_key": "ui-cap-1",
                                      "cost_consent": "on", "language": "hu",
                                      "selected_property_ids": raw})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(usage_counts(self.farm), before)
        prop_queries = [q for q in captured.captured_queries
                        if "farmproperty" in q["sql"].lower()
                        or "domain_properties" in q["sql"].lower()
                        or "farm_property" in q["sql"].lower()]
        # Batched recovery + normal render queries only; never one query per raw id.
        self.assertLessEqual(len(prop_queries), 6, msg=str(len(prop_queries)))
        html = r.content.decode("utf-8")
        self.assertIn(f'value="{self.prop_a.id}" checked', html)

    def test_get_wholefarm_status_explicit_nojs_and_subset_wording(self):
        from django.test import Client
        c = Client()
        c.force_login(self.u)
        r = c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'role="status"')
        self.assertContains(r, "Egész gazdaság")
        self.assertContains(r, "data-whole-text")
        # HU locale decimal formatting is intentional user formatting.
        self.assertContains(r, "5,00")
        # Subset wording: textual plan suggestions allowed, no executable mutations.
        self.assertContains(r, "szöveges tervjavaslat")
        self.assertContains(r, "végrehajtható írási javaslat nem készül")
        self.assertNotContains(r, "csak tervjavaslatokat és navigációt ad")
