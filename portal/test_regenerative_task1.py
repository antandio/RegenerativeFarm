"""Task1: canonical regenerative policy binding (portable, no external paths)."""
import hashlib
import json

import httpx
from django.test import TestCase, Client, SimpleTestCase, override_settings
from django.contrib.auth import get_user_model

from portal.provider import (
    build_messages, build_sowing_messages, MetaProviderClient,
)
from portal.chat import build_chat_messages, MetaChatClient
from portal.planning import generate_plan
from portal.tests import make_user

User = get_user_model()

BUNDLED = __import__("pathlib").Path(__file__).resolve().parent / "data" / "regenerative.md"
EXPECTED_SHA = "66e7922df7e6589915460680872311b426bc947efe7e6233a1642368523d1ae5"
# Tail of the canonical policy (last directive block); asserts no truncation.
POLICY_TAIL = "Worm counts per cubic foot"

FAKE_SETTINGS = dict(
    META_API_KEY="test-key-not-real", META_API_BASE_URL="https://api.meta.ai/v1",
    META_MODEL="muse-spark-1.3", ADVICE_MAX_OUTPUT_TOKENS=6000,
    ADVICE_MAX_INPUT_CHARS=8000,
)


def _advice_payload():
    steps = [
        {"title": "t%d" % i, "action_text": "a%d" % i, "rationale": "r%d" % i,
         "risk_note": "k%d" % i, "assumptions": [], "expert_referral": None}
        for i in range(3)
    ]
    content = json.dumps({"steps": steps, "assumptions": [],
                          "focused_questions": [], "is_general": False})
    return {"model": "muse-spark-1.3", "usage": {},
            "choices": [{"message": {"content": content}}]}


def _chat_payload():
    content = json.dumps({"answer": "ok", "actions": [], "links": []})
    return {"model": "muse-spark-1.3", "usage": {},
            "choices": [{"message": {"content": content}}]}


def _sowing_payload():
    content = json.dumps({"decision": "match", "planned_cycle_id": 7,
                          "reason_code": "crop_date_match"})
    return {"model": "muse-spark-1.3", "usage": {},
            "choices": [{"message": {"content": content}}]}


class PolicyAssetTests(TestCase):
    def test_bundled_asset_matches_expected_checksum(self):
        # Portable: no dependency on the external canonical path; the
        # orchestrator already verified canonical byte equality. The
        # immutable expected checksum pins the deployed bytes here.
        self.assertTrue(BUNDLED.exists(), "bundled canonical asset missing")
        raw = BUNDLED.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), EXPECTED_SHA)

    def test_loader_exposes_version_and_checksum(self):
        from portal import regenerative_policy as rp
        self.assertEqual(rp.POLICY_VERSION, "1.0.0")
        self.assertEqual(rp.policy_sha256(), EXPECTED_SHA)
        self.assertEqual(rp.policy_text(), BUNDLED.read_text(encoding="utf-8"))
        self.assertIn("Regenerative Agriculture System Architect Skill", rp.policy_text())


class PromptBindingTests(TestCase):
    def _assert_full_policy(self, system, lang):
        from portal import regenerative_policy as rp
        # ENTIRE policy text must travel in the system message, not headings.
        self.assertIn(rp.policy_text(), system)
        # Tail last directive proves no truncation.
        self.assertIn(POLICY_TAIL, system)
        self.assertIn("Regenerative Agriculture System Architect Skill", system)
        if lang == "hu":
            self.assertIn("feltöltött tartalom", system)
            self.assertIn("Tökéletes prompt-befolyás", system)
        else:
            self.assertIn("No perfect injection prevention", system)

    def test_advice_hu_system_carries_full_policy(self):
        system = build_messages({"talaj": "homok"}, language="hu")[0]["content"]
        self._assert_full_policy(system, "hu")
        self.assertIn("Tillage Exit Protocol", system)

    def test_advice_en_system_carries_full_policy(self):
        system = build_messages({"soil": "sand"}, language="en")[0]["content"]
        self._assert_full_policy(system, "en")
        self.assertIn("Tillage Exit Protocol", system)

    def test_sowing_system_carries_full_policy(self):
        msgs = build_sowing_messages("Buza", "2026-03-01", 7, "Buza", "2026-03-02", -1)
        self._assert_full_policy(msgs[0]["content"], "en")

    def test_chat_hu_and_en_systems_carry_full_policy(self):
        for lang in ("hu", "en"):
            msgs = build_chat_messages("Hello", "ctx", language=lang)
            self._assert_full_policy(msgs[0]["content"], lang)


@override_settings(**FAKE_SETTINGS)
class ProviderPayloadTests(SimpleTestCase):
    def _capture(self, payload):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json=payload)

        return captured, handler

    def test_advice_payload_system_carries_full_policy(self):
        from portal import regenerative_policy as rp
        captured, handler = self._capture(_advice_payload())
        client = MetaProviderClient(
            api_key="test-key-not-real",
            transport=httpx.MockTransport(handler))
        client.generate_advice({"talaj": "homok"}, language="hu")
        system = captured["body"]["messages"][0]["content"]
        self.assertIn(rp.policy_text(), system)
        self.assertIn(POLICY_TAIL, system)

    def test_sowing_payload_system_carries_full_policy(self):
        from portal import regenerative_policy as rp
        captured, handler = self._capture(_sowing_payload())
        client = MetaProviderClient(
            api_key="test-key-not-real",
            transport=httpx.MockTransport(handler))
        client.generate_sowing_comparison(
            "Buza", "2026-03-01", 7, "Buza", "2026-03-02", -1)
        system = captured["body"]["messages"][0]["content"]
        self.assertIn(rp.policy_text(), system)
        self.assertIn(POLICY_TAIL, system)

    def test_chat_payload_system_carries_full_policy(self):
        from portal import regenerative_policy as rp
        captured, handler = self._capture(_chat_payload())
        client = MetaChatClient(
            api_key="test-key-not-real",
            transport=httpx.MockTransport(handler))
        client.generate_chat("Hello", context="ctx", language="en")
        system = captured["body"]["messages"][0]["content"]
        self.assertIn(rp.policy_text(), system)
        self.assertIn(POLICY_TAIL, system)


class GuideViewTests(TestCase):
    def setUp(self):
        from portal.models import Farm
        self.u = make_user()
        self.farm = Farm.objects.create(owner=self.u, name="Tesztfarm")
        self.c = Client()
        self.url = "/regenerativ-elvek/"

    def test_unauth_redirects(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)

    def test_auth_renders_full_safe_policy_with_version(self):
        self.c.force_login(self.u)
        r = self.c.get(self.url)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Regenerative Agriculture", body)
        self.assertIn("1.0.0", body)
        self.assertIn(EXPECTED_SHA[:16], body)
        # read-only: no edit form or POST endpoint advertised
        self.assertNotIn("szerkesztes", body.lower())
        # Bounded parse: stop at the closing div of the policy region, so
        # page chrome (its own legitimate scripts/forms) stays excluded.
        # The canonical policy markup itself carries no div tags.
        region = body.split('<div class="policy-body">', 1)[1].split("</div>", 1)[0]
        self.assertNotIn("<script", region)
        self.assertNotIn("<form", region.lower())
        self.assertNotIn("javascript:", region)

    def test_post_is_read_only_405(self):
        self.c.force_login(self.u)
        r = self.c.post(self.url, {})
        self.assertEqual(r.status_code, 405)

    def test_menu_labels_hu_en(self):
        self.c.force_login(self.u)
        hu = self.c.get(self.url, HTTP_ACCEPT_LANGUAGE="hu").content.decode()
        self.assertIn("Regenerat", hu)
        self.c.post("/i18n/setlang/", {"language": "en", "next": self.url})
        en = self.c.get(self.url).content.decode()
        self.assertIn("Regenerative", en)


class PlanningReferenceTests(TestCase):
    def test_summary_carries_policy_reference(self):
        from datetime import date
        c = {"id": 1, "property_id": 10, "crop_name": "Buza", "state": "current",
             "growth_stage": "", "sow_date": date(2026, 3, 1),
             "expected_end_date": date(2026, 7, 1), "observations": "",
             "area_hectares": "1"}
        _, summary = generate_plan([c], [])
        self.assertEqual(summary.get("regenerative_policy_version"), "1.0.0")
        self.assertEqual(summary.get("regenerative_policy_sha256"), EXPECTED_SHA)
        self.assertTrue(summary.get("regenerative_policy_scope"))
        items, _ = generate_plan([c], [])
        self.assertTrue(any("1.0.0" in (i.get("guidance") or "") for i in items))


class SanitizerTests(TestCase):
    def test_renderer_strips_xss_and_remote_resources(self):
        from portal import regenerative_policy as rp
        evil = ('<script>alert(1)</script><p onclick="x()">Hi</p>'
                '<a href="javascript:alert(2)">click</a>'
                '<img src="https://tracker.example/pixel.png">'
                '<a href="https://example.com/info">ext</a>')
        html = rp.render_safe_markdown(evil)
        self.assertNotIn("<script", html)
        self.assertNotIn("onclick", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("tracker.example", html)
        self.assertIn('rel="noopener noreferrer nofollow"', html)
