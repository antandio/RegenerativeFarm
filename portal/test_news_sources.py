"""Task10 RED: own news-portal collection (CRUD only, no future scanning).

Contract (tasks.md item 10 + docs/ADDITIONAL_TASKS_PLAN.md section 10):
- NewsSource(owner FK->User, name mandatory, url validated, notes blank,
  is_active True default, created/updated, unique(owner, normalized url)).
  User-owned (like task8 docs), NOT farm-bound: no sharing implied, no
  farm activation; usable independent of farm memberships.
- URL: http/https only; strict parse; reject userinfo/credentials,
  whitespace/controls, fragments stripped canonically, default ports
  normalized, IDNA host normalized; reject localhost, single-label,
  .local, private/loopback/link-local/reserved/multicast IP literals
  (v4+v6), obfuscated numeric forms, invalid ports, unsafe schemes
  (javascript/data/file/ftp). No DNS fetch: syntactic defense only, and
  the future fetch worker must revalidate resolved IPs/redirects
  (DNS rebinding) — documented, not implemented here.
- NO network/scheduler/cloud/LLM: no fetch, no cron, no proposals. The UI
  states the future daily scan is not running.
- Owner-only: other's rows 404 everywhere (list/detail/edit/delete/toggle),
  no leak in HTML/URLs/errors. Active-farm switches never expose others.
- Writes: POST+CSRF only; GET never mutates (toggle GET -> 405, delete GET
  renders confirm). Invalid POST -> 400, no row.
- External links: rel="noopener noreferrer" target="_blank", user content
  escaped.
- Sidebar entry; HU/EN strings; responsive/accessible markup.

TDD: this file is written BEFORE production. Route tests assert desired 200
while baseline has no routes (404) — real assertion RED, not ImportError.
Model tests use lazy imports so route REDs still execute before the model
exists.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from portal.models import Farm, Membership
from portal.tests import make_user


def _models():
    from portal.models import NewsSource
    return NewsSource


VALID = {
    "name": "Hírportál",
    "url": "https://example.com/feed",
    "notes": "Napi agrárhírek.",
}


def _mk(user, name="Portál", url="https://example.com/feed"):
    NewsSource = _models()
    return NewsSource.objects.create(owner=user, name=name, url=url)


class NewsSourceRouteTests(TestCase):
    """Baseline: desired routes must exist (RED: 404 before production)."""

    def setUp(self):
        self.owner = make_user("t10route@pelda.hu")
        self.farm = Farm.objects.create(owner=self.owner, name="FarmR")
        self.client.force_login(self.owner)

    def test_list_route_desired_200(self):
        r = self.client.get("/hirforrasok/")
        self.assertEqual(r.status_code, 200)

    def test_create_route_desired_200(self):
        r = self.client.get("/hirforrasok/uj/")
        self.assertEqual(r.status_code, 200)


class NewsSourceModelTests(TestCase):
    def setUp(self):
        self.owner = make_user("t10m-a@pelda.hu")
        self.other = make_user("t10m-b@pelda.hu")

    def test_defaults_owner_unique_notes_blank(self):
        NewsSource = _models()
        obj = NewsSource.objects.create(
            owner=self.owner, name=" Portál ", url="https://example.com/feed")
        self.assertEqual(obj.name, "Portál")
        self.assertEqual(obj.notes, "")
        self.assertTrue(obj.is_active)
        self.assertIsNotNone(obj.created_at)
        self.assertIsNotNone(obj.updated_at)
        self.assertEqual(obj.owner, self.owner)

    def test_normalized_unique_per_owner_allows_other_owner(self):
        NewsSource = _models()
        _mk(self.owner, url="https://example.com/feed")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Same URL, different case + default port + fragment:
                # canonical duplicate -> DB unique violation, not 500 page.
                _mk(self.owner, name="Másik", url="HTTPS://EXAMPLE.COM:443/feed#x")
        # Another owner may store the same normalized URL (no sharing implied).
        _mk(self.other, url="https://example.com/feed")
        self.assertEqual(
            NewsSource.objects.filter(owner=self.other).count(), 1)

    def test_name_required(self):
        NewsSource = _models()
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            NewsSource.objects.create(
                owner=self.owner, name="   ", url="https://example.com/")

    def test_url_canonicalization(self):
        NewsSource = _models()
        obj = _mk(self.owner, url="HTTPS://Example.COM:443/hirek#mai")
        self.assertEqual(obj.url, "https://example.com/hirek")
        obj2 = _mk(self.owner, name="P2", url="http://example.com:80/a")
        self.assertEqual(obj2.url, "http://example.com/a")
        obj3 = _mk(self.other, name="P3", url="https://münchen.de/")
        self.assertIn("xn--", obj3.url)

    def test_url_rejection_matrix(self):
        NewsSource = _models()
        from django.core.exceptions import ValidationError
        bad = [
            "javascript:alert(1)",
            "data:text/plain,hi",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "example.com/feed",  # scheme-less
            "https://user:pass@example.com/",
            "https://user@example.com/",
            "https://127.0.0.1/",
            "https://10.0.0.5/n",
            "https://192.168.1.1/n",
            "https://[::1]/n",
            "https://localhost/n",
            "https://intranet/n",  # single-label
            "https://printer.local/n",
            "https://169.254.1.1/n",
            "https://224.0.0.1/n",
            "http://2130706433/",  # decimal-obfuscated 127.0.0.1
            "http://0x7f.0.0.1/",  # hex-obfuscated
            "http://0177.0.0.1/",  # octal-obfuscated
            "http://127.000.0.001/",  # leading-zero obfuscation
            "http://example.com:99999/",
            "http://example.com:0/",
            "http://example.com:abc/",
            "https://exam ple.com/",
            "https://example.com/\x01",
            "https://example.com\\evil",
            "x" * 2001,
            "",
            "   ",
        ]
        for raw in bad:
            with self.subTest(url=raw[:60]):
                with self.assertRaises(ValidationError):
                    NewsSource.objects.create(
                        owner=self.owner, name="Rossz", url=raw)
        self.assertEqual(NewsSource.objects.filter(owner=self.owner).count(), 0)


class NewsSourceViewTests(TestCase):
    def setUp(self):
        self.owner = make_user("fa10-owner@pelda.hu")
        self.other = make_user("fb10-owner@pelda.hu")
        self.farm_a = Farm.objects.create(owner=self.owner, name="FarmA")
        self.farm_b = Farm.objects.create(owner=self.other, name="FarmB")
        self.member = make_user("fa10-member@pelda.hu")
        Membership.objects.create(
            farm=self.farm_a, user=self.member, role="manager", is_active=True)
        self.client.force_login(self.owner)

    def _activate(self, farm):
        r = self.client.post("/aktiv-farm/", {"farm_id": str(farm.id)})
        self.assertEqual(r.status_code, 302)

    def test_no_farm_user_can_use_collection(self):
        """User-owned, not farm-bound: works without any farm."""
        fresh = make_user("t10-nofarm@pelda.hu")
        self.client.force_login(fresh)
        self.assertEqual(self.client.get("/hirforrasok/").status_code, 200)

    def test_sidebar_link_visible(self):
        r = self.client.get("/attekintes/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("hirforrasok", r.content.decode())

    def test_future_notice_no_scan_running(self):
        b = self.client.get("/hirforrasok/").content.decode().lower()
        self.assertIn("gyűjtemény", b)
        self.assertTrue(
            "nincs" in b and ("ütemez" in b or "pásztáz" in b or "feldolgoz" in b),
            "list must state the future scan is not running",
        )

    def test_create_persists_and_redirects_to_detail(self):
        r = self.client.post("/hirforrasok/uj/", VALID)
        self.assertIn(r.status_code, (302, 303))
        NewsSource = _models()
        obj = NewsSource.objects.get(owner=self.owner)
        self.assertEqual(obj.url, "https://example.com/feed")
        r2 = self.client.get(f"/hirforrasok/{obj.pk}/")
        self.assertEqual(r2.status_code, 200)
        body = r2.content.decode()
        self.assertIn("Hírportál", body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertIn('target="_blank"', body)

    def test_user_content_escaped(self):
        NewsSource = _models()
        obj = NewsSource.objects.create(
            owner=self.owner, name="<script>alert(1)</script>",
            url="https://example.com/", notes="<b>x</b>")
        b = self.client.get(f"/hirforrasok/{obj.pk}/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", b)
        self.assertIn("&lt;script&gt;", b)

    def test_other_owner_rows_404_everywhere_no_leak(self):
        NewsSource = _models()
        foreign = NewsSource.objects.create(
            owner=self.other, name="Másé", url="https://example.com/mase")
        n0 = NewsSource.objects.count()
        self.assertEqual(
            self.client.get(f"/hirforrasok/{foreign.pk}/").status_code, 404)
        self.assertEqual(
            self.client.get(f"/hirforrasok/{foreign.pk}/szerkesztes/").status_code, 404)
        self.assertEqual(
            self.client.post(f"/hirforrasok/{foreign.pk}/szerkesztes/",
                             {"name": "Lopott", "url": "https://example.com/mase"}).status_code, 404)
        self.assertEqual(
            self.client.post(f"/hirforrasok/{foreign.pk}/torles/").status_code, 404)
        self.assertEqual(
            self.client.post(f"/hirforrasok/{foreign.pk}/allapot/").status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Másé")
        self.assertEqual(NewsSource.objects.count(), n0)
        b = self.client.get("/hirforrasok/").content.decode()
        self.assertNotIn("Másé", b)
        self.assertNotIn("mase", b)

    def test_forged_pk_404_no_row(self):
        NewsSource = _models()
        n0 = NewsSource.objects.count()
        self.assertEqual(self.client.get("/hirforrasok/999999/").status_code, 404)
        self.assertEqual(
            self.client.post("/hirforrasok/999999/torles/").status_code, 404)
        self.assertEqual(NewsSource.objects.count(), n0)

    def test_activefarm_switch_never_exposes_others(self):
        NewsSource = _models()
        NewsSource.objects.create(
            owner=self.other, name="Másé2", url="https://example.com/mase2")
        own = _mk(self.owner, name="SajatEgyedi", url="https://example.com/sajat")
        self._activate(self.farm_a)
        b = self.client.get("/hirforrasok/").content.decode()
        self.assertIn("SajatEgyedi", b)
        self.assertNotIn("Másé2", b)
        # Membership on farm A does not grant access to other's collection.
        self.client.force_login(self.member)
        self._activate(self.farm_a)
        b2 = self.client.get("/hirforrasok/").content.decode()
        self.assertNotIn("SajatEgyedi", b2)
        self.assertNotIn("Másé2", b2)
        self.assertEqual(
            self.client.get(f"/hirforrasok/{own.pk}/").status_code, 404)

    def test_duplicate_normalized_url_form_400_no_row(self):
        NewsSource = _models()
        _mk(self.owner, url="https://example.com/feed")
        n0 = NewsSource.objects.count()
        r = self.client.post("/hirforrasok/uj/", {
            "name": "Dupla", "url": "https://example.com/feed#mas",
            "notes": ""})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(NewsSource.objects.count(), n0)

    def test_invalid_url_form_400_no_row(self):
        NewsSource = _models()
        n0 = NewsSource.objects.count()
        for raw in ("javascript:alert(1)", "https://127.0.0.1/",
                    "https://user:pw@example.com/", "example.com"):
            with self.subTest(url=raw):
                r = self.client.post("/hirforrasok/uj/", {
                    "name": "Rossz", "url": raw, "notes": ""})
                self.assertEqual(r.status_code, 400)
        self.assertEqual(NewsSource.objects.count(), n0)

    def test_get_never_mutates_and_has_csrf(self):
        NewsSource = _models()
        obj = _mk(self.owner)
        n0 = NewsSource.objects.count()
        self.assertEqual(self.client.get("/hirforrasok/").status_code, 200)
        self.assertIn("csrfmiddlewaretoken",
                      self.client.get("/hirforrasok/").content.decode())
        self.assertEqual(
            self.client.get(f"/hirforrasok/{obj.pk}/torles/").status_code, 200)
        self.assertEqual(self.client.get(f"/hirforrasok/{obj.pk}/allapot/").status_code, 405)
        self.assertTrue(NewsSource.objects.filter(pk=obj.pk).exists())
        self.assertEqual(NewsSource.objects.count(), n0)

    def test_edit_and_toggle_and_delete(self):
        NewsSource = _models()
        obj = _mk(self.owner, url="https://example.com/a")
        r = self.client.post(f"/hirforrasok/{obj.pk}/szerkesztes/", {
            "name": "Átnevezve", "url": "https://example.com/b",
            "notes": "jegyzet", "is_active": "on"})
        self.assertIn(r.status_code, (302, 303))
        obj.refresh_from_db()
        self.assertEqual(obj.name, "Átnevezve")
        self.assertEqual(obj.url, "https://example.com/b")
        r = self.client.post(f"/hirforrasok/{obj.pk}/allapot/")
        self.assertIn(r.status_code, (302, 303))
        obj.refresh_from_db()
        self.assertFalse(obj.is_active)
        # Toggling back on keeps the same row.
        self.client.post(f"/hirforrasok/{obj.pk}/allapot/")
        obj.refresh_from_db()
        self.assertTrue(obj.is_active)
        r = self.client.post(f"/hirforrasok/{obj.pk}/torles/")
        self.assertIn(r.status_code, (302, 303))
        self.assertFalse(NewsSource.objects.filter(pk=obj.pk).exists())

    def test_edit_duplicate_url_rejected_keeps_original(self):
        NewsSource = _models()
        _mk(self.owner, name="Első", url="https://example.com/elso")
        second = _mk(self.owner, name="Második", url="https://example.com/masodik")
        r = self.client.post(f"/hirforrasok/{second.pk}/szerkesztes/", {
            "name": "Második", "url": "https://example.com/elso#x", "notes": ""})
        self.assertEqual(r.status_code, 400)
        second.refresh_from_db()
        self.assertEqual(second.url, "https://example.com/masodik")

    def test_anonymous_redirects_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/hirforrasok/").status_code, 302)


class NewsSourceNoNetworkTests(TestCase):
    """Static guard: the new modules must not fetch. Syntactic defense only;
    the future worker must revalidate resolved IPs/redirects (DNS rebinding)."""

    def test_no_network_imports_in_news_modules(self):
        import pathlib
        root = pathlib.Path("portal")
        mods = ["news_url.py", "news_views.py", "news_forms.py"]
        found_any = False
        for name in mods:
            p = root / name
            self.assertTrue(p.exists(), f"missing portal/{name}")
            found_any = True
            src = p.read_text(encoding="utf-8")
            for bad in ("import requests", "import urllib.request",
                        "from urllib.request", "urlopen", "import socket",
                        "import ssl", "Celery", "celery", "openai",
                        "anthropic", "feedparser"):
                self.assertNotIn(bad, src, f"{name} must not use {bad}")
            self.assertNotIn("fetch(", src.lower())
        self.assertTrue(found_any)
