"""Task13 slice2 RED: pending status endpoint is JSON, read-only, provider-free.

English comments per project convention.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import AdviceRun, ConsentEvent, Farm
from portal.services import _reserve_advice_run

User = get_user_model()


class AdviceStatusRedTest(TestCase):
    """GET /tanacsok/<id>/allapot/ returns pending JSON without side effects."""

    def test_pending_status_json_without_provider_or_writes(self):
        user = User.objects.create_user(email="status-red@pelda.hu", password="TesztJelszo123!")
        farm = Farm.objects.create(owner=user, name="Status farm")
        ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
        self.client.force_login(user)
        run, created = _reserve_advice_run(user, farm, "status-red-1")
        self.assertTrue(created)
        runs_before = AdviceRun.objects.count()
        steps_before = run.steps.count()
        started_at = AdviceRun.objects.get(pk=run.pk).attempt_started_at
        with patch("portal.services.MetaProviderClient") as mock_provider:
            response = self.client.get(f"/tanacsok/{run.id}/allapot/")
            # Intended endpoint: 200 JSON pending (currently missing -> 404).
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload.get("status"), "pending")
            self.assertIn("status_url", payload)
            mock_provider.assert_not_called()
        self.assertEqual(AdviceRun.objects.count(), runs_before)
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).steps.count(), steps_before)
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).attempt_started_at, started_at)
        self.assertEqual(AdviceRun.objects.get(pk=run.pk).status, "pending")
