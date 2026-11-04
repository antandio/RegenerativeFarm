"""Async chat backend tests (TDD). Synthetic provider only, no paid API."""
from unittest.mock import patch
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from portal.models import Farm, ConsentEvent
from portal.chat_models import ChatAttempt

User = get_user_model()


def make_user(email="async@pelda.hu", pw="TesztJelszo123!"):
    return User.objects.create_user(email=email, password=pw)


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from datetime import timedelta
from portal.models import Farm, ConsentEvent, Membership
from portal.chat_models import ChatAttempt, ChatProposal

User = get_user_model()


def make_user(email="async@pelda.hu", pw="TesztJelszo123!"):
    return User.objects.create_user(email=email, password=pw)


def grant(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")


def revoke(farm, user):
    return ConsentEvent.objects.create(farm=farm, actor=user, event_type="revoked")


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


def good_payload(answer="Ontözesi megfigyeles hetente."):
    return {"answer": answer,
            "actions": [{"kind": "action_create", "text": "Heti megfigyeles"}],
            "links": [],
            "_actual_model": "muse-spark-test",
            "_usage_input_tokens": 10, "_usage_output_tokens": 20}


class AsyncEnqueueTests(TestCase):
    def setUp(self):
        self.u = make_user()
        self.farm = Farm.objects.create(owner=self.u, name="Tesztfarm")
        self.c = Client()
        self.c.force_login(self.u)
        grant(self.farm, self.u)

    def test_enqueue_post_returns_202_without_provider(self):
        constructed = []

        class ExplodingClient:
            def __init__(self, *a, **k):
                constructed.append(True)

            def generate_chat(self, *a, **k):
                raise AssertionError("provider must not be called")

        with patch("portal.chat.MetaChatClient", ExplodingClient):
            r = self.c.post("/chat/enqueue/", {"message": "Hogy ontozzek?",
                                               "idempotency_key": "async-red-1",
                                               "cost_consent": "on",
                                               "language": "hu"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(constructed, [])
        att = ChatAttempt.objects.get(farm=self.farm, idempotency_key="async-red-1")
        self.assertEqual(att.status, "pending")

    def test_status_api_queued_then_worker_done(self):
        r = self.c.post("/chat/enqueue/", {"message": "Hogy ontozzek?",
                                           "idempotency_key": "async-q-1",
                                           "cost_consent": "on", "language": "hu"})
        self.assertEqual(r.status_code, 202)
        att = ChatAttempt.objects.get(farm=self.farm, idempotency_key="async-q-1")
        s = self.c.get(f"/chat/status/{att.pk}/")
        self.assertEqual(s.status_code, 200)
        self.assertEqual(s.json()["status"], "queued")
        from portal.chat_worker import run_worker_once
        fake = FakeChatClient(payload=good_payload())
        self.assertEqual(run_worker_once(client=fake), "success")
        self.assertEqual(fake.calls, 1)
        s = self.c.get(f"/chat/status/{att.pk}/")
        self.assertEqual(s.json()["status"], "done")
        att.refresh_from_db()
        self.assertTrue(att.answer_text)

    def test_second_execute_never_calls_provider(self):
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt, authorize_chat_dispatch
        run, _ = _reserve_chat_attempt(self.u, self.farm, "async-once-1", "Hogy ontozzek?", "hu")
        fake = FakeChatClient(payload=good_payload())
        first = execute_chat_attempt(run, client=fake, context_str="ctx")
        self.assertEqual(first.status, "success")
        self.assertEqual(fake.calls, 1)
        # Re-authorization is refused: only the first winner proceeds.
        self.assertFalse(authorize_chat_dispatch(run))
        again = execute_chat_attempt(run, client=fake, context_str="ctx")
        self.assertEqual(again.status, "success")
        self.assertEqual(fake.calls, 1)

    def test_stale_claimed_reaped_stale_queued_survives(self):
        from portal.chat_worker import run_worker_once
        from portal.chat import _reserve_chat_attempt
        with override_settings(ADVICE_PENDING_STALE_MINUTES=30):
            old_q, _ = _reserve_chat_attempt(
                self.u, self.farm, "async-old-q", "Regi kerdes?", "hu")
            ChatAttempt.objects.filter(pk=old_q.pk).update(
                attempt_started_at=timezone.now() - timedelta(hours=5))
            old_c, _ = _reserve_chat_attempt(
                self.u, self.farm, "async-old-c", "Regi igeny?", "hu")
            from portal.chat import authorize_chat_dispatch
            self.assertTrue(authorize_chat_dispatch(old_c))
            ChatAttempt.objects.filter(pk=old_c.pk).update(
                attempt_started_at=timezone.now() - timedelta(hours=5),
                dispatch_authorized_at=timezone.now() - timedelta(hours=5))
            fake = FakeChatClient(payload=good_payload())
            # Claimed orphan fails without any provider call.
            self.assertEqual(run_worker_once(client=fake), "reaped")
            self.assertEqual(fake.calls, 0)
            old_c.refresh_from_db()
            self.assertEqual(old_c.status, "failed")
            # Queued survivor is claimed and processed afterwards.
            self.assertEqual(run_worker_once(client=fake), "success")
            self.assertEqual(fake.calls, 1)
            old_q.refresh_from_db()
            self.assertEqual(old_q.status, "success")

    def test_history_private_paginated_and_get_makes_no_writes(self):
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt
        for i in range(3):
            run, _ = _reserve_chat_attempt(
                self.u, self.farm, f"async-h-{i}", f"Kerdes {i}?", "hu")
            execute_chat_attempt(run, client=FakeChatClient(payload=good_payload()),
                                 context_str="ctx")
        other = make_user(email="masik@pelda.hu")
        Membership.objects.create(user=other, farm=self.farm, role="manager", is_active=True)
        oc = Client()
        oc.force_login(other)
        grant_other = grant(self.farm, other)
        from portal.chat import _reserve_chat_attempt as _r2, execute_chat_attempt as _e2
        orun, _ = _r2(other, self.farm, "async-h-other", "Mas kerdes?", "hu")
        _e2(orun, client=FakeChatClient(payload=good_payload()), context_str="ctx")
        before = list(ChatAttempt.objects.filter(farm=self.farm).values_list("pk", "updated_at"))
        r = self.c.get("/chat/history/?page=1&page_size=2")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["page_size"], 2)
        self.assertEqual(data["total"], 3)
        for row in data["results"]:
            self.assertNotIn("Mas kerdes?", row.get("message") or "")
        after = list(ChatAttempt.objects.filter(farm=self.farm).values_list("pk", "updated_at"))
        self.assertEqual(before, after)
        # Oversized page is capped, not an error.
        r = self.c.get("/chat/history/?page_size=500")
        self.assertEqual(r.json()["page_size"], 20)
        # Other user sees only their own row via status/history.
        s = oc.get(f"/chat/status/{orun.pk}/")
        self.assertEqual(s.status_code, 200)
        mine = ChatAttempt.objects.get(farm=self.farm, idempotency_key="async-h-0")
        self.assertEqual(oc.get(f"/chat/status/{mine.pk}/").status_code, 404)
        self.assertEqual(oc.get("/chat/history/").json()["total"], 1)

    def test_delete_tombstone_scrubs_and_worker_never_resurrects(self):
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt
        from portal.chat_worker import run_worker_once
        run, _ = _reserve_chat_attempt(
            self.u, self.farm, "async-del-1", "Torlendo kerdes?", "hu")
        execute_chat_attempt(run, client=FakeChatClient(payload=good_payload()),
                             context_str="ctx")
        att_id = run.pk
        prop_count = ChatProposal.objects.filter(attempt_id=att_id).count()
        self.assertGreaterEqual(prop_count, 1)
        r = self.c.post(f"/chat/attempt/{att_id}/delete/")
        self.assertEqual(r.status_code, 302)
        tomb = ChatAttempt.objects.get(pk=att_id)
        self.assertEqual(tomb.user_message, "")
        self.assertIsNone(tomb.answer_text)
        self.assertEqual(tomb.selection_snapshot, {})
        self.assertEqual(ChatProposal.objects.filter(attempt_id=att_id).count(), 0)
        # Minimal accounting metadata stays for quota audit.
        self.assertEqual(tomb.initiated_by_id, self.u.pk)
        self.assertEqual(tomb.idempotency_key, "async-del-1")
        # A queued row deleted before the worker runs never resurrects.
        run2, _ = _reserve_chat_attempt(
            self.u, self.farm, "async-del-2", "Torlendo sor?", "hu")
        self.c.post(f"/chat/attempt/{run2.pk}/delete/")
        fake = FakeChatClient(payload=good_payload())
        # Drain any other queued work first is unnecessary: deleted row is skipped.
        outcome = run_worker_once(client=fake)
        run2.refresh_from_db()
        self.assertIsNone(run2.answer_text)
        self.assertEqual(ChatProposal.objects.filter(attempt_id=run2.pk).count(), 0)
        self.assertIn(outcome, ("idle", "reaped", "success", "failed"))

    def test_cross_user_cannot_confirm_or_reuse_key(self):
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt, ChatRejected
        from portal.chat import confirm_proposal
        run, _ = _reserve_chat_attempt(
            self.u, self.farm, "async-priv-1", "Sajat kerdes?", "hu")
        execute_chat_attempt(run, client=FakeChatClient(payload=good_payload()),
                             context_str="ctx")
        prop = ChatProposal.objects.filter(attempt=run).first()
        self.assertIsNotNone(prop)
        other = make_user(email="harmadik@pelda.hu")
        Membership.objects.create(user=other, farm=self.farm, role="manager", is_active=True)
        grant(self.farm, other)
        with self.assertRaises(ChatRejected):
            confirm_proposal(other, self.farm, prop.pk)
        # Another user's idempotency key is rejected, never exposed.
        with self.assertRaises(ChatRejected):
            _reserve_chat_attempt(other, self.farm, "async-priv-1", "Mas?", "hu")
        oc = Client()
        oc.force_login(other)
        self.assertEqual(oc.post(f"/chat/attempt/{run.pk}/delete/").status_code, 404)
        self.assertEqual(oc.post(f"/chat/proposal/{prop.pk}/confirm/").status_code, 404)

    def test_get_chat_page_shows_only_own_history(self):
        from portal.chat import _reserve_chat_attempt, execute_chat_attempt
        run, _ = _reserve_chat_attempt(
            self.u, self.farm, "async-page-1", "Sajat kerdes?", "hu")
        execute_chat_attempt(run, client=FakeChatClient(payload=good_payload()),
                             context_str="ctx")
        other = make_user(email="negyedik@pelda.hu")
        Membership.objects.create(user=other, farm=self.farm, role="manager", is_active=True)
        grant(self.farm, other)
        orun, _ = _reserve_chat_attempt(other, self.farm, "async-page-2", "Masik kerdes?", "hu")
        execute_chat_attempt(orun, client=FakeChatClient(payload=good_payload()),
                             context_str="ctx")
        r = self.c.get("/chat/")
        self.assertEqual(r.status_code, 200)
        shown = [a.user_message for a in r.context["attempts"]]
        self.assertIn("Sajat kerdes?", shown)
        self.assertNotIn("Masik kerdes?", shown)
