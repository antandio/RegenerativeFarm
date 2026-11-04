"""Regression tests for tasks.md task2: redundant chat-local navigation card removed.

English comments/docstrings by project convention; UI strings stay HU/EN.
"""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from portal.models import Farm

User = get_user_model()


class ChatNavCardRemovalTests(TestCase):
    """The page-local nav card must be gone; global sidebar must stay."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="task2nav@pelda.hu", password="TesztJelszo123!"
        )
        self.farm = Farm.objects.create(owner=self.user, name="Task2 farm")
        self.client = Client()
        self.client.force_login(self.user)

    def test_no_page_local_nav_card(self):
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("Oldaltérkép", body)
        self.assertNotIn("Navigáció (rögzített hivatkozások)", body)

    def test_global_sidebar_preserved(self):
        body = self.client.get("/chat/").content.decode()
        self.assertIn("app-sidebar", body)

    def test_navigate_proposal_renders_stored_pending_proposal(self):
        # Real rendered fixture (not a template-source string check): a
        # stored pending navigate proposal must render its safe dest href,
        # human label, and confirm POST on GET /chat/.
        from django.urls import reverse
        from portal.chat import CHAT_SAFE_DESTS, validate_chat_parsed
        from portal.chat_models import ChatAttempt, ChatProposal
        _, actions, _ = validate_chat_parsed(
            {"answer": "ok",
             "actions": [{"kind": "navigate", "link": "action_create"}],
             "links": []})
        self.assertEqual(actions, [{"kind": "navigate", "link": "action_create"}])
        attempt = ChatAttempt.objects.create(
            farm=self.farm, initiated_by=self.user,
            idempotency_key="task2nav1", language="hu", status="success",
            source="live", requested_model="test",
            user_message="hova?", answer_text="Itt egy ugras.")
        prop = ChatProposal.objects.create(
            attempt=attempt, farm=self.farm, position=1,
            kind="navigate", payload_link="action_create")
        self.assertEqual(prop.status, "pending")
        body = self.client.get("/chat/").content.decode()
        dest = CHAT_SAFE_DESTS["action_create"]
        self.assertIn('href="%s"' % dest, body)
        self.assertInHTML('<a href="%s">Új teendő</a>' % dest, body)
        self.assertIn('action="%s"' % reverse(
            "chat_proposal_confirm", args=[prop.id]), body)

    def test_no_chat_local_nav_context(self):
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 200)
        context = getattr(response, "context", None)
        self.assertIsNotNone(context)
        self.assertNotIn("nav_items", context)
        self.assertNotIn("nav_links", context)
