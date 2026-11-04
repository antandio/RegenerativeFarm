"""Task13 HTML reserve: HTML POST reserves and redirects provider-free.

English comments per project convention.
INTENTIONAL CONTRACT CHANGE (Task1 GREEN): the request path is
reserve-only. Scheduling is never deferred to on_commit and no thread
executor exists; a dedicated worker claims queued runs.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import AdviceRun, ConsentEvent, Farm

User = get_user_model()


class AdviceHtmlReserveTest(TestCase):
    """HTML POST reserves once and redirects to detail without provider."""

    def test_html_post_reserves_and_redirects_without_provider(self):
        """HTML POST reserves; no provider, no on_commit scheduling."""
        user = User.objects.create_user(email="task13-html@pelda.hu", password="TesztJelszo123!")
        farm = Farm.objects.create(owner=user, name="HTML reserve farm")
        ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
        self.client.force_login(user)
        with patch("portal.services.MetaProviderClient") as mock_provider, \
                patch("portal.views.transaction.on_commit") as mock_commit:
            response = self.client.post(
                "/tanacs/keres/",
                {"idempotency_key": "task13-html-1"},
            )
            # Must redirect to the detail page for the reserved run.
            self.assertEqual(response.status_code, 302)
            run = AdviceRun.objects.get(farm=farm, idempotency_key="task13-html-1")
            self.assertEqual(run.status, "pending")
            self.assertTrue(response["Location"].endswith(f"/tanacsok/{run.id}/"))
            # No synchronous provider work and no deferred scheduling.
            mock_provider.assert_not_called()
            mock_commit.assert_not_called()
