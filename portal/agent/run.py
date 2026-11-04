"""Bounded run state machine: stages 1-7, max 1+2 revisions, final critic.

No domain writes in this slice: only AgentRun/AgentCall rows change.
All model HTTP happens outside DB transactions. A reserved call that
never returns a usable response is ledgered "unknown", never zero.
"""
import hashlib
import json

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import agri_gate, budgets, context as ctx
from . import critic as critic_mod
from . import policy_gate, schemas
from .budgets import BudgetExceeded
from .client import (AgentClient, build_critic_messages,
                     build_planner_messages)
from .models import AgentCall, AgentRun
from portal.provider import ProviderError, ProviderTimeout


class _DeletedStop(Exception):
    pass


class _NeedsRebase(Exception):
    pass


def _payload_hash(farm, actor, goal, doc_ids, language, granted_ids=()):
    blob = json.dumps({
        "farm": farm.pk, "actor": actor.pk, "goal": str(goal or ""),
        "docs": sorted(int(d) for d in doc_ids or []),
        "granted": sorted(int(d) for d in granted_ids or []),
        "language": language or "hu",
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _candidate_hash(candidate):
    blob = json.dumps(candidate or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stable_sources(manifest):
    """Stable identity: source_id/hash/revision only, no timestamps."""
    out = []
    for item in (manifest or {}).get("items", []):
        if not isinstance(item, dict):
            continue
        out.append((str(item.get("source_id") or ""),
                    str(item.get("hash") or ""),
                    str(item.get("revision") or "")))
    return sorted(out)


def fingerprint(run):
    """Revalidation fingerprint for later apply gates (WP2)."""
    try:
        revalidation = run.revalidation or {}
    except Exception:
        revalidation = {}
    return {
        "run_id": run.pk,
        "farm_id": run.farm_id,
        "payload_hash": run.payload_hash,
        "policy_sha256": run.policy_sha256,
        "consent_event_id": revalidation.get("consent_event_id"),
        "candidate_sha256": _candidate_hash(run.candidate),
        "source_hashes": [i.get("source_id") + ":" + i.get("hash", "")
                          for i in (run.context_manifest or {}).get(
                              "items", [])],
    }


def _fresh_actor_status(farm, actor):
    """Fresh actor check from live rows: active user + farm role."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        fresh = User.objects.filter(pk=actor.pk).first()
    except Exception:
        fresh = None
    if fresh is None or not getattr(fresh, "is_active", False):
        return False
    try:
        # Read the owner from the live row, not the in-memory instance.
        from portal.models import Farm as FarmModel
        owner_id = FarmModel.objects.filter(
            pk=farm.pk).values_list("owner_id", flat=True).first()
        if getattr(actor, "pk", None) is not None and \
                actor.pk == owner_id:
            return True
    except Exception:
        pass
    try:
        from portal.models import Membership
        row = Membership.objects.filter(
            farm=farm, user=actor, is_active=True).values_list(
                "role", flat=True).first()
        return bool(row)
    except Exception:
        return False


def _require_fresh_actor(farm, actor):
    if not _fresh_actor_status(farm, actor):
        raise PermissionError("Actor is revoked or has no farm role.")


def _is_deleted(run):
    try:
        row = AgentRun.objects.filter(pk=run.pk).values_list(
            "status", flat=True).first()
    except Exception:
        row = None
    return row == "deleted"


def _is_gone_status(status):
    return status in ("deleted", "cancelled")


def _guarded_save(run, fields):
    # Conditional UPDATE: never resurrect deleted/cancelled rows and
    # avoid check-then-save races. Queryset UPDATE bypasses model
    # save() by design; immutable context/policy are bound once
    # before validation and never rewritten through this path.
    try:
        now = timezone.now()
        values = {}
        for field in list(fields or []):
            try:
                values[field] = getattr(run, field)
            except Exception:
                continue
        values["updated_at"] = now
        updated = type(run).objects.filter(pk=run.pk).exclude(
            status__in=("deleted", "cancelled")).update(**values)
        if updated:
            try:
                run.updated_at = now
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False


def _record_stage(run, stage, code):
    """Append a safe progress event (stage, code, timestamp), no CoT."""
    try:
        events = list(run.stage_events or [])
    except Exception:
        events = []
    events.append({"stage": int(stage), "code": str(code),
                   "at": timezone.now().isoformat()})
    run.stage_events = events[-50:]
    run.stage = int(stage)
    return _guarded_save(run, ["stage_events", "stage"])


def _finish(run, status, error_code=None, error_message=None):
    # Conditional UPDATE finish: deleted/cancelled rows stay terminal.
    run.status = status
    # Mutation preview rests at stage 5; readonly completion at stage 7.
    run.stage = 5 if status == "preview" else 7
    run.error_code = error_code
    run.error_message_safe = error_message
    run.completed_at = timezone.now()
    try:
        events = list(run.stage_events or [])
    except Exception:
        events = []
    code = "preview" if status == "preview" else (
        "independent" if status == "done" else f"terminal-{status}")
    events.append({"stage": int(run.stage), "code": code,
                   "at": timezone.now().isoformat()})
    run.stage_events = events[-50:]
    try:
        type(run).objects.filter(pk=run.pk).exclude(
            status__in=("deleted", "cancelled")).update(
            status=run.status, stage=run.stage,
            error_code=run.error_code,
            error_message_safe=run.error_message_safe,
            completed_at=run.completed_at,
            stage_events=run.stage_events, updated_at=timezone.now())
    except Exception:
        pass
    return run


def _check_live_gates(run, farm, actor):
    """Fresh consent/actor/policy/deleted gate before dispatch and save."""
    if _is_deleted(run):
        raise _DeletedStop()
    try:
        live_status = type(run).objects.filter(
            pk=run.pk).values_list("status", flat=True).first()
    except Exception:
        live_status = None
    if _is_gone_status(live_status):
        raise _DeletedStop()
    _require_fresh_actor(farm, actor)
    from portal.models import current_consent_granted
    if not bool(current_consent_granted(farm)):
        raise _NeedsRebase("consent_missing")
    bound_sha = getattr(run, "policy_sha256", "") or ""
    if bound_sha:
        try:
            fresh_policy = policy_gate.ensure_policy()
        except policy_gate.PolicyBlocked:
            raise _NeedsRebase("policy_blocked")
        if str(fresh_policy.get("sha256") or "") != str(bound_sha):
            raise _NeedsRebase("policy_changed")


def start_run(*, farm, actor, goal, idempotency_key, language="hu",
              selected_doc_ids=(), granted_doc_ids=(),
              selected_property_ids=(), client=None,
              transport=None, limits_overrides=None, execute=True,
              cost_consent=True):
    """Create and advance one bounded planning run. Returns the AgentRun."""
    from portal.models import latest_consent_event
    _require_fresh_actor(farm, actor)
    limits = budgets.get_limits(limits_overrides)
    payload_hash = _payload_hash(farm, actor, goal, selected_doc_ids,
                                 language, granted_doc_ids)
    existing = AgentRun.objects.filter(
        farm=farm, idempotency_key=idempotency_key).first()
    if existing is not None:
        same_payload = (existing.payload_hash == payload_hash)
        same_actor = (existing.initiated_by_id == actor.pk)
        if not (same_payload and same_actor):
            raise ValueError("Idempotency key reuse with different input.")
        return existing
    if not bool(cost_consent):
        run = AgentRun(farm=farm, initiated_by=actor,
                       idempotency_key=idempotency_key,
                       payload_hash=payload_hash, goal=str(goal or ""),
                       language=language or "hu", status="blocked",
                       budgets=limits)
        run.stage = 7
        run.error_code = "cost_consent_missing"
        run.error_message_safe = "Hiányzik a költség-hozzájárulás."
        run.completed_at = timezone.now()
        try:
            with transaction.atomic():
                run.save()
        except IntegrityError:
            dup = AgentRun.objects.filter(
                farm=farm, idempotency_key=idempotency_key).first()
            if dup is None:
                raise
            if dup.payload_hash != payload_hash:
                raise ValueError(
                    "Idempotency key reuse with different input.")
            return dup
        return run
    run = AgentRun(farm=farm, initiated_by=actor,
                   idempotency_key=idempotency_key,
                   payload_hash=payload_hash, goal=str(goal or ""),
                   language=language or "hu", status="queued",
                   budgets=limits)
    run.input_snapshot = {
        "selected_property_ids": sorted(
            {int(d) for d in (selected_property_ids or [])}),
        "selected_doc_ids": sorted(
            {int(d) for d in (selected_doc_ids or [])}),
        "granted_doc_ids": sorted(
            {int(d) for d in (granted_doc_ids or [])}),
        "language": language or "hu",
    }
    run.stage_events = [{"stage": 0, "code": "enqueued",
                         "at": timezone.now().isoformat()}]
    try:
        with transaction.atomic():
            run.save()
            from .models import AgentDocGrant
            for doc_id in dict.fromkeys(int(d) for d in
                                        granted_doc_ids or []):
                AgentDocGrant.objects.get_or_create(
                    run=run, document_id=doc_id,
                    defaults={"granted": True, "granted_by": actor})
    except IntegrityError:
        dup = AgentRun.objects.filter(
            farm=farm, idempotency_key=idempotency_key).first()
        if dup is None:
            raise
        if dup.payload_hash != payload_hash or \
                dup.initiated_by_id != actor.pk:
            raise ValueError("Idempotency key reuse with different input.")
        return dup
    if not execute:
        return run
    try:
        _advance(run, farm, actor, limits, client, transport,
                 selected_doc_ids)
    except (BudgetExceeded, ProviderError):
        raise
    except _DeletedStop:
        pass
    except PermissionError:
        try:
            run.refresh_from_db()
        except Exception:
            pass
        if run.pk and getattr(run, "status", "") not in (
                "deleted", "cancelled"):
            _finish(run, "blocked", "actor_revoked",
                    "A szerepkör visszavonásra került.")
        raise
    except _NeedsRebase as exc:
        try:
            run.refresh_from_db()
        except Exception:
            pass
        if run.status != "deleted":
            code = str(exc) or "needs_rebase"
            if code == "consent_missing":
                _finish(run, "blocked", "consent_missing",
                        "Hiányzik a farm felhő-hozzájárulása.")
            else:
                _finish(run, "blocked", "needs_rebase",
                        "A források változtak; újratervezés kell.")
    except IntegrityError:
        raise
    return run


def enqueue_run(*, farm, actor, goal, idempotency_key, language="hu",
                selected_doc_ids=(), granted_doc_ids=(),
                selected_property_ids=(), limits_overrides=None):
    """Reserve-only entry: grants + language stored, no domain writes."""
    return start_run(farm=farm, actor=actor, goal=goal,
                     idempotency_key=idempotency_key, language=language,
                     selected_doc_ids=selected_doc_ids,
                     granted_doc_ids=granted_doc_ids,
                     selected_property_ids=selected_property_ids,
                     limits_overrides=limits_overrides, execute=False)


def _advance(run, farm, actor, limits, client, transport, selected_doc_ids):
    from portal.models import (current_consent_granted,
                               latest_consent_event)
    from .models import AgentDocGrant
    _require_fresh_actor(farm, actor)
    run.status = "running"
    if not _guarded_save(run, ["status"]):
        return
    if not _record_stage(run, 1, "context"):
        return
    if client is None:
        _require_fresh_actor(farm, actor)
        client = AgentClient(transport=transport)

    # Manifest gate is None until the immutable manifest exists; the
    # agri classifier runs before any manifest and must not use it.
    fresh_manifest_ok = None

    def dispatch(kind, func, prompt_text=""):
        _check_live_gates(run, farm, actor)
        gate = fresh_manifest_ok
        if kind in ("planner", "critic") and callable(gate):
            if not gate():
                raise _NeedsRebase("needs_rebase")
        cap = int((limits or {}).get("max_output_tokens", 2000))
        remaining = budgets.remaining_wall_seconds(run)
        call = budgets.reserve(run, kind, prompt_text=prompt_text,
                               max_output_tokens=cap)
        if transport is not None and hasattr(client, "_post"):
            pass
        try:
            if kind in ("planner", "critic") and remaining > 0 and \
                    hasattr(client, "_post"):
                result = func()
            else:
                result = func()
        except ProviderTimeout:
            budgets.record_call(call, "unknown")
            raise
        except ProviderError:
            budgets.record_call(call, "failed")
            raise
        budgets.record_call(
            call, "success",
            input_tokens=result.get("input_tokens"),
            output_tokens=result.get("output_tokens"),
            cost_estimate=result.get("cost_estimate"))
        # Late callbacks never resurrect: cancelled rows stop here
        # exactly like deleted rows.
        _check_live_gates(run, farm, actor)
        gate = fresh_manifest_ok
        if kind in ("planner", "critic") and callable(gate):
            if not gate():
                raise _NeedsRebase("needs_rebase")
        return result

    # Revalidate before any dispatch: farm cloud consent is separate from
    # per-document grants and is re-checked here, at dispatch and at apply.
    consent_event = latest_consent_event(farm)
    consent_ok = bool(current_consent_granted(farm))
    run.revalidation = {
        "consent_event_id": getattr(consent_event, "pk", None),
        "consent_granted": consent_ok,
    }
    if not _guarded_save(run, ["revalidation"]):
        return
    if not consent_ok:
        run.critic_findings = [{"severity": "blocked", "step": 0,
                                "code": "consent-missing",
                                "reason": "Farm cloud consent missing."}]
        if not _guarded_save(run, ["critic_findings"]):
            return
        _finish(run, "blocked", "consent_missing",
                "Hiányzik a farm felhő-hozzájárulása.")
        return

    agri_text = str(run.goal or "")
    try:
        if hasattr(client, "classify_agri_call"):
            classified = dispatch(
                "agri_classify",
                lambda: client.classify_agri_call(agri_text),
                prompt_text=agri_text)
            verdict = classified.get("verdict")
        else:
            classified = dispatch(
                "agri_classify", lambda: {"verdict": client.classify_agri(
                    agri_text)}, prompt_text=agri_text)
            verdict = classified.get("verdict")
        agri_ok = (agri_gate.local_screen(agri_text) != "nonagri"
                   and str(verdict) == "agri")
    except (ProviderError, BudgetExceeded):
        run.critic_findings = [{"severity": "blocked", "step": 0,
                                "code": "agri-unproven",
                                "reason": "Agricultural purpose unproven."}]
        if not _guarded_save(run, ["critic_findings"]):
            return
        _finish(run, "blocked", "agri_unproven",
                "Nem igazolható a mezőgazdasági cél.")
        return
    if not agri_ok:
        run.critic_findings = [{"severity": "blocked", "step": 0,
                                "code": "non-agricultural",
                                "reason": "Non-agricultural request."}]
        if not _guarded_save(run, ["critic_findings"]):
            return
        _finish(run, "blocked", "non_agricultural",
                "Nem mezőgazdasági kérés.")
        return

    try:
        policy = policy_gate.ensure_policy()
    except policy_gate.PolicyBlocked:
        _finish(run, "blocked", "policy_blocked",
                "Hiányzó/sérült központi policy.")
        return
    run.policy_version = policy["version"]
    run.policy_sha256 = policy["sha256"]
    if not _guarded_save(run, ["policy_version", "policy_sha256"]):
        return

    granted_ids = list(AgentDocGrant.objects.filter(
        run=run, granted=True).values_list("document_id", flat=True))
    manifest = ctx.build_manifest(
        farm=farm, actor=actor, goal=run.goal,
        selected_doc_ids=list(selected_doc_ids or []),
        granted_doc_ids=granted_ids, language=run.language)
    run.context_manifest = manifest
    run.policy_cited_sections = []
    if not _guarded_save(run, ["context_manifest",
                               "policy_cited_sections"]):
        return
    if not _record_stage(run, 2, "goal"):
        return

    from portal.domain_models import RuleActivation
    try:
        local_rules = [a.rule for a in RuleActivation.objects.filter(
            farm=farm, is_active=True).select_related("rule")]
    except Exception:
        local_rules = []

    def fresh_manifest_ok():
        try:
            fresh_policy = policy_gate.ensure_policy()
        except policy_gate.PolicyBlocked:
            return False
        if str(fresh_policy.get("sha256") or "") != str(
                run.policy_sha256 or ""):
            return False
        try:
            fresh = ctx.build_manifest(
                farm=farm, actor=actor, goal=run.goal,
                selected_doc_ids=list(selected_doc_ids or []),
                granted_doc_ids=list(AgentDocGrant.objects.filter(
                    run=run, granted=True).values_list("document_id",
                                                       flat=True)),
                language=run.language)
        except Exception:
            return False
        try:
            stored = AgentRun.objects.filter(pk=run.pk).values_list(
                "context_manifest", flat=True).first() or manifest
        except Exception:
            stored = manifest
        return _stable_sources(fresh) == _stable_sources(stored)

    def _loop_candidate_commands(cand):
        out = []
        try:
            steps = (cand or {}).get("steps") or []
        except Exception:
            return out
        for step in steps:
            if not isinstance(step, dict):
                continue
            for cmd in step.get("commands") or []:
                if isinstance(cmd, dict):
                    out.append(cmd)
        return out

    def _risk_block_findings(cand, commands):
        # Deterministic risk gate: high risk without uncertainties is
        # blocked, and critical mutating steps with unknown numeric
        # cost are blocked. Safe small deletes with history-free
        # validation still pass via the registry check below.
        extra = []
        try:
            steps = (cand or {}).get("steps") or []
        except Exception:
            steps = []
        has_critical_cmd = any(
            str((c or {}).get("op") or "") == "delete"
            for c in commands or [])
        cost_unknown = False
        try:
            for call in AgentCall.objects.filter(run=run):
                if call.cost_estimate is None:
                    cost_unknown = True
                    break
        except Exception:
            cost_unknown = False
        for pos, step in enumerate(steps or [], start=1):
            if not isinstance(step, dict):
                continue
            risk = str(step.get("risk") or (cand or {}).get("risk")
                       or "").lower()
            unknowns = step.get("uncertainties")
            if risk == "high" and not unknowns:
                extra.append({
                    "severity": "critical", "step": pos,
                    "code": "high-risk-no-unknowns",
                    "reason": "High-risk step without uncertainties."})
            step_cmds = [c for c in (step.get("commands") or [])
                         if isinstance(c, dict)]
            has_delete = any(
                str(c.get("op") or "") == "delete" for c in step_cmds)
            if has_delete and risk == "high" and not unknowns:
                continue  # already recorded above
            if has_delete and not (step.get("evidence") or []):
                extra.append({
                    "severity": "critical", "step": pos,
                    "code": "unsupported-claim",
                    "reason": "Delete without manifest evidence."})
        if has_critical_cmd and cost_unknown:
            extra.append({
                "severity": "critical", "step": 0,
                "code": "cost-unknown-critical",
                "reason": "Critical action with unknown numeric cost."})
        return extra

    max_revisions = min(int(limits.get("max_revisions", 2) or 0), 2)
    planner_messages = build_planner_messages(
        manifest, policy, run.goal, language=run.language)
    try:
        first = dispatch("planner",
                         lambda: client.plan(planner_messages, limits),
                         prompt_text=json.dumps(planner_messages,
                                                ensure_ascii=False))
    except (ProviderError, BudgetExceeded):
        _finish(run, "outcome_unknown", "planner_unknown",
                "A tervjelölt hívás eredménye ismeretlen.")
        return
    candidate = first.get("candidate")
    if not isinstance(candidate, dict):
        _finish(run, "error", "invalid_candidate",
                "Érvénytelen tervjelölt.")
        return
    run.candidate = candidate
    run.candidate_revision = 0
    if not _guarded_save(run, ["candidate", "candidate_revision"]):
        return
    if not _record_stage(run, 3, "candidate"):
        return

    previous_hash = None
    revision = 0
    verdict = "needs_revision"
    all_findings = []
    critic_ran = False
    while True:
        if not fresh_manifest_ok():
            run.critic_findings = [{"severity": "blocked", "step": 0,
                                    "code": "needs_rebase",
                                    "reason": "Source/policy drift."}]
            _guarded_save(run, ["critic_findings"])
            _finish(run, "blocked", "needs_rebase",
                    "A források változtak; újratervezés kell.")
            return
        errors = schemas.validate_candidate(candidate, manifest)
        det_findings = [
            {"severity": "critical", "step": 0,
             "code": "schema", "reason": e} for e in errors]
        try:
            det_findings += policy_gate.check_conflicts(
                json.dumps(candidate, ensure_ascii=False), local_rules)
        except policy_gate.PolicyBlocked:
            _finish(run, "blocked", "policy_blocked",
                    "Hiányzó/sérült központi policy.")
            return
        # Registry validation before any critique: invalid commands
        # block even when the critic would say verified. No writes.
        loop_commands = _loop_candidate_commands(candidate)
        if loop_commands:
            try:
                from . import registry as _reg
                _reg.validate_commands(actor, farm, loop_commands)
            except Exception as exc:
                det_findings.append({
                    "severity": "critical", "step": 0,
                    "code": "registry-invalid",
                    "reason": f"Command validation failed: {exc}"})
            try:
                det_findings += _risk_block_findings(
                    candidate, loop_commands)
            except Exception:
                pass
        try:
            review = dispatch(
                "critic", lambda: client.critique(
                    build_critic_messages(candidate, manifest, policy,
                                         language=run.language),
                    limits),
                prompt_text=json.dumps(candidate, ensure_ascii=False)[:4000])
        except (ProviderError, BudgetExceeded):
            _finish(run, "outcome_unknown", "critic_unknown",
                    "A független ellenőrzés eredménye ismeretlen.")
            return
        critic_ran = True
        try:
            checked = schemas.validate_critic_output(review)
        except Exception:
            run.critic_findings = [{"severity": "blocked", "step": 0,
                                    "code": "critic-malformed",
                                    "reason": "Malformed critic output."}]
            _guarded_save(run, ["critic_findings"])
            _finish(run, "blocked", "critic_malformed",
                    "Az ellenőrző válasza érvénytelen.")
            return
        findings = list(det_findings) + list(checked.get("findings") or [])
        critic_verdict = str(checked.get("verdict") or "needs_revision")
        if det_findings or critic_verdict == "blocked":
            verdict = "blocked"
        elif findings or critic_verdict != "verified":
            verdict = "needs_revision"
        else:
            verdict = "verified"
        all_findings = findings
        run.critic_findings = findings
        if not _guarded_save(run, ["critic_findings"]):
            return
        if not _record_stage(run, 4, "validation"):
            return
        if verdict == "verified":
            break
        current_hash = _candidate_hash(candidate)
        if revision >= max_revisions:
            verdict = "blocked"
            all_findings = findings + [{
                "severity": "blocked", "step": 0,
                "code": "revision-budget-exhausted",
                "reason": "Revision budget exhausted."}]
            run.critic_findings = all_findings
            _guarded_save(run, ["critic_findings"])
            break
        if previous_hash is not None and current_hash == previous_hash:
            verdict = "blocked"
            all_findings = findings + [{
                "severity": "blocked", "step": 0,
                "code": "candidate-unchanged",
                "reason": "Unchanged faulty candidate; stopping."}]
            run.critic_findings = all_findings
            _guarded_save(run, ["critic_findings"])
            break
        previous_hash = current_hash
        revision += 1
        planner_messages = build_planner_messages(
            manifest, policy, run.goal, feedback=findings,
            language=run.language)
        try:
            nxt = dispatch("planner",
                           lambda: client.plan(planner_messages, limits),
                           prompt_text=json.dumps(
                               planner_messages,
                               ensure_ascii=False)[:8000])
        except (ProviderError, BudgetExceeded):
            _finish(run, "outcome_unknown", "planner_unknown",
                    "A javító hívás eredménye ismeretlen.")
            return
        candidate = nxt.get("candidate")
        if not isinstance(candidate, dict):
            _finish(run, "error", "invalid_candidate",
                    "Érvénytelen javított jelölt.")
            return
        run.candidate = candidate
        run.candidate_revision = revision
        if not _guarded_save(run, ["candidate", "candidate_revision"]):
            return

    if not critic_ran:
        run.critic_findings = [{"severity": "blocked", "step": 0,
                                "code": "critic-missing",
                                "reason": "No independent critic run."}]
        _guarded_save(run, ["critic_findings"])
        _finish(run, "blocked", "critic_missing",
                "Hiányzik a független ellenőrzés.")
        return
    try:
        cited = sorted({e.get("policy_section")
                        for s in (candidate.get("steps") or [])
                        for e in (s.get("evidence") or [])
                        if isinstance(e, dict) and e.get("policy_section")})
    except Exception:
        cited = []
    run.policy_cited_sections = cited
    calls = AgentCall.objects.filter(run=run)
    in_tok = out_tok = 0
    tok_unknown = False
    for call in calls:
        if call.input_tokens is None or call.output_tokens is None:
            tok_unknown = True
        else:
            in_tok += call.input_tokens
            out_tok += call.output_tokens
    run.usage_summary = {
        "calls": calls.count(),
        "input_tokens": None if tok_unknown else in_tok,
        "output_tokens": None if tok_unknown else out_tok,
        "unknown_cost": None,
    }
    run.revalidation = {
        "consent_event_id": getattr(latest_consent_event(farm), "pk",
                                    None),
        "consent_granted": bool(current_consent_granted(farm)),
        "policy_sha256": run.policy_sha256,
        "candidate_sha256": _candidate_hash(candidate),
        "critic_verified_candidate_sha": (
            _candidate_hash(candidate) if verdict == "verified" else ""),
    }
    if not _guarded_save(run, ["policy_cited_sections", "usage_summary",
                               "revalidation"]):
        return
    if verdict == "verified":
        has_commands = any(
            bool((s or {}).get("commands"))
            for s in (candidate.get("steps") or [])
            if isinstance(s, dict))
        if has_commands:
            _finish(run, "preview")
        else:
            _finish(run, "done")
    else:
        _finish(run, "blocked", "candidate_blocked",
                "A tervjelölt ellenőrzésen fennakadt.")
