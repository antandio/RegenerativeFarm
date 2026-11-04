"""Advice provider phase-timeout regression (vertical TDD, RED first).

English comments per project convention; user-visible strings stay HU.
Real transport path only: httpx.MockTransport, no network, no paid calls.
Root evidence echoed: real failed advice elapsed 30.1s on effective 30s
setting; same-shape synthetic Spark6000 response 25.436s.
"""
import json
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings

from portal.provider import MetaProviderClient, ProviderTimeout

FAKE_KEY = "test-fake-key-not-real"
FAKE_BASE = "https://api.meta.ai/v1"
FAKE_MODEL = "muse-spark-1.3"

SECRET_KEY_SENTINEL = "KEY_SENTINEL_9f8e7d6c5b"
PROMPT_SENTINEL = "PROMPT_SENTINEL_q1w2e3r4"
EXC_TEXT_SENTINEL = "EXC_SENTINEL_z9x8c7v6"
RESPONSE_SENTINEL = "RESP_SENTINEL_a1b2c3d4"


def valid_advice_body():
    steps = [
        {"title": "t%d" % i, "action_text": "a%d" % i,
         "rationale": "r%d %s" % (i, RESPONSE_SENTINEL) if i == 0 else "r%d" % i,
         "risk_note": "k%d" % i, "assumptions": [], "expert_referral": None}
        for i in range(3)
    ]
    content = json.dumps({"steps": steps, "assumptions": [],
                          "focused_questions": [], "is_general": False})
    return {"model": FAKE_MODEL, "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            "choices": [{"message": {"content": content}}]}


def handler_with_capture(captured):
    def handler(request):
        captured["request"] = request
        captured["timeout_ext"] = request.extensions.get("timeout")
        captured["body"] = json.loads(request.content.decode())
        captured["calls"] = captured.get("calls", 0) + 1
        return httpx.Response(200, json=valid_advice_body())
    return handler


def raising_handler(exc_factory, captured):
    def handler(request):
        captured["request"] = request
        captured["calls"] = captured.get("calls", 0) + 1
        raise exc_factory(request)
    return handler


@override_settings(META_API_KEY=FAKE_KEY, META_API_BASE_URL=FAKE_BASE,
                   META_MODEL=FAKE_MODEL, ADVICE_MAX_OUTPUT_TOKENS=6000,
                   ADVICE_MAX_INPUT_CHARS=8000)
class AdvicePhaseTimeoutTests(SimpleTestCase):
    def test_default_client_uses_phase_timeouts(self):
        captured = {}
        with override_settings(ADVICE_REQUEST_TIMEOUT_SECONDS=120.0):
            client = MetaProviderClient(
                transport=httpx.MockTransport(handler_with_capture(captured)))
            self.assertEqual(client.timeout, 120.0)
            out = client.generate_advice({"farm": "adat"})
        self.assertEqual(len(out["steps"]), 3)
        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["body"]["max_tokens"], 6000)
        ext = captured["timeout_ext"]
        self.assertEqual(ext, {"connect": 10.0, "read": 120.0,
                               "write": 10.0, "pool": 10.0})

    def test_explicit_timeout_override_read(self):
        captured = {}
        client = MetaProviderClient(
            timeout=75.0,
            transport=httpx.MockTransport(handler_with_capture(captured)))
        self.assertEqual(client.timeout, 75.0)
        client.generate_advice({"farm": "adat"})
        ext = captured["timeout_ext"]
        self.assertEqual(ext, {"connect": 10.0, "read": 75.0,
                               "write": 10.0, "pool": 10.0})

    def test_runtime_default_timeout_is_120(self):
        from config.runtime import load_runtime_config
        r = load_runtime_config(config_path="/nonexistent-dir-xyz/portal-config.toml",
                                environ={}, dedicated_environ={})
        self.assertEqual(r["ADVICE_REQUEST_TIMEOUT_SECONDS"], 120.0)

    def _timeout_case(self, exc_class, expected_type, elapsed_pair):
        captured = {}
        # Use the secret sentinel as the actual client key so the
        # non-leak assertion below is non-vacuous.
        with override_settings(ADVICE_REQUEST_TIMEOUT_SECONDS=120.0,
                               META_API_KEY=SECRET_KEY_SENTINEL):
            client = MetaProviderClient(
                transport=httpx.MockTransport(
                    raising_handler(lambda req: exc_class(EXC_TEXT_SENTINEL, request=req),
                                    captured)))
            with patch("portal.provider.time.monotonic",
                        side_effect=[elapsed_pair[0], elapsed_pair[1]]):
                with self.assertLogs("portal.provider", level="WARNING") as logs:
                    with self.assertRaises(ProviderTimeout) as ctx:
                        client.generate_advice({"field": PROMPT_SENTINEL})
        self.assertEqual(ctx.exception.code, "provider_timeout")
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertNotIn(EXC_TEXT_SENTINEL, str(ctx.exception))
        self.assertNotIn(SECRET_KEY_SENTINEL, str(ctx.exception))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertEqual(captured["calls"], 1)
        text = "\n".join(logs.output)
        self.assertIn("event=advice_provider_timeout", text)
        self.assertIn("timeout_type=%s" % expected_type, text)
        self.assertIn("read_timeout=120", text)
        self.assertNotIn(EXC_TEXT_SENTINEL, text)
        self.assertNotIn(PROMPT_SENTINEL, text)
        self.assertNotIn(SECRET_KEY_SENTINEL, text)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("Traceback", text)
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertFalse(record.exc_text)
            self.assertFalse(record.stack_info)
        return text

    def test_timeout_subtypes_logged_sanitized(self):
        cases = [(httpx.ConnectTimeout, "ConnectTimeout"),
                 (httpx.ReadTimeout, "ReadTimeout"),
                 (httpx.WriteTimeout, "WriteTimeout"),
                 (httpx.PoolTimeout, "PoolTimeout"),
                 (httpx.TimeoutException, "TimeoutException")]
        for exc_class, expected in cases:
            with self.subTest(exc=expected):
                text = self._timeout_case(exc_class, expected, (100.0, 130.1))
                self.assertIn("elapsed_seconds=30.1", text)

    def test_malicious_custom_timeout_subclass_falls_back(self):
        class SneakyTimeout(httpx.ReadTimeout):
            pass
        with override_settings(ADVICE_REQUEST_TIMEOUT_SECONDS=120.0):
            captured = {}
            client = MetaProviderClient(
                transport=httpx.MockTransport(
                    raising_handler(lambda req: SneakyTimeout(EXC_TEXT_SENTINEL, request=req),
                                    captured)))
            with self.assertLogs("portal.provider", level="WARNING") as logs:
                with self.assertRaises(ProviderTimeout):
                    client.generate_advice({"field": PROMPT_SENTINEL})
        text = "\n".join(logs.output)
        self.assertIn("timeout_type=TimeoutException", text)
        self.assertNotIn("SneakyTimeout", text)
        self.assertNotIn(EXC_TEXT_SENTINEL, text)

    def test_success_logs_sanitized_timing_metadata(self):
        captured = {}
        # Sentinel is the live key here, so its absence from logs is proven.
        with override_settings(ADVICE_REQUEST_TIMEOUT_SECONDS=120.0,
                               META_API_KEY=SECRET_KEY_SENTINEL):
            client = MetaProviderClient(
                transport=httpx.MockTransport(handler_with_capture(captured)))
            with patch("portal.provider.time.monotonic",
                        side_effect=[50.0, 75.436]):
                with self.assertLogs("portal.provider", level="INFO") as logs:
                    out = client.generate_advice({"field": PROMPT_SENTINEL})
        self.assertEqual(len(out["steps"]), 3)
        text = "\n".join(logs.output)
        self.assertIn("event=advice_provider_response", text)
        self.assertIn("elapsed_seconds=25.436", text)
        self.assertIn("status_code=200", text)
        self.assertNotIn(PROMPT_SENTINEL, text)
        self.assertNotIn(SECRET_KEY_SENTINEL, text)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(RESPONSE_SENTINEL, text)
        self.assertNotIn("Traceback", text)
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertFalse(record.exc_text)
            self.assertFalse(record.stack_info)

    def test_sowing_path_unchanged(self):
        captured = {}
        with override_settings(ADVICE_REQUEST_TIMEOUT_SECONDS=120.0):
            from portal.provider import SOWING_MAX_OUTPUT_TOKENS  # noqa: F401

            def sow_handler(request):
                captured["timeout_ext"] = request.extensions.get("timeout")
                captured["body"] = json.loads(request.content.decode())
                content = json.dumps({"decision": "match", "planned_cycle_id": 7,
                                      "reason_code": "crop_date_match"})
                return httpx.Response(200, json={
                    "model": FAKE_MODEL,
                    "choices": [{"message": {"content": content}}]})

            client = MetaProviderClient(
                transport=httpx.MockTransport(sow_handler))
            out = client.generate_sowing_comparison(
                "buza", "2026-10-01", 7, "buza", "2026-10-02", 1)
        self.assertEqual(out["decision"], "match")
        ext = captured["timeout_ext"]
        self.assertEqual(ext["connect"], ext["read"])
        self.assertEqual(ext["read"], ext["write"])
        self.assertEqual(ext["write"], ext["pool"])
