"""Live-protocol budget tests: real AgentClient over httpx.MockTransport.

Synthetic fixtures only (no real farm data), no network. RED->GREEN for
the cost-None live blocker: every call must persist a non-None cost so a
second reserve never raises unknown_cost; unknown usage must fail closed
on the token ceiling; a timeout records one unknown row with no retry.
"""
import json

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase

from portal.agent import budgets
from portal.agent.client import AgentClient
from portal.agent.models import AgentCall, AgentRun
from portal.models import ConsentEvent, Farm

User = get_user_model()
RATES_URL = "https://dev.meta.ai/products/muse-code"


def _user(email="elo@pelda.hu"):
    return User.objects.create_user(email=email, password="TesztJelszo123!")


def _farm(user):
    return Farm.objects.create(owner=user, name="Szintetikus",
                               region_text="Sehol")


def _run(user, farm):
    ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted")
    return AgentRun.objects.create(
        farm=farm, initiated_by=user, idempotency_key="live-key-1",
        payload_hash="0" * 64, goal="Szintetikus tesztcel")


def _ok_payload(kind):
    if kind == "plan":
        content = {"agricultural_goal": "g", "summary": "s", "steps": [],
                   "risk": "low", "unknowns": []}
    elif kind == "critic":
        content = {"verdict": "verified", "findings": []}
    else:
        content = {"decision": "agri"}
    return {
        "model": "muse-spark-1.3",
        "choices": [{"message": {"content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40,
                  "prompt_tokens_details": {"cached_tokens": 20}},
    }


def _transport(payloads, calls):
    def handler(request):
        calls.append(json.loads(request.content.decode("utf-8")))
        if isinstance(payloads, Exception):
            raise payloads
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


class LiveCostLedgerTests(TestCase):
    def test_three_calls_record_actual_cost_no_unknown_block(self):
        user, farm, run = _user(), None, None
        farm = _farm(user)
        run = _run(user, farm)
        run.budgets = budgets.get_limits()
        calls = []
        kinds = ["plan", "critic", "classify"]
        client = AgentClient(api_key="t", transport=_transport(
            [_ok_payload("plan"), _ok_payload("critic"),
             _ok_payload("classify")], calls))
        limits = budgets.get_limits()
        results = [
            client.plan([{"role": "user", "content": "terv"}], limits),
            client.critique([{"role": "user", "content": "biralat"}],
                            limits),
            client.classify_agri_call("buza vetes"),
        ]
        for kind, result in zip(kinds, results):
            self.assertIsNotNone(result["input_tokens"], kind)
            self.assertIsNotNone(result["output_tokens"], kind)
            self.assertIsNotNone(result["cost_estimate"], kind)
            self.assertEqual(result["actual_model"], "muse-spark-1.3")
            row = budgets.reserve(run, kind, prompt_text="x" * 100,
                                  max_output_tokens=200)
            budgets.record_call(
                row, "success",
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                cost_estimate=result["cost_estimate"],
                actual_model=result.get("actual_model"),
                cached_tokens=result.get("cached_tokens"))
        spent = budgets._spent(run)
        self.assertFalse(spent["cost_unknown"])
        self.assertGreater(spent["cost"], 0)
        # The live blocker: a fourth reserve must not raise unknown_cost.
        try:
            budgets.reserve(run, "critic", prompt_text="x" * 100,
                            max_output_tokens=200)
        except budgets.BudgetExceeded as exc:
            self.assertNotEqual(exc.code, "unknown_cost")
        self.assertEqual(len(calls), 3)

    def test_unknown_usage_fails_closed_on_token_ceiling(self):
        user = _user(email="masik@pelda.hu")
        farm, run = _farm(user), None
        run = _run(user, farm)
        payload = _ok_payload("plan")
        del payload["usage"]
        calls = []
        client = AgentClient(api_key="t",
                             transport=_transport([payload], calls))
        result = client.plan([{"role": "user", "content": "terv"}],
                             budgets.get_limits())
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["cost_estimate"])
        row = budgets.reserve(run, "planner", prompt_text="x" * 500,
                              max_output_tokens=200)
        self.assertIsNotNone(row.reserved_tokens)
        budgets.record_call(row, "success",
                            input_tokens=result["input_tokens"],
                            output_tokens=result["output_tokens"],
                            cost_estimate=result["cost_estimate"])
        spent = budgets._spent(run)
        # Reservation backstop counts: unknown usage is never zero.
        self.assertIsNotNone(spent["tokens"])
        self.assertGreater(spent["tokens"], 0)
        tiny = budgets.get_limits({"max_tokens": 10})
        run.budgets = tiny
        run.save(update_fields=["budgets"])
        with self.assertRaises(budgets.BudgetExceeded) as ctx:
            budgets.reserve(run, "critic", prompt_text="y" * 500,
                            max_output_tokens=200)
        self.assertEqual(ctx.exception.code, "max_tokens")

    def test_timeout_records_unknown_once_without_retry(self):
        from portal.provider import ProviderTimeout
        user = _user(email="harmadik@pelda.hu")
        farm = _farm(user)
        run = _run(user, farm)
        calls = []
        transport = _transport(
            httpx.TimeoutException("slow", request=None), calls)
        client = AgentClient(api_key="t", transport=transport)
        row = budgets.reserve(run, "planner", prompt_text="x" * 100,
                              max_output_tokens=200)
        with self.assertRaises(ProviderTimeout):
            client.plan([{"role": "user", "content": "terv"}],
                        budgets.get_limits())
        budgets.record_call(row, "unknown")
        row.refresh_from_db()
        self.assertEqual(row.status, "unknown")
        self.assertIsNone(row.cost_estimate)
        self.assertEqual(len(calls), 1)
