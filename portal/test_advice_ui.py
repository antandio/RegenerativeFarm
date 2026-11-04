"""Task13 UI slice RED: progressive-enhancement advice UI.

English comments per project convention; user-visible strings stay HU/EN.
Covers the rendered request form (data hook, aria-live status, JS asset),
the pending detail page (auto-poll hook, manual guidance, restart honesty)
and the success status fragment (3 steps with save-action forms).
"""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.models import AdviceStep, ConsentEvent, Farm

User = get_user_model()


def make_owner(email="task13-ui@pelda.hu"):
    """Create an authenticated owner with a farm and granted consent."""
    user = User.objects.create_user(email=email, password="TesztJelszo123!")
    farm = Farm.objects.create(owner=user, name="Task13 UI farm")
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return user, farm


def make_run(user, farm, key="task13-ui-1", status="pending"):
    """Reserve a run through the real service boundary (no provider)."""
    from portal.services import _reserve_advice_run
    run, _ = _reserve_advice_run(user, farm, key)
    if status != "pending":
        run.status = status
        run.save(update_fields=["status"])
    return run


def make_success_run(user, farm, key="task13-ui-success"):
    """Reserve a run and complete it with exactly 3 steps."""
    run = make_run(user, farm, key=key, status="success")
    for pos in (1, 2, 3):
        AdviceStep.objects.create(
            advice_run=run, farm=farm, position=pos,
            title=f"Lepes {pos}", action_text=f"Teendo {pos}",
            rationale=f"Indoklas {pos}", assumptions=[f"Felteteles {pos}"],
            risk_note=f"Kockazat {pos}",
        )
    return run


class AdviceRequestUiTests(TestCase):
    """Request page carries progressive-enhancement hooks and the JS asset."""

    def test_form_has_data_hook_status_region_and_js_asset(self):
        user, _ = make_owner()
        self.client.force_login(user)
        response = self.client.get("/tanacs/keres/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Progressive-enhancement hook: JS upgrades this form, no-JS posts normally.
        self.assertIn("data-advice-form", body)
        self.assertIn('name="idempotency_key"', body)
        # Honest progress region: announced, never a fake percentage bar.
        self.assertIn('role="status"', body)
        self.assertIn('aria-live="polite"', body)
        self.assertRegex(body, r"aria-busy=\"(true|false)\"")
        self.assertNotIn("progress-bar", body)
        self.assertNotIn("szazalek", body.lower())
        # External cache-busted JS loaded via the static template tag.
        self.assertRegex(body, r"/static/portal/advice\.js\?v=[\w.\-]+")

    def test_english_renders_via_language_cookie(self):
        user, _ = make_owner(email="task13-ui-en@pelda.hu")
        self.client.force_login(user)
        self.client.cookies.load({"django_language": "en"})
        response = self.client.get("/tanacs/keres/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('<html lang="en">', body)
        self.assertIn("data-advice-form", body)
        # Key controls are translated; Hungarian labels must not leak.
        self.assertIn("Request advice", body)
        self.assertNotIn("Tanács kérése (pontosan 3 lépés)", body)


class AdviceDetailUiTests(TestCase):
    """Pending detail auto-polls without reload and stays honest without JS."""

    def test_pending_detail_polls_with_manual_guidance(self):
        user, farm = make_owner(email="task13-ui-det@pelda.hu")
        self.client.force_login(user)
        run = make_run(user, farm, key="task13-ui-det-1")
        response = self.client.get(f"/tanacsok/{run.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Auto-poll hook with the read-only status URL; no full-page reload.
        self.assertIn("data-advice-poll", body)
        self.assertIn(f"/tanacsok/{run.id}/allapot/", body)
        self.assertIn('aria-live="polite"', body)
        self.assertIn('aria-busy="true"', body)
        self.assertNotIn("location.reload", body)
        self.assertNotIn("http-equiv=\"refresh\"", body.lower())
        # No-JS/manual guidance plus honest server-restart limitation.
        self.assertIn(f"/tanacsok/{run.id}/", body)
        self.assertRegex(body, r"[Ff]riss[ií]t")
        self.assertRegex(body, r"[Úú]jraind")

    def test_success_status_fragment_has_steps_and_save_forms(self):
        user, farm = make_owner(email="task13-ui-frag@pelda.hu")
        self.client.force_login(user)
        run = make_success_run(user, farm)
        response = self.client.get(f"/tanacsok/{run.id}/allapot/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "success")
        html = payload.get("html", "")
        # All three real steps are rendered server-side (escaped by Django).
        for pos in (1, 2, 3):
            self.assertIn(f"Lepes {pos}", html)
        # Each step keeps a usable save-action form with CSRF protection.
        self.assertEqual(len(re.findall(r"csrfmiddlewaretoken", html)), 3)
        self.assertEqual(html.count(f"/tanacsok/{run.id}/lepes/"), 3)
        self.assertIn("/akcio/", html)
        # Fragment navigation stays in-page; the player never reloads.
        self.assertNotIn("location.reload", html)
        self.assertNotIn("location.assign", html)


class AdviceJsContractTests(TestCase):
    """Deterministic static contract for the external advice player (no driver)."""

    @classmethod
    def _js(cls):
        from pathlib import Path
        from django.conf import settings
        path = Path(settings.BASE_DIR) / "portal" / "static" / "portal" / "advice.js"
        assert path.exists(), "portal/static/portal/advice.js is missing"
        return path.read_text(encoding="utf-8")

    def test_fetch_uses_csrf_and_json_without_fake_progress(self):
        js = self._js()
        self.assertIn("application/json", js)
        self.assertIn("X-CSRFToken", js)
        # Network/error text goes through textContent, never raw HTML sinks.
        self.assertIn("textContent", js)
        self.assertNotIn("innerHTML =", js.replace("resultEl.innerHTML", "resultEl.innerHTML-allowed-once"))
        # No fake percentage: no width-% progress simulation.
        self.assertNotRegex(js, r"width\s*=\s*[\"']?\d+%")

    def test_terminal_updates_data_phase_done_or_error(self):
        js = self._js()
        start = js.index("function finishTerminal")
        end = js.index("function startPoll", start)
        body = js[start:end]
        # Terminal rendering must move data-phase off queued/running.
        self.assertIn('setAttribute("data-phase"', body)
        self.assertIn('"done"', body)
        self.assertIn('"error"', body)
        self.assertIn("payload.phase", body)

    def test_polling_backoff_serialized_guarded_and_same_key(self):
        js = self._js()
        # Serialized backoff: starts at 2s, capped at 5s.
        self.assertIn("2000", js)
        self.assertIn("5000", js)
        # Stale responses after unmount or a newer run must not overwrite.
        self.assertRegex(js, r"[Aa]bortController|generation|runToken|stale")
        # Same idempotency key is reused; the player never mints a new one.
        self.assertIn("idempotency_key", js)
        self.assertNotRegex(js, r"crypto\.randomUUID|uuid|newKey|regenerat")
        # Terminal states stop polling; never an automatic new POST or reload.
        self.assertNotIn("location.reload", js)
        self.assertNotIn("location.assign", js)
        self.assertEqual(js.count("fetch("), 2)


class TerminalDetailHookTests(TestCase):
    """Terminal detail pages never carry the pending auto-poll hook."""

    def test_success_detail_has_no_poll_hook(self):
        user, farm = make_owner(email="task13-ui-term1@pelda.hu")
        self.client.force_login(user)
        run = make_success_run(user, farm, key="task13-ui-term1")
        response = self.client.get(f"/tanacsok/{run.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("data-advice-poll", body)
        self.assertIn('aria-busy="false"', body)

    def test_failed_detail_has_no_poll_hook(self):
        user, farm = make_owner(email="task13-ui-term2@pelda.hu")
        self.client.force_login(user)
        run = make_run(user, farm, key="task13-ui-term2", status="failed")
        response = self.client.get(f"/tanacsok/{run.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("data-advice-poll", body)
        self.assertIn('aria-busy="false"', body)
