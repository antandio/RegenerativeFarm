"""Structured candidate schema plus deterministic validation.

The candidate carries evidence (source IDs from the manifest), the
related policy section and uncertainties per step. Commands are typed
and checked for shape here; WP1 never executes them (the WP2 executor
hooks in later). No domain writes happen in this slice.
"""
from portal.safety import screen_parsed

_CRITIC_VERDICTS = frozenset({"verified", "needs_revision", "blocked"})
_CRITIC_SEVERITIES = frozenset({"blocked", "critical", "revision", "info"})


def validate_critic_output(parsed):
    """Strict critic dict schema: verdict + findings(severity/step/code)."""
    from portal.provider import ProviderInvalidOutput
    if not isinstance(parsed, dict):
        raise ProviderInvalidOutput("A válasz nem érvényes JSON.")
    if parsed.get("verdict") not in _CRITIC_VERDICTS:
        raise ProviderInvalidOutput("Érvénytelen critic verdict.")
    findings = parsed.get("findings")
    if not isinstance(findings, list):
        raise ProviderInvalidOutput("Érvénytelen critic findings.")
    for entry in findings:
        if not isinstance(entry, dict):
            raise ProviderInvalidOutput("Érvénytelen critic finding.")
        if entry.get("severity") not in _CRITIC_SEVERITIES:
            raise ProviderInvalidOutput("Érvénytelen finding severity.")
        if not isinstance(entry.get("step"), int):
            raise ProviderInvalidOutput("Érvénytelen finding step.")
        if not str(entry.get("code") or "").strip():
            raise ProviderInvalidOutput("Érvénytelen finding code.")
        if not str(entry.get("reason") or "").strip():
            raise ProviderInvalidOutput("Érvénytelen finding reason.")
    return {"verdict": parsed["verdict"], "findings": findings}


COMMAND_TYPES = frozenset({
    "farm", "extra_field", "location", "property", "equipment",
    "equipment_property", "cycle", "observation", "sowing_review",
    "action", "work_item", "plan_revision", "farming_event",
    "livestock_group", "livestock_event", "livestock_work",
    "external_contact", "membership", "invitation", "rule_category",
    "rule_definition", "rule_activation", "document", "farm_document",
    "news_source", "conversation", "message", "agent_run",
})
COMMAND_OPS = frozenset({"create", "update", "delete", "archive"})


def _is_step(value):
    return isinstance(value, dict)


def validate_candidate(candidate, manifest):
    """Return a list of error strings; empty means structurally valid."""
    errors = []
    if not isinstance(candidate, dict):
        return ["candidate must be a JSON object"]
    if not str(candidate.get("agricultural_goal") or "").strip():
        errors.append("agricultural_goal is required")
    if not str(candidate.get("summary") or "").strip():
        errors.append("summary is required")
    steps = candidate.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
        steps = []
    known = {str(i.get("source_id")) for i in (manifest or {}).get("items", [])
             if isinstance(i, dict) and i.get("source_id")}
    for pos, step in enumerate(steps, start=1):
        if not _is_step(step):
            errors.append(f"step {pos}: must be an object")
            continue
        for field in ("title", "action_text", "rationale"):
            if not str(step.get(field) or "").strip():
                errors.append(f"step {pos}: {field} is required")
        evidence = step.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"step {pos}: evidence is required")
            evidence = []
        for item in evidence:
            if not isinstance(item, dict):
                errors.append(f"step {pos}: evidence entry malformed")
                continue
            sid = str(item.get("source_id") or "")
            if not sid:
                errors.append(f"step {pos}: evidence source_id required")
            elif sid not in known:
                errors.append(
                    f"step {pos}: evidence {sid} not in the manifest")
            if not str(item.get("policy_section") or "").strip():
                errors.append(
                    f"step {pos}: evidence policy_section required")
            if "uncertainty" not in item:
                errors.append(f"step {pos}: evidence uncertainty required")
        if "uncertainties" not in step:
            errors.append(f"step {pos}: uncertainties required")
        for command in step.get("commands") or []:
            errors.extend(_check_command(pos, command))
    if candidate.get("risk") not in ("low", "medium", "high"):
        errors.append("risk must be low, medium or high")
    if not isinstance(candidate.get("unknowns"), list):
        errors.append("unknowns must be a list")
    try:
        if screen_parsed(candidate):
            errors.append("prohibited content detected by safety screen")
    except Exception:
        errors.append("safety screen could not run")
    return errors


def _check_command(pos, command):
    if not isinstance(command, dict):
        return [f"step {pos}: command must be an object"]
    target = str(command.get("target") or "")
    op = str(command.get("op") or "")
    problems = []
    if target not in COMMAND_TYPES:
        problems.append(f"step {pos}: unknown command target {target!r}")
    if op not in COMMAND_OPS:
        problems.append(f"step {pos}: unknown command op {op!r}")
    fields = command.get("fields")
    if op in ("delete", "archive") and fields is None:
        pass
    elif not isinstance(fields, dict):
        problems.append(f"step {pos}: command fields must be an object")
    return problems
