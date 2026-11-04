import json as _json
import httpx as _httpx
from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.utils import translation
from .models import Farm, AdviceRun, AdviceStep, ConsentEvent, FarmAction
from .services import request_advice
from .provider import MetaProviderClient, build_messages
from .tests import make_user, grant, good_payload

User = get_user_model()

class CapturingClient:
    def __init__(self, payload=None):
        self.payload = payload or good_payload(); self.calls = 0; self.profiles = []; self.languages = []
    def generate_advice(self, profile, language=None):
        self.calls += 1; self.profiles.append(dict(profile)); self.languages.append(language)
        return self.payload

def switch(client, lang, next_url="/belepes/"):
    return client.post("/i18n/setlang/", {"language": lang, "next": next_url})

class AnonLanguageTests(TestCase):
    def test_default_hungarian_login_and_topbar(self):
        r = self.client.get("/belepes/")
        b = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('<html lang="hu">', b)
        self.assertIn("Bejelentkezés", b); self.assertIn("Magyar", b); self.assertIn("English", b)
        self.assertIn("/i18n/setlang/", b)
    def test_switch_to_english_login_no_hungarian_labels(self):
        r = switch(self.client, "en")
        self.assertEqual(r.status_code, 302); self.assertEqual(r.url, "/belepes/")
        self.assertEqual(self.client.cookies["django_language"].value, "en")
        r = self.client.get("/belepes/"); b = r.content.decode()
        self.assertIn('<html lang="en">', b); self.assertIn("Log in", b)
        self.assertNotIn("Bejelentkezés", b); self.assertNotIn("Belépés", b)
        self.assertNotIn("E-mail cím", b)
    def test_switch_back_to_hungarian(self):
        switch(self.client, "en"); r = switch(self.client, "hu")
        self.assertEqual(self.client.cookies["django_language"].value, "hu")
        b = self.client.get("/belepes/").content.decode()
        self.assertIn('<html lang="hu">', b); self.assertIn("Bejelentkezés", b)
    def test_cookie_persists_across_anon_pages_english(self):
        switch(self.client, "en")
        for url, en_text in [("/belepes/", "Log in"), ("/regisztracio/", "Registration"), ("/tudnivalok/", "Information")]:
            r = self.client.get(url); b = r.content.decode()
            self.assertIn('<html lang="en">', b, url); self.assertIn(en_text, b, url)
        b = self.client.get("/regisztracio/").content.decode()
        self.assertNotIn("Regisztráció", b); self.assertNotIn("Jelszó újra", b)
        b = self.client.get("/tudnivalok/").content.decode()
        self.assertNotIn("Tájékoztatók", b); self.assertNotIn("Felhőadat-kezelési nyilatkozat", b)
    def test_invalid_language_not_stored(self):
        r = switch(self.client, "fr")
        self.assertEqual(r.status_code, 302)
        c = self.client.cookies.get("django_language")
        self.assertTrue(c is None or c.value not in ("fr",))
        b = self.client.get("/belepes/").content.decode()
        self.assertIn("Bejelentkezés", b)
    def test_set_language_requires_csrf(self):
        c = Client(enforce_csrf_checks=True)
        r = c.post("/i18n/setlang/", {"language": "en", "next": "/belepes/"})
        self.assertEqual(r.status_code, 403)
    def test_external_next_not_open_redirect(self):
        r = switch(self.client, "en", next_url="https://evil.example/phish")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.example", r.url)
        self.assertTrue(r.url.startswith("/"), r.url)
    def test_protocol_relative_next_not_open_redirect(self):
        r = switch(self.client, "en", next_url="//evil.example/phish")
        self.assertNotIn("evil.example", r.url)

class AuthLanguageTests(TestCase):
    def setUp(self):
        self.u = make_user("lang@pelda.hu"); self.farm = Farm.objects.create(owner=self.u, name="Eredeti farmnév", soil_notes="Homokos talaj eredeti szöveg")
        self.client.force_login(self.u)
    def test_english_auth_pages_headings_no_hungarian_nav(self):
        switch(self.client, "en", next_url="/attekintes/")
        cases = [("/attekintes/", "Overview"), ("/gazdasagom/", "My farm"), ("/tanacs/keres/", "Request advice"), ("/teendok/", "Tasks"), ("/tanacsok/", "History"), ("/fiok/", "Account and privacy")]
        for url, en_text in cases:
            r = self.client.get(url); b = r.content.decode()
            self.assertEqual(r.status_code, 200, url); self.assertIn('<html lang="en">', b, url); self.assertIn(en_text, b, url)
            for hu in ["Áttekintés", "Gazdaságom", "Tanács kérése", "Teendők", "Előzmények"]:
                self.assertNotIn(hu, b, f"{hu} on {url}")
    def test_language_persists_through_login(self):
        c = Client(); switch(c, "en")
        r = c.post("/belepes/", {"email": "lang@pelda.hu", "password": "TesztJelszo123!"})
        self.assertEqual(r.status_code, 302)
        b = c.get("/attekintes/").content.decode()
        self.assertIn('<html lang="en">', b); self.assertIn("Overview", b)
    def test_switching_grants_no_consent_and_changes_nothing(self):
        before = (self.farm.name, self.farm.profile_version, self.farm.soil_notes)
        switch(self.client, "en", next_url="/attekintes/")
        switch(self.client, "hu", next_url="/attekintes/")
        self.farm.refresh_from_db()
        self.assertEqual((self.farm.name, self.farm.profile_version, self.farm.soil_notes), before)
        self.assertEqual(ConsentEvent.objects.count(), 0); self.assertEqual(AdviceRun.objects.count(), 0)
    def test_stored_content_not_translated_and_note_shown_en(self):
        grant(self.farm, self.u)
        with translation.override("hu"):
            run = request_advice(self.u, self.farm, "lang-stored-1", client=CapturingClient())
        switch(self.client, "en", next_url="/attekintes/")
        b = self.client.get(f"/tanacsok/{run.id}/").content.decode()
        self.assertIn("Lépés 1", b)  # stored step title unchanged
        self.assertIn("Saved content remains in its original language", b)
        self.farm.refresh_from_db(); self.assertEqual(self.farm.soil_notes, "Homokos talaj eredeti szöveg")
        run.refresh_from_db(); self.assertEqual(run.steps.first().title, "Lépés 1")
    def test_throttle_message_english(self):
        # 5 failures then correct attempt -> throttled 429 with English safe message
        self.client.post("/kilepes/")
        switch(self.client, "en")
        for _ in range(5):
            self.client.post("/belepes/", {"email": "lang@pelda.hu", "password": "rossz-jelszo-123"})
        r = self.client.post("/belepes/", {"email": "lang@pelda.hu", "password": "TesztJelszo123!"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many failed attempts", r.content.decode())

class AdviceLanguageTests(TestCase):
    def setUp(self):
        self.u = make_user("advlang@pelda.hu"); self.farm = Farm.objects.create(owner=self.u, name="Nyelvfarm", soil_notes="Homok", water_notes="Kút")
        grant(self.farm, self.u)
    def test_advicerun_language_default_hu(self):
        run = request_advice(self.u, self.farm, "lang-def", client=CapturingClient())
        self.assertEqual(run.language, "hu")
    def test_future_advice_en_prompt_locale_captured(self):
        cap = CapturingClient()
        with translation.override("en"):
            run = request_advice(self.u, self.farm, "lang-en", client=cap)
        self.assertEqual(run.status, "success"); self.assertEqual(run.language, "en")
        seen = (cap.languages, cap.profiles)
        blob = str(seen)
        self.assertTrue("en" in blob)
        msgs = build_messages(cap.profiles[0], language=cap.languages[0] or run.language)
        self.assertIn("English", msgs[0]["content"])
    def test_idempotent_replay_cannot_change_language(self):
        cap = CapturingClient()
        with translation.override("hu"):
            r1 = request_advice(self.u, self.farm, "lang-replay", client=cap)
        with translation.override("en"):
            r2 = request_advice(self.u, self.farm, "lang-replay", client=cap)
        self.assertEqual(r1.pk, r2.pk); self.assertEqual(cap.calls, 1); self.assertEqual(r2.language, "hu")
    def test_provider_payload_requests_english_via_mock_transport(self):
        captured = {}
        def handler(request):
            captured["body"] = _json.loads(request.content.decode())
            return _httpx.Response(200, json={"model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 2}, "choices": [{"message": {"content": _json.dumps({"steps": good_payload()["steps"], "assumptions": [], "focused_questions": [], "is_general": False})}}]})
        client = MetaProviderClient(api_key="test-key", transport=_httpx.MockTransport(handler))
        client.generate_advice({"name": "Teszt"}, language="en")
        blob = _json.dumps(captured["body"], ensure_ascii=False)
        self.assertIn("English", blob)
        captured.clear()
        client.generate_advice({"name": "Teszt"}, language="hu")
        blob = _json.dumps(captured["body"], ensure_ascii=False)
        self.assertIn("magyarul", blob.lower())

class VisibleLabelRegressionTests(TestCase):
    def setUp(self):
        self.u = make_user("labels@pelda.hu")
        self.farm = Farm.objects.create(owner=self.u, name="Label farm", soil_notes="Sand")
        self.client.force_login(self.u)
        from .models import FarmProfileSnapshot, FarmAction
        snap = FarmProfileSnapshot.objects.create(farm=self.farm, profile_version=1, profile_json=self.farm.profile_dict())
        for st in ("pending","success","failed"):
            AdviceRun.objects.create(farm=self.farm, initiated_by=self.u, profile_snapshot=snap, idempotency_key=f"lbl-{st}", status=st, requested_model="m")
        FarmAction.objects.create(farm=self.farm, text="Open task text", status="open")
        FarmAction.objects.create(farm=self.farm, text="Done task text", status="done")
        switch(self.client, "en", next_url="/attekintes/")
    def test_missing_field_names_translated_not_raw_or_hu(self):
        b = self.client.get("/attekintes/").content.decode()
        for raw in ("region_text","climate_text","water_notes","crops_livestock_notes"):
            self.assertNotIn(f"({raw})", b)
        self.assertIn("Region", b)
        b = self.client.get("/gazdasagom/").content.decode()
        gaps = b.split("Current gaps:")[-1].split("</p>")[0]
        for raw in ("region_text","climate_text","water_notes"):
            self.assertNotIn(raw, gaps)
        self.assertIn("Region", gaps)
    def test_status_labels_english_no_hu(self):
        b = self.client.get("/tanacsok/").content.decode()
        for en in ("pending","successful","failed"):
            self.assertIn(en, b)
        for hu in ("függőben","sikeres","sikertelen"):
            self.assertNotIn(hu, b)
        b = self.client.get("/teendok/").content.decode()
        self.assertIn("Open", b); self.assertIn("Done", b)
        self.assertNotIn("Nyitott", b); self.assertNotIn("Kész", b)
    def test_model_choice_labels_english(self):
        from django.utils import translation
        with translation.override("en"):
            self.assertEqual(str(AdviceRun._meta.get_field("status").choices[0][1]), "pending")
            self.assertEqual(str(AdviceRun._meta.get_field("status").choices[1][1]), "successful")
        # visible choice must not resolve to Hungarian under en
        with translation.override("en"):
            labels = [str(v) for _, v in FarmAction._meta.get_field("status").choices]
            self.assertNotIn("Nyitott", labels); self.assertNotIn("Kész", labels)
    def test_default_farm_name_translation_not_fuzzy(self):
        import gettext
        t = gettext.translation("django", localedir="locale", languages=["en"], fallback=False)
        self.assertEqual(t.gettext("Saját gazdaságom"), "My farm")
