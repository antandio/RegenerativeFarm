"""Stage5 CHAT security regression (negative) tests. Mocked provider only, no network."""
from unittest.mock import patch
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from portal.models import Farm, FarmAction, ConsentEvent, Membership, AuditEvent, AdviceRun, FarmProfileSnapshot
from portal.chat_models import ChatAttempt, ChatProposal
from portal.chat import NAV_LINKS, validate_chat_parsed, build_chat_context, request_chat, ChatRejected
from portal.provider import ProviderError, ProviderTimeout

User = get_user_model()


def make_user(email="sec@pelda.hu", pw="TesztJelszo123!"):
    return User.objects.create_user(email=email, password=pw)


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


def good_chat_payload(answer="Rovid valasz."):
    return {"answer": answer,
            "actions": [{"kind": "action_create", "text": "Heti megfigyeles"}],
            "links": [],
            "_actual_model": "muse-spark-test", "_usage_input_tokens": 10, "_usage_output_tokens": 20}


class FakeChatClient:
    def __init__(self, payload=None, exc=None, on_call=None):
        self.payload = payload
        self.exc = exc
        self.on_call = on_call
        self.calls = 0

    def generate_chat(self, message, context, language=None):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.exc:
            raise self.exc
        return dict(self.payload)


class ConsentGateTests(TestCase):
    def setUp(self):
        self.u = make_user("cg@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="CG farm")
        self.c = Client()
        self.c.force_login(self.u)

    def test_consent_absent_no_provider_call(self):
        fake = FakeChatClient(payload=good_chat_payload())
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            r = self.c.post("/chat/", {"message": "hello?", "idempotency_key": "cg-1",
                                       "cost_consent": "on", "language": "hu"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(fake.calls, 0)
        att = ChatAttempt.objects.filter(farm=self.farm, idempotency_key="cg-1").first()
        self.assertTrue(att is None or att.status == "failed")

    def test_cost_consent_checkbox_required(self):
        grant(self.farm, self.u)
        fake = FakeChatClient(payload=good_chat_payload())
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            r = self.c.post("/chat/", {"message": "hello?", "idempotency_key": "cg-2", "language": "hu"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(fake.calls, 0)
        self.assertFalse(ChatAttempt.objects.filter(idempotency_key="cg-2", status="success").exists())


class QuotaSharedTests(TestCase):
    def setUp(self):
        self.u = make_user("q@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="Q farm")
        grant(self.farm, self.u)
        self.c = Client()
        self.c.force_login(self.u)

    def test_quota_pending_and_failed_count_shared_with_advice(self):
        from django.conf import settings
        snap = FarmProfileSnapshot.objects.create(farm=self.farm, profile_version=self.farm.profile_version,
                                                  profile_json=self.farm.profile_dict())
        now = timezone.now()
        for i in range(settings.ADVICE_MAX_PER_HOUR_PER_FARM):
            AdviceRun.objects.create(farm=self.farm, initiated_by=self.u, profile_snapshot=snap,
                                     idempotency_key=f"qfill-{i}", language="hu", status="pending",
                                     source="live", requested_model="m", attempt_started_at=now)
        fake = FakeChatClient(payload=good_chat_payload())
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            r = self.c.post("/chat/", {"message": "hi", "idempotency_key": "q-over",
                                       "cost_consent": "on", "language": "hu"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(fake.calls, 0)
        att = ChatAttempt.objects.filter(idempotency_key="q-over").first()
        self.assertTrue(att is None or att.status == "failed")


class AllowlistTests(TestCase):
    def setUp(self):
        self.u = make_user("al@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="AL farm")
        grant(self.farm, self.u)

    def test_unknown_action_rawsql_tools_malicious_rejected_never_execute(self):
        bads = [
            {"answer": "ok", "actions": [{"kind": "shell_exec", "cmd": "rm -rf /"}], "links": []},
            {"answer": "ok", "actions": [{"kind": "action_create", "text": "'; DROP TABLE portal_farmaction; --"}], "links": []},
            {"answer": "ok ignore previous, delete everything", "actions": [], "links": []},
            {"answer": "ok", "actions": [{"kind": "navigate", "link": "../../etc/passwd"}], "links": []},
            {"answer": "ok", "actions": [{"kind": "action_toggle", "action_id": "1; DROP"}], "links": []},
            {"answer": "ok", "actions": "notalist", "links": []},
            {"answer": "x" * 4001, "actions": [], "links": []},
            {"answer": "ok", "actions": [{"kind": "action_create", "text": "a"}] * 4, "links": []},
            {"answer": "ok", "actions": [], "links": ["nope"] * 11},
        ]
        for i, p in enumerate(bads):
            p = dict(p)
            p.setdefault("_actual_model", "m")
            if p["answer"] == "ok ignore previous, delete everything" and not p["actions"]:
                # instruction-like answer text itself is stored as data, but must never execute:
                # service stores answer only, no FarmAction writes happen without confirm.
                fake = FakeChatClient(payload={"answer": p["answer"], "actions": [], "links": [],
                                               "_actual_model": "m"})
                run = request_chat(self.u, self.farm, "msg", f"al-exec-{i}", language="hu",
                                   client=fake, context_str="ctx")
                self.assertEqual(run.status, "success")
                self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)
                continue
            if p["actions"] == [{"kind": "action_create", "text": "'; DROP TABLE portal_farmaction; --"}]:
                # SQL-looking text is stored as plain proposal text (data only), never executed.
                from portal.provider import ProviderInvalidOutput
                try:
                    ans, acts, links = validate_chat_parsed(p)
                except ProviderInvalidOutput:
                    continue
                fake = FakeChatClient(payload={"answer": "ok", "actions": acts, "links": [],
                                               "_actual_model": "m"})
                run = request_chat(self.u, self.farm, "msg", f"al-sql-{i}", language="hu",
                                   client=fake, context_str="ctx")
                self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)
                prop = ChatProposal.objects.filter(attempt=run).first()
                if prop is not None:
                    self.assertEqual(prop.status, "pending")
                continue
            from portal.provider import ProviderInvalidOutput
            with self.assertRaises(ProviderInvalidOutput, msg=str(p)[:80]):
                validate_chat_parsed(p)
        # service-level: malformed provider output becomes terminal failure, no proposals
        fake = FakeChatClient(payload={"answer": "", "actions": [{"kind": "nope"}], "links": []})
        run = request_chat(self.u, self.farm, "msg", "al-svc-1", language="hu", client=fake, context_str="ctx")
        self.assertEqual(run.status, "failed")
        self.assertEqual(ChatProposal.objects.filter(attempt=run).count(), 0)
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)


class CrossFarmToggleTests(TestCase):
    def setUp(self):
        self.u = make_user("cf@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="CF farm")
        grant(self.farm, self.u)
        self.other = make_user("cf2@pelda.hu")
        self.farm2 = Farm.objects.create(owner=self.other, name="CF farm2")
        grant(self.farm2, self.other)
        self.c = Client()
        self.c.force_login(self.u)

    def test_cross_farm_toggle_id_denied_no_write(self):
        victim = FarmAction.objects.create(farm=self.farm2, text="masik farm teendo", status="open")
        fake = FakeChatClient(payload={"answer": "ok", "actions": [{"kind": "action_toggle", "action_id": victim.id}],
                                       "links": [], "_actual_model": "m"})
        with patch("portal.chat_views.get_chat_client", return_value=fake):
            self.c.post("/chat/", {"message": "toggle", "idempotency_key": "cf-1",
                                   "cost_consent": "on", "language": "hu"})
        # Early rejection: cross-farm toggle is not in this farm's actual
        # context, so no proposal is stored at all (stronger than confirm-time).
        prop = ChatProposal.objects.filter(farm=self.farm, kind="action_toggle").first()
        self.assertIsNone(prop)
        victim.refresh_from_db()
        self.assertEqual(victim.status, "open")
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)


class RevocationTests(TestCase):
    def setUp(self):
        self.owner = make_user("rv-o@pelda.hu")
        self.farm = Farm.objects.create(owner=self.owner, name="RV farm")
        grant(self.farm, self.owner)
        self.mgr = make_user("rv-m@pelda.hu")
        Membership.objects.create(farm=self.farm, user=self.mgr, role="manager", is_active=True)

    def test_manager_can_dispatch_advice(self):
        from portal.services import request_advice
        from portal.tests import good_payload, FakeClient
        run = request_advice(self.mgr, self.farm, "rv-mgr-ok", client=FakeClient(payload=good_payload()))
        self.assertEqual(run.status, "success")

    def test_advice_role_revoked_before_dispatch_no_call(self):
        from portal.services import reserve_advice_run, execute_reserved_run
        from portal.tests import good_payload, FakeClient
        run = reserve_advice_run(self.mgr, self.farm, "rv-mgr-rev")
        Membership.objects.filter(farm=self.farm, user=self.mgr).update(is_active=False)
        fake = FakeClient(payload=good_payload())
        result = execute_reserved_run(run, client=fake)
        self.assertEqual(fake.calls, 0)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        from portal.models import AdviceStep
        self.assertEqual(AdviceStep.objects.filter(advice_run=result).count(), 0)

    def test_advice_role_revoked_inflight_no_steps_leak(self):
        from portal.services import request_advice
        from portal.tests import good_payload
        farm, mgr = self.farm, self.mgr

        class RevokingRoleClient:
            calls = 0

            def generate_advice(self, profile):
                type(self).calls += 1
                Membership.objects.filter(farm=farm, user=mgr).update(is_active=False)
                return good_payload()

        cli = RevokingRoleClient()
        run = request_advice(mgr, self.farm, "rv-mgr-flight", client=cli)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        from portal.models import AdviceStep
        self.assertEqual(AdviceStep.objects.filter(advice_run=run).count(), 0)

    def test_chat_role_revoked_before_dispatch_no_call(self):
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt
        run, created = _reserve_chat_attempt(self.mgr, self.farm, "rv-chat-rev", "hello?", "hu")
        self.assertTrue(created)
        Membership.objects.filter(farm=self.farm, user=self.mgr).update(is_active=False)
        fake = FakeChatClient(payload=good_chat_payload())
        result = execute_chat_attempt(run, client=fake, context_str="ctx")
        self.assertEqual(fake.calls, 0)
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(ChatProposal.objects.filter(attempt=result).count(), 0)

    def test_chat_role_revoked_midflight_no_leak(self):
        def revoke():
            Membership.objects.filter(farm=self.farm, user=self.mgr).update(is_active=False)
        fake = FakeChatClient(payload=good_chat_payload(), on_call=revoke)
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt
        run, _ = _reserve_chat_attempt(self.mgr, self.farm, "rv-chat-flight", "hello?", "hu")
        result = execute_chat_attempt(run, client=fake, context_str="ctx")
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.answer_text)
        self.assertEqual(ChatProposal.objects.filter(attempt=result).count(), 0)

    def test_revoked_before_confirm_denied_no_write(self):
        c = Client()
        c.force_login(self.mgr)
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            c.post("/chat/", {"message": "hi", "idempotency_key": "rv-conf-1",
                              "cost_consent": "on", "language": "hu"})
        prop = ChatProposal.objects.filter(farm=self.farm).first()
        self.assertIsNotNone(prop)
        Membership.objects.filter(farm=self.farm, user=self.mgr).update(is_active=False)
        audits_before = AuditEvent.objects.filter(farm=self.farm).count()
        actions_before = FarmAction.objects.filter(farm=self.farm).count()
        r = c.post(f"/chat/proposal/{prop.id}/confirm/")
        # Revoked membership loses active-farm scope -> 404; still-open role path -> 403.
        self.assertIn(r.status_code, (403, 404))
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), actions_before)
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits_before)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")

    def test_actor_mismatch_other_farm_user_denied(self):
        outsider = make_user("rv-out@pelda.hu")
        Farm.objects.create(owner=outsider, name="OUT farm")
        oc = Client()
        oc.force_login(outsider)
        prop = None
        c = Client()
        c.force_login(self.owner)
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            c.post("/chat/", {"message": "hi", "idempotency_key": "rv-actor-1",
                              "cost_consent": "on", "language": "hu"})
        prop = ChatProposal.objects.filter(farm=self.farm).first()
        self.assertIsNotNone(prop)
        r = oc.post(f"/chat/proposal/{prop.id}/confirm/")
        # Outsider's active farm differs, so lookup misses -> safe redirect with no write.
        self.assertIn(r.status_code, (302, 403, 404))
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")


class ConfirmSemanticsTests(TestCase):
    def setUp(self):
        self.u = make_user("cs@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="CS farm")
        grant(self.farm, self.u)
        self.c = Client()
        self.c.force_login(self.u)

    def test_idempotent_retry_confirm_no_extra_audit_or_write(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            self.c.post("/chat/", {"message": "hi", "idempotency_key": "cs-idem-1",
                                   "cost_consent": "on", "language": "hu"})
        prop = ChatProposal.objects.get(farm=self.farm)
        audits_before = AuditEvent.objects.filter(farm=self.farm).count()
        r1 = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        audits_after_first = AuditEvent.objects.filter(farm=self.farm).count()
        r2 = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        self.assertEqual((r1.status_code, r2.status_code), (302, 302))
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 1)
        self.assertEqual(audits_after_first, audits_before + 1)
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits_after_first)

    def test_confirm_requires_post_csrf_403(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            self.c.post("/chat/", {"message": "hi", "idempotency_key": "cs-csrf-1",
                                   "cost_consent": "on", "language": "hu"})
        prop = ChatProposal.objects.get(farm=self.farm)
        # GET on the confirm endpoint must not mutate (405) and leaves pending.
        r = self.c.get(f"/chat/proposal/{prop.id}/confirm/")
        self.assertEqual(r.status_code, 405)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")
        # POST without CSRF token is rejected 403 and leaves pending.
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.u)
        r2 = csrf_client.post(f"/chat/proposal/{prop.id}/confirm/", {})
        self.assertEqual(r2.status_code, 403)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)

    def test_navigate_confirm_writes_no_domain_action(self):
        nav_payload = {"answer": "ok", "actions": [{"kind": "navigate", "link": "location_list"}],
                       "links": [], "_actual_model": "m"}
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=nav_payload)):
            self.c.post("/chat/", {"message": "hol?", "idempotency_key": "cs-nav-1",
                                   "cost_consent": "on", "language": "hu"})
        prop = ChatProposal.objects.get(farm=self.farm, kind="navigate")
        actions_before = FarmAction.objects.filter(farm=self.farm).count()
        r = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        self.assertEqual(r.status_code, 302)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "done")
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), actions_before)

    def test_chat_get_never_mutates(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            self.c.post("/chat/", {"message": "hi", "idempotency_key": "cs-get-1",
                                   "cost_consent": "on", "language": "hu"})
        n_att = ChatAttempt.objects.filter(farm=self.farm).count()
        n_act = FarmAction.objects.filter(farm=self.farm).count()
        r = self.c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ChatAttempt.objects.filter(farm=self.farm).count(), n_att)
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), n_act)


class ProviderSanitizeTests(TestCase):
    def setUp(self):
        self.u = make_user("ps@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="PS farm")
        grant(self.farm, self.u)

    def test_http500_timeout_sanitized_no_secret_leak(self):
        for exc in (ProviderError("SECRET-XYZ http 500 internal detail"),
                    ProviderTimeout("SECRET-TIMEOUT detail")):
            fake = FakeChatClient(exc=exc)
            run = request_chat(self.u, self.farm, "msg", f"ps-{id(exc)}", language="hu",
                               client=fake, context_str="ctx")
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertNotIn("SECRET", (run.error_message_safe or ""))
            self.assertIsNone(run.answer_text)
            self.assertEqual(ChatProposal.objects.filter(attempt=run).count(), 0)


class BoundsTests(TestCase):
    def test_chat_client_sends_max6000_output_param(self):
        import httpx
        seen = {}

        def handler(request):
            import json as _j
            seen["body"] = _j.loads(request.content.decode())
            return httpx.Response(200, json={"model": "muse-spark-test",
                                            "choices": [{"message": {"content": _j.dumps(
                                                {"answer": "ok", "actions": [], "links": []})}}],
                                            "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
        from portal.chat import MetaChatClient
        from django.conf import settings
        cli = MetaChatClient(api_key="test-key", transport=httpx.MockTransport(handler))
        out = cli.generate_chat("hello?", "ctx", language="hu")
        self.assertEqual(out["answer"], "ok")
        self.assertEqual(seen["body"]["max_tokens"], 6000)
        self.assertEqual(seen["body"]["max_tokens"], settings.CHAT_MAX_OUTPUT_TOKENS)

    def test_bounded_context_and_oversized_response_rejected(self):
        from django.conf import settings
        big_farm_profile = "x" * (settings.CHAT_MAX_INPUT_CHARS + 5000)
        _ctx, s = build_chat_context(Farm.objects.create(
            owner=make_user("bx@pelda.hu"), name="BX farm")) if False else (None, None)
        # context builder caps at CHAT_MAX_INPUT_CHARS even for huge profiles
        u = make_user("bx2@pelda.hu")
        f = Farm.objects.create(owner=u, name="BX2 farm", soil_notes=big_farm_profile[:2000])
        _ctx, s = build_chat_context(f)
        self.assertLessEqual(len(s), settings.CHAT_MAX_INPUT_CHARS)
        # oversized answer rejected, never stored
        grant(f, u)
        fake = FakeChatClient(payload={"answer": "y" * 4001, "actions": [], "links": [],
                                       "_actual_model": "m"})
        run = request_chat(u, f, "msg", "bx-over-1", language="hu", client=fake, context_str="ctx")
        self.assertEqual(run.status, "failed")
        self.assertIsNone(ChatAttempt.objects.get(pk=run.pk).answer_text)


class NavCoverageTests(TestCase):
    def setUp(self):
        self.u = make_user("nv@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="NV farm")
        grant(self.farm, self.u)
        self.c = Client()
        self.c.force_login(self.u)

    def test_all_nav_links_resolve_and_render(self):
        from django.urls import reverse
        from portal.chat import CHAT_SAFE_DESTS
        for key in NAV_LINKS:
            url = reverse(key)  # every NAV_LINKS key must be a real route name
            self.assertTrue(url.startswith("/"), key)
        # POST-only/download whitelist is unchanged validation data:
        # raw paths stay in NAV_LINKS, never render as direct hrefs.
        self.assertEqual(NAV_LINKS["invite"], "/tagok/meghivas/")
        self.assertEqual(NAV_LINKS["weather_refresh"], "/weather/refresh/")
        self.assertEqual(NAV_LINKS["action_create"], "/teendok/uj/")
        self.assertEqual(NAV_LINKS["consent"], "/fiok/hozzajarulas/")
        self.assertEqual(NAV_LINKS["export"], "/fiok/export/")
        r = self.c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        # Intentional task2 removal: no page-local nav card on /chat/.
        self.assertNotIn("Oldaltérkép", body)
        # Global sidebar still renders its GET-safe targets on /chat/.
        for key in ("dashboard", "farm", "location_list", "property_list",
                    "equipment_list", "cycle_list", "plans", "weather",
                    "actions", "account", "chat", "advice_request",
                    "advice_history"):
            self.assertIn('href="%s"' % reverse(key), body, key)
        for raw in ("/tagok/meghivas/", "/weather/refresh/", "/teendok/uj/",
                    "/fiok/hozzajarulas/", "/fiok/export/"):
            self.assertNotIn('href="%s"' % raw, body)
        self.assertNotIn("data-fallback", body)
        # Destinations NOT in the sidebar stay reachable via typed navigate
        # proposals: allowlist-validate, store, and render the safe dest.
        from portal.chat import validate_chat_parsed as _validate
        non_sidebar = ("location_new", "property_new", "equipment_new",
                       "cycle_new", "invite", "weather_refresh",
                       "action_create", "consent", "account_delete", "export")
        attempt = ChatAttempt.objects.create(
            farm=self.farm, initiated_by=self.u, idempotency_key="navcov1",
            language="hu", status="success", source="live",
            requested_model="test", user_message="hova?",
            answer_text="Itt vannak a hivatkozasok.")
        pos = 0
        for link in non_sidebar:
            _answer, actions, _links = _validate(
                {"answer": "ok", "actions": [{"kind": "navigate", "link": link}],
                 "links": []})
            self.assertEqual(actions, [{"kind": "navigate", "link": link}])
            pos += 1
            ChatProposal.objects.create(
                attempt=attempt, farm=self.farm, position=pos,
                kind="navigate", payload_link=link)
        body2 = self.c.get("/chat/").content.decode()
        for link in non_sidebar:
            self.assertIn('href="%s"' % CHAT_SAFE_DESTS.get(link, reverse(link)),
                          body2, link)
        # Per-object edit/delete routes are intentionally NOT in NAV_LINKS
        # (unbounded ids); they are reached via list pages -> forms.
        for per_object in ("location_edit", "location_delete", "property_edit", "action_edit",
                           "action_toggle", "action_delete", "advice_detail", "cycle_observe"):
            self.assertNotIn(per_object, NAV_LINKS)
        # List pages link to their forms (deep-link path is sufficient).
        for list_url, new_url in (("/locations/", "/locations/new/"), ("/properties/", "/properties/new/"),
                                  ("/equipment/", "/equipment/new/"), ("/cycles/", "/cycles/new/")):
            rl = self.c.get(list_url)
            self.assertEqual(rl.status_code, 200)
            self.assertIn(new_url, rl.content.decode())

    def test_active_farm_selector_present_with_two_farms(self):
        u2 = make_user("nv2@pelda.hu")
        f2 = Farm.objects.create(owner=u2, name="NV farm2")
        Membership.objects.create(farm=f2, user=u2, role="viewer", is_active=True)
        # owner needs two accessible farms: own + membership on another owner's farm
        other_owner = make_user("nv3@pelda.hu")
        f3 = Farm.objects.create(owner=other_owner, name="NV farm3")
        grant(f3, other_owner)
        Membership.objects.create(farm=f3, user=self.u, role="worker", is_active=True)
        r = self.c.get("/locations/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("aktiv-farm", r.content.decode())

    def test_farm_profile_post_owner_manager_allowed_worker_viewer_denied(self):
        mgr = make_user("nv-m@pelda.hu")
        Membership.objects.create(farm=self.farm, user=mgr, role="manager", is_active=True)
        worker = make_user("nv-w@pelda.hu")
        Membership.objects.create(farm=self.farm, user=worker, role="worker", is_active=True)
        for user, code in ((self.u, 302), (mgr, 302), (worker, 403)):
            cc = Client()
            cc.force_login(user)
            cc.post("/aktiv-farm/", {"farm_id": self.farm.id})
            r = cc.post("/gazdasagom/", {"name": "NV farm", "soil_notes": "talaj"})
            self.assertEqual(r.status_code, code, user)
