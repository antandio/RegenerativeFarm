"""WP1 budgets: pre-declared maxima, reserve-before-dispatch, real ledger.

Unknown price and unknown token counts stay None (never zero): None
means "unknown", zero would falsely claim a free call.
Official Muse Spark rates (per 1M tokens, USD):
input 1.25, cached input 0.15, output 4.25. Unknown models have no
price (None) and require explicit allow_unknown_cost before dispatch.
"""
import math

from django.conf import settings
from django.utils import timezone


def _get(name, default):
    try:
        return getattr(settings, name, default)
    except Exception:
        return default


DEFAULTS = {
    # At most one first candidate plus two targeted revisions.
    "max_revisions": 2,
    # Separate ceilings per model call class.
    "max_calls": 7,
    "max_tokens": 60000,
    "max_wall_seconds": 300,
    # Configurable cost ceiling; unknown cost is None, never 0.
    "max_cost": 0.25,
    "unknown_cost": None,
    "max_output_tokens": 2000,
}

# Official Muse Spark prices per 1M tokens (USD). Only the known model
# family carries a price; anything else is None (unknown).
KNOWN_MODEL_PREFIXES = ("muse-spark",)
PRICE_INPUT_PER_M = 1.25
PRICE_CACHED_PER_M = 0.15
PRICE_OUTPUT_PER_M = 4.25


def get_limits(overrides=None):
    """Effective limits: code defaults, settings, then explicit overrides."""
    limits = dict(DEFAULTS)
    limits["max_calls"] = _get("AGENT_MAX_CALLS", limits["max_calls"])
    limits["max_tokens"] = _get("AGENT_MAX_TOKENS", limits["max_tokens"])
    limits["max_wall_seconds"] = _get(
        "AGENT_MAX_WALL_SECONDS", limits["max_wall_seconds"])
    limits["max_cost"] = _get("AGENT_MAX_COST", limits["max_cost"])
    limits["max_output_tokens"] = _get(
        "AGENT_MAX_OUTPUT_TOKENS", limits["max_output_tokens"])
    for key, value in dict(overrides or {}).items():
        if value is not None:
            limits[key] = value
    return limits


def _model_price_known(model):
    name = str(model or "")
    return any(name.startswith(p) for p in KNOWN_MODEL_PREFIXES)


def estimate_cost_usd(*, model, prompt_text="", max_output_tokens=0,
                       cached_tokens=0):
    """Conservative USD estimate, or None when the model price is unknown."""
    if not _model_price_known(model):
        return None
    prompt_tokens = max(1, int(math.ceil(
        len(str(prompt_text or "").encode("utf-8")) / 4.0)))
    total_in = prompt_tokens + int(max_output_tokens or 0)
    fresh_in = max(0, total_in - int(cached_tokens or 0))
    cost = (fresh_in * PRICE_INPUT_PER_M / 1_000_000.0
            + int(cached_tokens or 0) * PRICE_CACHED_PER_M / 1_000_000.0
            + int(max_output_tokens or 0) * PRICE_OUTPUT_PER_M
            / 1_000_000.0)
    return round(cost, 6)


def actual_cost(model, input_tokens, output_tokens, cached_tokens=None):
    """Real call cost, or None when tokens or price are unknown."""
    if not _model_price_known(model):
        return None
    if input_tokens is None or output_tokens is None:
        return None
    try:
        total_in = int(input_tokens)
        out = int(output_tokens)
    except Exception:
        return None
    cached = 0 if cached_tokens is None else int(cached_tokens or 0)
    cached = max(0, min(cached, total_in))
    fresh_in = max(0, total_in - cached)
    cost = (fresh_in * PRICE_INPUT_PER_M / 1_000_000.0
            + cached * PRICE_CACHED_PER_M / 1_000_000.0
            + out * PRICE_OUTPUT_PER_M / 1_000_000.0)
    return round(cost, 6)


def _spent(run):
    """Actual usage so far (None stays unknown, never coerced to zero)."""
    from .models import AgentCall
    calls = AgentCall.objects.filter(run=run)
    spent_calls = calls.count()
    spent_tokens = 0
    tokens_known = True
    spent_cost = 0.0
    cost_unknown = False
    for call in calls:
        for count in (call.input_tokens, call.output_tokens):
            if count is None:
                tokens_known = False
            else:
                spent_tokens += count
        if call.cost_estimate is None:
            if call.status in ("success", "reserved", "unknown"):
                cost_unknown = True
        else:
            spent_cost += call.cost_estimate
    # Fail-closed backstop: unknown token counts never read as zero.
    tokens = spent_tokens if tokens_known else max(spent_tokens, 1)
    return {
        "calls": spent_calls,
        "tokens": tokens,
        "tokens_known": tokens_known,
        "cost": spent_cost,
        "cost_unknown": cost_unknown,
    }


def _elapsed_seconds(run):
    # Budget wall starts at dispatch, not queue waiting: a long-queued
    # run keeps its full wall budget once the worker claims it.
    try:
        start = getattr(run, "dispatch_authorized_at", None) or run.created_at
        if start is None:
            return 0.0
        return max(0.0, (timezone.now() - start).total_seconds())
    except Exception:
        return 0.0


def reserve(run, kind, prompt_text="", max_output_tokens=None,
            estimated_cost=None):
    """Reserve one call before dispatch; raises when a ceiling is hit.

    Creates the ledger row in status "reserved" so every dispatch attempt
    is counted even when the worker is interrupted afterwards. One
    attempt per dispatch, no automatic retry (enforced by the caller).
    """
    from .models import AgentCall
    limits = run.budgets or get_limits()
    spent = _spent(run)
    if spent["calls"] >= int(limits.get("max_calls", 7)):
        raise BudgetExceeded("max_calls")
    if _elapsed_seconds(run) >= float(limits.get("max_wall_seconds", 300)):
        raise BudgetExceeded("max_wall")
    max_tokens = int(limits.get("max_tokens", 60000))
    cap = (int(max_output_tokens)
           if max_output_tokens is not None
           else int(limits.get("max_output_tokens", 2000)))
    prompt_tokens = max(1, int(math.ceil(
        len(str(prompt_text or "").encode("utf-8")) / 4.0)))
    allow_unknown = bool(limits.get("allow_unknown_cost", False))
    # Fail closed on unknown token usage: block on the token ceiling
    # unless legacy fake mode explicitly allows unknown cost, in which
    # case the reserved output cap still counts toward the ceiling.
    if not spent["tokens_known"] and not allow_unknown:
        raise BudgetExceeded("max_tokens")
    effective = spent["tokens"] if spent["tokens"] is not None else 0
    if effective + prompt_tokens + cap > max_tokens:
        raise BudgetExceeded("max_tokens")
    model = _get("META_MODEL", "muse-spark-1.3")
    if estimated_cost is None:
        estimated_cost = estimate_cost_usd(
            model=model, prompt_text=prompt_text, max_output_tokens=cap)
    if estimated_cost is None:
        if not allow_unknown:
            raise BudgetExceeded("unknown_cost")
    else:
        max_cost = limits.get("max_cost", 0.25)
        try:
            ceiling = float(max_cost)
        except Exception:
            ceiling = 0.25
        if spent["cost"] + float(estimated_cost) > ceiling:
            raise BudgetExceeded("max_cost")
        if spent["cost_unknown"] and not allow_unknown:
            raise BudgetExceeded("unknown_cost")
    row = AgentCall.objects.create(run=run, kind=kind, model=model)
    # Back-compat reservation size for ledger checks without migration.
    row.reserved_tokens = prompt_tokens + cap
    return row


def record_call(call, status, input_tokens=None, output_tokens=None,
                cost_estimate=None, actual_model=None, cached_tokens=None,
                **extra):
    """Record the actual outcome of a dispatched call."""
    call.status = status
    if input_tokens is not None:
        call.input_tokens = input_tokens
    if output_tokens is not None:
        call.output_tokens = output_tokens
    if cost_estimate is not None:
        call.cost_estimate = cost_estimate
    if actual_model:
        try:
            call.model = str(actual_model)
        except Exception:
            pass
    call.completed_at = timezone.now()
    call.save(update_fields=["status", "input_tokens", "output_tokens",
                             "cost_estimate", "completed_at", "model"])
    return call


def remaining_wall_seconds(run):
    limits = run.budgets or get_limits()
    try:
        cap = float(limits.get("max_wall_seconds", 300))
    except Exception:
        cap = 300.0
    return max(0.0, cap - _elapsed_seconds(run))


class BudgetExceeded(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
