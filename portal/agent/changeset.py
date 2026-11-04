"""Immutable approval executor: prepare / approve / reject / verify.

Server-side only: no model, provider, file or network calls happen
inside the write transaction. Preview creates no domain records; the
signed token binds actor, farm, changeset id, payload hash and an
expiry capped by the changeset row; apply re-authenticates everything
and revalidates consent, role, policy, run verdict, sources and
staleness before writing.

Apply holds a single write transaction: the conditional preview to
applying claim is the first statement inside ``transaction.atomic``,
so a failed validation or write rolls the claim itself back to
preview. No ``select_for_update`` row-lock fiction is used; SQLite
serializes the conditional UPDATE and a bounded busy retry absorbs
transient locks.
"""
import hashlib
import json
import time

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

from . import registry
from .models import AgentChangeset

TOKEN_SALT = "agent-changeset-v1"
TOKEN_MAX_AGE = 24 * 3600

_BUSY_RETRIES = 3


class ChangesetError(ValueError):
    pass


class RunNotVerified(ChangesetError):
    pass


class StaleChangeset(ChangesetError):
    pass


def _signer():
    return signing.Signer(salt=TOKEN_SALT)


def _fresh_actor(farm, actor):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    fresh = User.objects.filter(pk=actor.pk).first()
    if fresh is None or not getattr(fresh, "is_active", False):
        raise PermissionDenied("Actor is revoked or inactive.")
    # Read the owner from the live row, not the in-memory instance.
    from portal.models import Farm as FarmModel
    owner_id = FarmModel.objects.filter(
        pk=farm.pk).values_list("owner_id", flat=True).first()
    if owner_id == actor.pk:
        return True
    from portal.models import Membership
    row = Membership.objects.filter(
        farm=farm, user=actor, is_active=True).values_list(
            "role", flat=True).first()
    if not row:
        raise PermissionDenied("Actor has no farm role.")
    return True


def _current_role(farm, actor):
    # Read the owner from the live row, not the in-memory instance.
    from portal.models import Farm as FarmModel
    owner_id = FarmModel.objects.filter(
        pk=farm.pk).values_list("owner_id", flat=True).first()
    if owner_id == actor.pk:
        return "owner"
    from portal.models import Membership
    return Membership.objects.filter(
        farm=farm, user=actor, is_active=True).values_list(
            "role", flat=True).first()


def _stable_sources(manifest):
    out = []
    for item in (manifest or {}).get("items", []):
        if not isinstance(item, dict):
            continue
        out.append((str(item.get("source_id") or ""),
                    str(item.get("hash") or ""),
                    str(item.get("revision") or "")))
    return sorted(out)


def _source_vector(farm, run):
    if run is not None:
        items = _stable_sources(run.context_manifest or {})
        return [f"{sid}:{h}:{rev}" for sid, h, rev in items]
    # Manual proposals carry an explicit empty vector; the policy and
    # consent bindings below still apply.
    return []


def _candidate_commands(run):
    """Every structured command the run candidate proposes, in order."""
    try:
        steps = (run.candidate or {}).get("steps") or []
    except Exception:
        return []
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for cmd in step.get("commands") or []:
            if isinstance(cmd, dict):
                out.append(cmd)
    return out


def _normalized(cmd):
    parsed = registry.parse_command(cmd)
    return {"target": parsed["target"], "op": parsed["op"],
            "id": parsed["id"], "fields": parsed["fields"]}


def _candidate_sha(run):
    if run is None:
        return ""
    blob = json.dumps(run.candidate or {}, ensure_ascii=False,
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ensure_run_verified(run, farm, *, actor, policy_sha256, parsed):
    """A run gates approval only when fully verified at stage 7.

    Raises PermissionDenied when the run belongs to another actor,
    StaleChangeset on policy drift, and RunNotVerified otherwise. The
    candidate must propose exactly the prepared commands: no
    arbitrary UI-supplied commands ride on a verified run.
    """
    if run is None:
        raise RunNotVerified("No verified run is bound to this preview.")
    if run.farm_id != farm.pk:
        raise RunNotVerified("Run belongs to another farm.")
    if getattr(run, "initiated_by_id", None) != actor.pk:
        raise PermissionDenied("Run was initiated by another actor.")
    if run.status not in ("preview", "done"):
        raise RunNotVerified("Run is not verified for approval.")
    if run.stage != 7:
        allow_preview = (
            run.stage == 5
            and run.status == "preview"
            and (run.revalidation or {}).get(
                "critic_verified_candidate_sha") == _candidate_sha(run)
            and _candidate_sha(run) != ""
        )
        if allow_preview:
            from .models import AgentCall
            allow_preview = AgentCall.objects.filter(
                run=run, kind="critic", status="success").exists()
        if not allow_preview:
            raise RunNotVerified("Run is not verified for approval.")
    if not run.candidate:
        raise RunNotVerified("Run has no candidate to approve.")
    if not run.policy_sha256 or \
            run.policy_sha256 != (policy_sha256 or ""):
        raise StaleChangeset("Policy changed since the run.")
    for finding in run.critic_findings or []:
        if str((finding or {}).get("severity") or "") in (
                "critical", "blocked", "unknown"):
            raise RunNotVerified("Run critic did not verify the candidate.")
    try:
        want = [_normalized(c) for c in (parsed or [])]
        got = [_normalized(c) for c in _candidate_commands(run)]
    except Exception as exc:
        raise RunNotVerified(
            "Candidate commands are not well formed.") from exc
    if not got or want != got:
        raise RunNotVerified(
            "Candidate does not propose these exact commands.")
    return True


def _critic_sha(run):
    blob = json.dumps({"findings": run.critic_findings or [],
                       "candidate": run.candidate or {}},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _manifest_doc_ids(manifest):
    ids = []
    for item in (manifest or {}).get("items", []):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "")
        if sid.startswith("document:"):
            try:
                ids.append(int(sid.split(":", 1)[1]))
            except (TypeError, ValueError):
                continue
    return sorted(set(ids))


def _doc_rule_triples(manifest):
    out = []
    for item in (manifest or {}).get("items", []):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "")
        if sid.startswith("document:") or sid.startswith("rule:"):
            out.append((sid, str(item.get("hash") or ""),
                        str(item.get("revision") or "")))
    return sorted(out)


def _fresh_doc_rule_vector(farm, run):
    """Rebuild the document/rule slice fresh (file reads allowed).

    Only callers outside the write transaction may use this: document
    blobs are re-read and re-hashed here, then compared against the
    stored manifest triples. Any drift (deleted, changed, deactivated
    or permission-lost source) raises StaleChangeset; a lost grant or
    consent raises PermissionDenied. Missing rows block, never omit.
    """
    from . import context as ctx
    from .models import AgentDocGrant
    manifest = run.context_manifest or {}
    selected = _manifest_doc_ids(manifest)
    granted = list(AgentDocGrant.objects.filter(
        run=run, granted=True).values_list("document_id", flat=True))
    fresh = ctx.build_manifest(
        farm=farm, actor=run.initiated_by, goal=run.goal,
        selected_doc_ids=sorted(set(selected) | set(int(d) for d in
                                                    granted)),
        granted_doc_ids=granted, language=run.language or "hu")
    want = _doc_rule_triples(manifest)
    got = _doc_rule_triples(fresh)
    if want != got:
        raise StaleChangeset("Sources changed since preview.")
    return got


def _check_db_source_revision(farm, run):
    """Cheap DB-only source revalidation; safe inside the write txn.

    Re-reads only live rows: consent event identity, document
    revision/visibility/assignment/grant and rule revision/visibility/
    activation. No file, provider or network calls happen here. Any
    deleted, changed, deactivated or permission-lost source blocks the
    apply; rows are never silently omitted.
    """
    from portal.domain_models import Document, FarmDocument, RuleActivation
    from portal.models import (current_consent_granted,
                               latest_consent_event)
    from .context import TEXT_LIMIT
    from .models import AgentDocGrant
    if run is None:
        return True
    if not bool(current_consent_granted(farm)):
        raise PermissionDenied("Consent was revoked.")
    recorded = (run.revalidation or {}).get("consent_event_id")
    if recorded is not None:
        latest = latest_consent_event(farm)
        if latest is None or latest.pk != recorded:
            raise StaleChangeset(
                "Consent changed; a new preview is required.")
    manifest = run.context_manifest or {}
    actor_id = getattr(run, "initiated_by_id", None)
    for item in (manifest.get("items") or []):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "")
        if sid.startswith("document:"):
            try:
                doc_id = int(sid.split(":", 1)[1])
            except (TypeError, ValueError):
                raise StaleChangeset("Sources changed since preview.")
            doc = Document.objects.filter(pk=doc_id).first()
            if doc is None:
                raise StaleChangeset("A source document was deleted.")
            try:
                updated = doc.updated_at.isoformat()
            except Exception:
                updated = ""
            if str(item.get("revision") or "") != updated:
                raise StaleChangeset("A source document changed.")
            eligible = (doc.owner_id == actor_id) or bool(doc.is_public)
            if not eligible:
                raise PermissionDenied(
                    "A source document is no longer visible.")
            if not FarmDocument.objects.filter(
                    farm=farm, document=doc, is_active=True).exists():
                raise StaleChangeset("A source document was uninstalled.")
            if not AgentDocGrant.objects.filter(
                    run=run, document=doc, granted=True).exists():
                raise StaleChangeset("A document grant is missing.")
        elif sid.startswith("rule:"):
            try:
                rule_id = int(sid.split(":", 1)[1])
            except (TypeError, ValueError):
                raise StaleChangeset("Sources changed since preview.")
            act = RuleActivation.objects.filter(
                farm=farm, rule_id=rule_id,
                is_active=True).select_related("rule").first()
            if act is None or act.rule is None:
                raise StaleChangeset("A source rule was deactivated.")
            rule = act.rule
            if not ((rule.owner_id == farm.owner_id) or
                    bool(rule.is_public)):
                raise PermissionDenied(
                    "A source rule is no longer visible.")
            body = str(rule.body or "")[:TEXT_LIMIT]
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if str(item.get("hash") or "") != digest:
                raise StaleChangeset("A source rule changed.")
    return True


def _payload_hash(farm, actor, parsed, run, source_vector, policy,
                  before_hash, expires_at, impacts, candidate_sha):
    """Immutable full canonical package hash.

    Binds farm, actor, run, the exact normalized commands, the source
    vector, the policy binding, the before snapshot hash, the expiry,
    the delete-impact summary and the candidate hash in one record.
    """
    blob = json.dumps({
        "farm": farm.pk, "actor": actor.pk,
        "commands": parsed,
        "run": getattr(run, "pk", None),
        "candidate": candidate_sha or "",
        "sources": list(source_vector or []),
        "policy": {"version": policy.get("version", ""),
                   "sha256": policy.get("sha256", "")},
        "before": before_hash or "",
        "expires": expires_at.isoformat() if expires_at else "",
        "impacts": impacts or [],
    }, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_token(changeset, ttl_seconds=3600):
    """Sign a token whose expiry never exceeds the changeset expiry."""
    exp = int(timezone.now().timestamp()) + int(ttl_seconds)
    try:
        cap = int(changeset.expires_at.timestamp())
    except Exception:
        cap = exp
    payload = {"csid": changeset.pk, "farm": changeset.farm_id,
               "actor": changeset.actor_id,
               "phash": changeset.payload_hash,
               "exp": min(exp, cap)}
    raw = json.dumps(payload, sort_keys=True)
    return _signer().sign(raw)


def _check_token(actor, farm, cs, token, payload_hash):
    try:
        raw = _signer().unsign(token)
    except signing.BadSignature:
        raise PermissionDenied("Bad approval token.")
    try:
        payload = json.loads(raw)
    except Exception:
        raise PermissionDenied("Bad approval token.")
    if payload.get("csid") != cs.pk or payload.get("farm") != farm.pk:
        raise PermissionDenied("Approval token is not for this farm.")
    if payload.get("actor") != actor.pk:
        # Tokens are client-specific: only the preparing actor approves.
        raise PermissionDenied("Approval token belongs to another actor.")
    if payload.get("phash") != payload_hash:
        raise PermissionDenied("Payload hash mismatch.")
    now = int(timezone.now().timestamp())
    if int(payload.get("exp", 0)) < now:
        raise PermissionDenied("Approval token expired.")
    try:
        cap = int(cs.expires_at.timestamp())
    except Exception:
        cap = now
    if int(payload.get("exp", 0)) > cap:
        # A freshly signed token must never extend the stored expiry.
        raise PermissionDenied("Approval token exceeds changeset expiry.")
    return payload


def _fresh_run(farm, run):
    if run is None:
        return None
    return type(run).objects.filter(
        pk=run.pk).select_related(
            "farm", "initiated_by").first()


def _record_revalidation(run, policy, candidate_sha):
    from portal.models import latest_consent_event
    event = latest_consent_event(run.farm)
    revalidation = dict(run.revalidation or {})
    revalidation.update({
        "consent_event_id": getattr(event, "pk", None),
        "consent_granted": True,
        "policy_sha256": policy.get("sha256", ""),
        "candidate_sha256": candidate_sha or "",
    })
    type(run).objects.filter(pk=run.pk).update(revalidation=revalidation)
    run.revalidation = revalidation
    return revalidation


def prepare_changeset(actor, farm, commands, run=None,
                      idempotency_key=None, ttl_seconds=3600,
                      allow_manual=False):
    """Validate and store a preview; writes no domain records.

    ``run=None`` is the explicit manual server-only path
    (``allow_manual=True``); no client route may pass it. Every other
    preview binds a fully verified run whose candidate proposes
    exactly the prepared commands.
    """
    from portal.agent import policy_gate
    from portal.models import current_consent_granted
    _fresh_actor(farm, actor)
    if not bool(current_consent_granted(farm)):
        raise PermissionDenied("Farm cloud consent is not granted.")
    try:
        policy = policy_gate.ensure_policy()
    except policy_gate.PolicyBlocked as exc:
        raise RunNotVerified("Policy is unavailable.") from exc
    if run is None and not allow_manual:
        raise RunNotVerified(
            "Direct prepare needs an explicit manual proposal "
            "(allow_manual=True); client command routes never bypass "
            "the verified run.")
    parsed = registry.validate_commands(actor, farm, commands)
    live = _fresh_run(farm, run)
    candidate_sha = ""
    if live is not None:
        _ensure_run_verified(live, farm, actor=actor,
                             policy_sha256=policy["sha256"],
                             parsed=parsed)
        try:
            _fresh_doc_rule_vector(farm, live)
        except (StaleChangeset, PermissionDenied) as exc:
            raise RunNotVerified(str(exc)) from exc
        try:
            _check_db_source_revision(farm, live)
        except (StaleChangeset, PermissionDenied) as exc:
            raise RunNotVerified(str(exc)) from exc
        candidate_sha = _candidate_sha(live)
    source_vector = _source_vector(farm, live)
    before = registry.snapshot_farm(farm, actor=actor)
    before_hash = registry.snapshot_hash(before)
    impacts = registry.preview_impacts(farm, parsed, actor=actor)
    expires_at = timezone.now() + timezone.timedelta(
        seconds=int(ttl_seconds))
    payload_hash = _payload_hash(
        farm, actor, parsed, live, source_vector, policy, before_hash,
        expires_at, impacts, candidate_sha)
    key = idempotency_key or f"cs-{payload_hash[:16]}"
    existing = AgentChangeset.objects.filter(
        farm=farm, idempotency_key=key).first()
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ChangesetError(
                "Idempotency key reuse with different input.")
        if live is not None:
            _record_revalidation(live, policy, candidate_sha)
        return existing, make_token(existing, ttl_seconds)
    if live is not None:
        _record_revalidation(live, policy, candidate_sha)
    critic_sha = _critic_sha(live) if live is not None else ""
    cs = AgentChangeset(
        farm=farm, actor=actor, run=live, idempotency_key=key,
        payload_hash=payload_hash, commands=parsed,
        before_snapshot=before, before_hash=before_hash,
        source_vector=source_vector,
        policy_version=policy.get("version", ""),
        policy_sha256=policy.get("sha256", ""),
        run_critic_sha256=critic_sha, state="preview",
        expires_at=expires_at)
    with transaction.atomic():
        # Safe source retrieval happens before the txn; inside only a
        # cheap revision re-read guards against a consent race.
        from portal.models import latest_consent_event
        latest_consent_event(farm)
        cs.save()
    return cs, make_token(cs, ttl_seconds)


def reject_changeset(actor, farm, csid):
    """Move a live preview to rejected; terminal states stay put."""
    _fresh_actor(farm, actor)
    cs = AgentChangeset.objects.filter(pk=csid, farm=farm).first()
    if cs is None:
        raise ChangesetError("Unknown changeset.")
    if cs.actor_id != actor.pk:
        from portal.permissions import role_for
        if role_for(actor, farm) != "owner":
            raise PermissionDenied("Only the preparer or owner rejects.")
    if cs.state != "preview":
        raise ChangesetError(
            f"Only previews reject (state={cs.state}).")
    cs.state = "rejected"
    cs.save(update_fields=["state", "updated_at"])
    return cs


def approve_changeset(actor, farm, csid, token, payload_hash):
    """Apply a preview after full revalidation; returns the receipt."""
    from portal.agent import policy_gate
    _fresh_actor(farm, actor)
    cs = AgentChangeset.objects.filter(pk=csid, farm=farm).first()
    if cs is None:
        raise ChangesetError("Unknown changeset.")
    if cs.payload_hash != payload_hash:
        # No receipt is disclosed on a hash mismatch.
        raise PermissionDenied("Payload hash mismatch.")
    if cs.expires_at < timezone.now():
        raise ChangesetError("Changeset expired.")
    _check_token(actor, farm, cs, token, payload_hash)
    if cs.state in ("verified", "applied"):
        # Duplicate approval of the same package replays the exact
        # receipt without any further write. The caller already passed
        # fresh actor, farm, token and hash checks above.
        return dict(cs.receipt or {})
    if cs.state == "rejected":
        raise ChangesetError("Changeset was rejected.")
    if cs.state != "preview":
        raise StaleChangeset(
            f"Changeset is not a live preview (state={cs.state}).")
    try:
        policy = policy_gate.ensure_policy()
    except policy_gate.PolicyBlocked as exc:
        raise StaleChangeset("Policy is unavailable.") from exc
    try:
        parsed = [registry.parse_command(c)
                  for c in (cs.commands or [])]
    except Exception as exc:
        raise StaleChangeset("Stored commands are not well formed.") from exc
    if cs.run_id is not None:
        live = _fresh_run(farm, cs.run)
        if live is None:
            raise StaleChangeset("Bound run is gone.")
        try:
            _fresh_doc_rule_vector(farm, live)
        except (StaleChangeset, PermissionDenied):
            raise
    last = OperationalError("database is busy")
    for attempt in range(_BUSY_RETRIES):
        try:
            return _approve_once(
                actor, farm, cs, payload_hash, policy, parsed)
        except OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last = exc
            time.sleep(0.05 * (attempt + 1))
    raise last


def _approve_once(actor, farm, cs, payload_hash, policy, parsed):
    """Single atomic claim, revalidation and write; rolls back whole."""
    from portal.models import AuditEvent, current_consent_granted
    with transaction.atomic():
        # The conditional claim is the first statement inside the
        # transaction: exactly one approver wins the preview row, and
        # any later failure rolls the claim itself back to preview.
        claimed = AgentChangeset.objects.filter(
            pk=cs.pk, state="preview").update(state="applying")
        if claimed != 1:
            lost = AgentChangeset.objects.filter(pk=cs.pk).first()
            if lost is not None and \
                    lost.payload_hash == payload_hash and \
                    lost.state in ("verified", "applied") and lost.receipt:
                return dict(lost.receipt)
            raise StaleChangeset("Changeset was already claimed.")
        locked = AgentChangeset.objects.get(pk=cs.pk)
        # Cheap DB-only revalidation inside the write transaction: no
        # provider, file or network calls happen below.
        _fresh_actor(farm, actor)
        if locked.actor_id != actor.pk:
            raise PermissionDenied(
                "Approval token belongs to another actor.")
        role = _current_role(farm, actor)
        if role is None:
            raise PermissionDenied("Actor role was revoked.")
        if not bool(current_consent_granted(farm)):
            raise PermissionDenied("Consent was revoked.")
        if locked.expires_at < timezone.now():
            raise ChangesetError("Changeset expired.")
        if locked.policy_sha256 != policy["sha256"]:
            raise StaleChangeset("Policy changed since preview.")
        run = None
        candidate_sha = ""
        if locked.run_id is not None:
            run = _fresh_run(farm, locked.run)
            if run is None:
                raise StaleChangeset("Bound run is gone.")
            _ensure_run_verified(
                run, farm, actor=actor,
                policy_sha256=policy["sha256"], parsed=parsed)
            candidate_sha = _candidate_sha(run)
        _check_db_source_revision(farm, run)
        impacts = registry.preview_impacts(farm, parsed, actor=actor)
        live_before = registry.snapshot_farm(farm, actor=actor)
        if registry.snapshot_hash(live_before) != locked.before_hash:
            raise StaleChangeset("Domain rows changed since preview.")
        for cmd in parsed:
            registry.check_permission(actor, farm, cmd["target"])
            registry.validate_one(farm, cmd, actor=actor)
        expected = _payload_hash(
            farm, actor, parsed, run,
            list(locked.source_vector or []),
            {"version": locked.policy_version,
             "sha256": locked.policy_sha256},
            locked.before_hash, locked.expires_at, impacts,
            candidate_sha)
        if expected != locked.payload_hash:
            raise StaleChangeset(
                "Approval package changed since preview.")
        results = registry.execute_commands(farm, parsed, actor=actor)
        after = registry.snapshot_farm(farm, actor=actor)
        after_hash = registry.snapshot_hash(after)
        audit_ids = []
        for res in results:
            ev = AuditEvent.objects.create(
                farm=farm, actor=actor,
                action=f"changeset.{res['target']}.{res['op']}",
                object_type=res["target"],
                object_id=str(res.get("pk") or ""),
                meta={"csid": locked.pk, "ref": res.get("ref") or "",
                      "before": res.get("before"),
                      "after": res.get("after"),
                      "related": res.get("related_writes") or [],
                      "impact": res.get("impact"),
                      "before_hash": locked.before_hash,
                      "after_hash": after_hash})
            audit_ids.append(ev.pk)
        receipt = {"csid": locked.pk,
                   "payload_hash": locked.payload_hash,
                   "before_hash": locked.before_hash,
                   "after_hash": after_hash,
                   "results": results,
                   "audits": audit_ids,
                   "approved_by": actor.pk,
                   "verified": False}
        locked.after_snapshot = after
        locked.after_hash = after_hash
        locked.receipt = receipt
        locked.audit_ids = audit_ids
        locked.state = "applied"
        locked.save(update_fields=["after_snapshot", "after_hash",
                                   "receipt", "audit_ids", "state",
                                   "updated_at"])
    # Independent readback after the txn: re-read every touched row
    # and compare fields (or confirm deletion), then mark verified.
    receipt = verify_after_commit(farm, locked.pk, actor=actor)
    return receipt


def verify_after_commit(farm, csid, actor=None):
    """Independent readback check; flips applied to verified."""
    cs = AgentChangeset.objects.filter(pk=csid, farm=farm).first()
    if cs is None:
        raise ChangesetError("Unknown changeset.")
    if cs.state == "verified":
        return dict(cs.receipt or {})
    if cs.state != "applied":
        raise ChangesetError(
            f"Nothing to verify (state={cs.state}).")
    receipt = dict(cs.receipt or {})
    results = list(receipt.get("results") or [])
    ok = True
    for res in results:
        eff = actor if actor is not None else cs.actor
        live = registry.snapshot_one(
            farm, res["target"], res.get("pk"), actor=eff)
        if res["op"] == "delete":
            if live is not None:
                ok = False
            for rel in res.get("related_writes") or []:
                if rel.get("deleted"):
                    continue
                if rel.get("after") is not None:
                    ok = False
        else:
            want = res.get("after") or {}
            if live != want:
                ok = False
    receipt["verified"] = bool(ok)
    cs.receipt = receipt
    if ok:
        cs.state = "verified"
        cs.save(update_fields=["receipt", "state", "updated_at"])
    else:
        cs.save(update_fields=["receipt", "updated_at"])
        raise ChangesetError("Readback verification failed.")
    return receipt

