"""Security-fix regressions (TDD RED first). No network, no secrets."""
import hashlib
import json

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, Client

from portal.models import Farm, FarmAction, Membership, Invitation, ConsentEvent, AuditEvent
from portal.chat_models import ChatAttempt, ChatProposal
from portal.chat import request_chat

User = get_user_model()
PW = "TesztJelszo123!"


def mkuser(email):
    return User.objects.create_user(email=email, password=PW)


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


def good_toggle_payload(action_id):
    return {"answer": "Rendben, javaslom a valtast.",
            "actions": [{"kind": "action_toggle", "action_id": action_id}],
            "links": [], "_actual_model": "m", "_usage_input_tokens": 1,
            "_usage_output_tokens": 2}


class FakeChatClient:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = 0

    def generate_chat(self, message, context, language=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return dict(self.payload)


class InviteRoleKeepTests(TestCase):
    def test_accept_keeps_existing_active_role_no_overwrite(self):
        owner = mkuser("own-keep@x.hu")
        farm = Farm.objects.create(owner=owner, name="Keep farm")
        w = mkuser("w-keep@x.hu")
        Membership.objects.create(farm=farm, user=w, role="worker", is_active=True)
        # New invite tries to upgrade the active member to manager.
        inv = Invitation.objects.create(
            farm=farm, email="w-keep@x.hu", role="manager",
            token_hash=hashlib.sha256(b"tok-keep-123").hexdigest(),
            created_by=owner,
            expires_at=__import__("django.utils.timezone", fromlist=["now"]).now()
            + __import__("datetime").timedelta(hours=72))
        c = Client()
        c.force_login(w)
        # Either outcome is acceptable per spec, but role must NOT be overwritten
        # to manager via the invite: reject, or accept-keep-current + mark used.
        r = c.post("/tagok/elfogadas/tok-keep-123/")
        mem = Membership.objects.get(farm=farm, user=w)
        self.assertEqual(mem.role, "worker")
        inv.refresh_from_db()
        if r.status_code == 302:
            self.assertIsNotNone(inv.used_at)
            self.assertTrue(AuditEvent.objects.filter(
                farm=farm, action="invitation.accepted").exists())
        else:
            self.assertIn(r.status_code, (403, 404))

    def test_invite_rejected_when_active_member_exists(self):
        owner = mkuser("own-dup@x.hu")
        farm = Farm.objects.create(owner=owner, name="Dup farm")
        w = mkuser("w-dup@x.hu")
        Membership.objects.create(farm=farm, user=w, role="worker", is_active=True)
        c = Client()
        c.force_login(owner)
        r = c.post("/tagok/meghivas/", {"email": "w-dup@x.hu", "role": "manager"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Invitation.objects.filter(
            farm=farm, email="w-dup@x.hu", used_at__isnull=True,
            revoked_at__isnull=True).exists())

    def test_revoke_audit_has_no_token(self):
        owner = mkuser("own-rev@x.hu")
        farm = Farm.objects.create(owner=owner, name="Rev farm")
        inv = Invitation.objects.create(
            farm=farm, email="r@x.hu", role="viewer",
            token_hash=hashlib.sha256(b"tok-rev-1").hexdigest(),
            created_by=owner,
            expires_at=__import__("django.utils.timezone", fromlist=["now"]).now()
            + __import__("datetime").timedelta(hours=72))
        c = Client()
        c.force_login(owner)
        r = c.post(f"/tagok/{inv.id}/visszavonas/")
        self.assertEqual(r.status_code, 302)
        ev = AuditEvent.objects.filter(farm=farm, action="invitation.revoked").first()
        self.assertIsNotNone(ev)
        self.assertNotIn(inv.token_hash, json.dumps(ev.meta, default=str))
        self.assertNotIn("tok-rev-1", json.dumps(ev.meta, default=str))


class ToggleStaleTests(TestCase):
    def setUp(self):
        self.u = mkuser("tog@x.hu")
        self.farm = Farm.objects.create(owner=self.u, name="Tog farm")
        grant(self.farm, self.u)
        self.c = Client()
        self.c.force_login(self.u)

    def test_stale_toggle_rejected_no_flip_no_audit(self):
        act = FarmAction.objects.create(farm=self.farm, text="Ontozesi kor", status="open")
        run = request_chat(self.u, self.farm, "toggle?", "tog-stale-1", language="hu",
                           client=FakeChatClient(payload=good_toggle_payload(act.id)),
                           context_str="ctx")
        self.assertEqual(run.status, "success")
        prop = ChatProposal.objects.filter(attempt=run, kind="action_toggle").first()
        self.assertIsNotNone(prop)
        # State changes after the proposal snapshot: different text + status.
        act.text = "Teljesen mas teendo"
        act.status = "done"
        from django.utils import timezone
        act.done_at = timezone.now()
        act.save()
        audits_before = AuditEvent.objects.filter(farm=self.farm).count()
        r = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        self.assertEqual(r.status_code, 302)
        act.refresh_from_db()
        self.assertEqual((act.status, act.text), ("done", "Teljesen mas teendo"))
        prop.refresh_from_db()
        self.assertEqual(prop.status, "pending")
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits_before)

    def test_fresh_toggle_confirms_to_desired_state(self):
        act = FarmAction.objects.create(farm=self.farm, text="Ontozesi kor", status="open")
        run = request_chat(self.u, self.farm, "toggle?", "tog-fresh-1", language="hu",
                           client=FakeChatClient(payload=good_toggle_payload(act.id)),
                           context_str="ctx")
        prop = ChatProposal.objects.filter(attempt=run, kind="action_toggle").first()
        r = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        self.assertEqual(r.status_code, 302)
        act.refresh_from_db()
        self.assertEqual(act.status, "done")
        prop.refresh_from_db()
        self.assertEqual(prop.status, "done")
        # UI preview must show target text + desired state.
        r2 = self.c.get("/chat/")
        body = r2.content.decode()
        self.assertIn("Ontozesi kor", body)


class ChatSafetyTests(TestCase):
    def setUp(self):
        self.u = mkuser("sf@x.hu")
        self.farm = Farm.objects.create(owner=self.u, name="SF farm")
        grant(self.farm, self.u)

    def test_unsafe_provider_answer_fails_no_proposals(self):
        bad = ("Permetezz 5 kg/ha novenyvedoszert, adag: 5 kg/ha. "
               "Garantalt hozam, biztos bevetel.")
        payload = {"answer": bad, "actions": [], "links": [], "_actual_model": "m"}
        run = request_chat(self.u, self.farm, "mit tegyek?", "sf-unsafe-1",
                           language="hu", client=FakeChatClient(payload=payload),
                           context_str="ctx")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.answer_text)
        self.assertEqual(ChatProposal.objects.filter(attempt=run).count(), 0)

    def test_unsafe_action_text_fails_no_proposals(self):
        payload = {"answer": "Rendben.",
                   "actions": [{"kind": "action_create",
                                "text": "Adj be 50 mg antibiotikumot naponta."}],
                   "links": [], "_actual_model": "m"}
        run = request_chat(self.u, self.farm, "mit tegyek?", "sf-unsafe-2",
                           language="hu", client=FakeChatClient(payload=payload),
                           context_str="ctx")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(ChatProposal.objects.filter(attempt=run).count(), 0)


class ProviderBoundTests(TestCase):
    def test_oversized_provider_body_rejected(self):
        from portal.chat import MetaChatClient
        from portal.provider import ProviderInvalidOutput
        big_answer = "x" * 600_000
        body = json.dumps({"answer": big_answer, "actions": [], "links": []})

        def handler(request):
            content = json.dumps(
                {"model": "m", "choices": [{"message": {"content": body}}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
            return httpx.Response(200, content=content.encode(),
                                  headers={"Content-Type": "application/json"})

        cli = MetaChatClient(api_key="test-key",
                             transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderInvalidOutput):
            cli.generate_chat("hello?", "ctx", language="hu")
