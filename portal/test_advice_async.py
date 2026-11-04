"""Task13 FIRST vertical slice RED regression: async advice request.

English comments/docstrings per project convention; UI strings stay HU/EN.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import ConsentEvent, Farm

User = get_user_model()


def make_owner(email="task13-owner@pelda.hu"):
    """Create an authenticated owner with a farm and granted consent."""
    user = User.objects.create_user(email=email, password="TesztJelszo123!")
    farm = Farm.objects.create(owner=user, name="Task13 farm")
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return user, farm


class AdviceAsyncRedTest(TestCase):
    """Async slice must return 202 pending without synchronous provider call."""

    def test_owner_json_post_returns_202_pending_without_sync_provider_call(self):
        """Authenticated consenting owner JSON POST gets 202 pending run."""
        user, farm = make_owner()
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "task13-red-1"},
                HTTP_ACCEPT="application/json",
            )
            # Intended async behavior: 202 with pending run and status URL.
            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload.get("status"), "pending")
            self.assertIn("status_url", payload)
            # Scheduling boundary must defer provider work, never call inline.
            mock_provider.assert_not_called()
