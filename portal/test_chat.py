"""Stage5 CHAT tests (TDD FIRST). Mocked provider only, no network."""
from unittest.mock import patch
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from portal.models import Farm, FarmAction, ConsentEvent, Membership, AuditEvent, AdviceRun, FarmProfileSnapshot
from portal.chat_models import ChatAttempt, ChatProposal

User = get_user_model()


def make_user(email="anna@pelda.hu", pw="TesztJelszo123!"):
    return User.objects.create_user(email=email, password=pw)


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


def good_chat_payload(answer="Öntözési megfigyelés hetente."):
    return {"answer": answer,
            "actions": [{"kind": "action_create", "text": "Heti talajnedvesség megfigyelés"}],
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


def send_chat(testcase, client, msg="Hogy öntözzek?", idem="chat-idem-1", consent="on"):
    return client.post("/chat/", {"message": msg, "idempotency_key": idem,
                                  "cost_consent": consent, "language": "hu"})


class ChatFlowTests(TestCase):
    def setUp(self):
        self.u = make_user()
        self.farm = Farm.objects.create(owner=self.u, name="Tesztfarm")
        self.c = Client()
        self.c.force_login(self.u)
        grant(self.farm, self.u)

    def test_send_returns_proposal_pending_no_farmaction(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            r = send_chat(self, self.c)
        self.assertEqual(r.status_code, 302)
        att = ChatAttempt.objects.get(farm=self.farm, idempotency_key="chat-idem-1")
        self.assertEqual(att.status, "success")
        props = list(ChatProposal.objects.filter(attempt=att))
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].status, "pending")
        self.assertEqual(props[0].kind, "action_create")
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)

    def test_confirm_creates_action_atomic_idempotent(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            send_chat(self, self.c)
        prop = ChatProposal.objects.get(farm=self.farm)
        audits_before = AuditEvent.objects.filter(farm=self.farm).count()
        r1 = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        r2 = self.c.post(f"/chat/proposal/{prop.id}/confirm/")
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(r2.status_code, 302)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "done")
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 1)
        # idempotent: second confirm creates no duplicate action / single audit
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(farm=self.farm).count(), audits_before + 1)

    def test_viewer_read_allowed_but_no_confirm(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            send_chat(self, self.c)
        prop = ChatProposal.objects.get(farm=self.farm)
        viewer = make_user("nezo@pelda.hu")
        Membership.objects.create(farm=self.farm, user=viewer, role="viewer", is_active=True)
        vc = Client()
        vc.force_login(viewer)
        vc.post("/aktiv-farm/", {"farm_id": self.farm.id})
        self.assertEqual(vc.get("/chat/").status_code, 200)
        self.assertEqual(vc.post(f"/chat/proposal/{prop.id}/confirm/").status_code, 403)
        # viewer cannot send either (read-only)
        r = vc.post("/chat/", {"message": "x", "idempotency_key": "v1", "cost_consent": "on"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(FarmAction.objects.filter(farm=self.farm).count(), 0)

    def test_consent_required_every_call(self):
        ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="revoked")
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            r = send_chat(self, self.c, idem="no-consent-1")
        self.assertEqual(r.status_code, 302)
        att = ChatAttempt.objects.filter(farm=self.farm, idempotency_key="no-consent-1").first()
        self.assertTrue(att is None or att.status == "failed")

    def test_revoke_midflight_suppresses_proposals(self):
        def revoke():
            ConsentEvent.objects.create(farm=self.farm, actor=self.u, event_type="revoked")
        with patch("portal.chat_views.get_chat_client",
                   return_value=FakeChatClient(payload=good_chat_payload(), on_call=revoke)):
            send_chat(self, self.c, idem="revoke-mid-1")
        att = ChatAttempt.objects.get(farm=self.farm, idempotency_key="revoke-mid-1")
        self.assertEqual(att.status, "failed")
        self.assertEqual(ChatProposal.objects.filter(attempt=att).count(), 0)

    def test_quota_shared_with_advice(self):
        from django.conf import settings
        snap = FarmProfileSnapshot.objects.create(farm=self.farm, profile_version=self.farm.profile_version,
                                                  profile_json=self.farm.profile_dict())
        now = timezone.now()
        for i in range(settings.ADVICE_MAX_PER_HOUR_PER_FARM):
            AdviceRun.objects.create(farm=self.farm, initiated_by=self.u, profile_snapshot=snap,
                                     idempotency_key=f"fill-{i}", language="hu", status="failed",
                                     source="live", requested_model="m", attempt_started_at=now)
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            r = send_chat(self, self.c, idem="over-quota-1")
        self.assertEqual(r.status_code, 302)
        att = ChatAttempt.objects.filter(farm=self.farm, idempotency_key="over-quota-1").first()
        self.assertTrue(att is None or att.status == "failed")

    def test_message_bounds_rejected(self):
        with patch("portal.chat_views.get_chat_client", return_value=FakeChatClient(payload=good_chat_payload())):
            r = send_chat(self, self.c, msg="x" * 2001, idem="too-long-1")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ChatAttempt.objects.filter(idempotency_key="too-long-1", status="success").exists())
