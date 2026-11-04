import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from datetime import timedelta
from .permissions import role_for as _role_for
from .models import Farm, FarmExtraField, FarmProfileSnapshot, AdviceRun, AdviceStep, PROFILE_TEXT_FIELDS, latest_consent_event, current_consent_granted
from django.utils.translation import gettext_lazy as _, get_language
from .provider import MetaProviderClient, ProviderError, ProviderTimeout, ProviderInvalidOutput
from .safety import screen_parsed

SAFE_MESSAGES = {
    "provider_timeout": _("A tanácsadás időtúllépés miatt nem sikerült. Kérjük, próbálja újra."),
    "provider_error": _("A tanácsadó szolgáltatás átmenetileg nem elérhető. Kérjük, próbálja újra később."),
    "invalid_structured_output": _("A tanács formátuma nem volt megfelelő, ezért nem jelenítjük meg. Kérjük, próbálja újra."),
    "prohibited_content": _("A kapott tanács nem felelt meg a biztonsági szabályoknak, ezért nem jelenítjük meg."),
    "consent_required": _("Tanácshoz felhőadat-kezelési hozzájárulás szükséges."),
    "consent_withdrawn_before_call": _("A hozzájárulás visszavonásra került, ezért a tanács nem készült el."),
    "quota_exceeded": _("Elérte a tanácskérési keretet. Kérjük, próbálja később."),
    "global_quota_exceeded": _("A szolgáltatás napi kerete betelt. Kérjük, próbálja holnap."),
    "attempt_interrupted": _("A korábbi tanácskérés megszakadt. Kérjük, próbálja újra."),
    "invalid_input": _("A beküldött adatok érvénytelenek."),
}

MAX_USAGE_TOKENS = 2_147_483_647


class AdviceRejected(Exception):
    def __init__(self, code):
        self.code = code
        self.message = SAFE_MESSAGES.get(code, _("A kérés nem teljesíthető."))
        super().__init__(self.message)


def validate_parsed(parsed):
    """Strict allowlist validation. Returns (steps, assumptions, questions, is_general) or raises ProviderInvalidOutput."""
    if not isinstance(parsed, dict):
        raise ProviderInvalidOutput("bad")
    steps = parsed.get("steps")
    if not isinstance(steps, list) or len(steps) != 3:
        raise ProviderInvalidOutput("steps!=3")
    clean_steps = []
    for s in steps:
        if not isinstance(s, dict):
            raise ProviderInvalidOutput("step type")
        title = str(s.get("title") or "").strip()
        action_text = str(s.get("action_text") or "").strip()
        rationale = str(s.get("rationale") or "").strip()
        risk_note = str(s.get("risk_note") or "").strip()
        if not title or len(title) > 160: raise ProviderInvalidOutput("title")
        if not action_text or len(action_text) > 2000: raise ProviderInvalidOutput("action")
        if not rationale or len(rationale) > 4000: raise ProviderInvalidOutput("rationale")
        if not risk_note or len(risk_note) > 2000: raise ProviderInvalidOutput("risk")
        assumptions = s.get("assumptions") or []
        if not isinstance(assumptions, list): raise ProviderInvalidOutput("assump")
        assumptions = [str(a)[:500] for a in assumptions][:5]
        ref = s.get("expert_referral")
        ref = str(ref).strip()[:1000] if ref else None
        clean_steps.append({"title": title, "action_text": action_text, "rationale": rationale, "assumptions": assumptions, "risk_note": risk_note, "expert_referral": ref})
    assumptions = parsed.get("assumptions") or []
    if not isinstance(assumptions, list): raise ProviderInvalidOutput("common assump")
    assumptions = [str(a)[:500] for a in assumptions][:10]
    questions = parsed.get("focused_questions") or []
    if not isinstance(questions, list) or len(questions) > 3: raise ProviderInvalidOutput("questions")
    questions = [str(q)[:300] for q in questions]
    is_general = bool(parsed.get("is_general", False))
    return clean_steps, assumptions, questions, is_general


def validate_provider_metadata(parsed):
    """Reject malformed provider metadata before any DB write (Blocking3).

    Booleans are rejected for token counts (bool subclasses int), as are
    oversized integers such as 2**100 and non-string / overlong model names
    supplied directly by a client that bypasses provider parsing.
    """
    if not isinstance(parsed, dict):
        raise ProviderInvalidOutput("bad")
    actual = parsed.get("_actual_model")
    if actual is not None:
        if not isinstance(actual, str) or not actual.strip() or len(actual) > 120:
            raise ProviderInvalidOutput("model metadata")
    for key in ("_usage_input_tokens", "_usage_output_tokens"):
        v = parsed.get(key)
        if v is not None and (type(v) is not int or v < 0 or v > MAX_USAGE_TOKENS):
            raise ProviderInvalidOutput("usage metadata")
    if "is_general" in parsed and not isinstance(parsed.get("is_general"), bool):
        raise ProviderInvalidOutput("is_general metadata")


def update_farm_profile(farm, cleaned_data):
    """Save a profile edit with a monotonic version (Blocking1).

    The farm is re-fetched inside the (IMMEDIATE) transaction, so a stale
    caller-held instance cannot resurrect an old profile_version and create
    a duplicate version / mismatched snapshot. Writers are serialized by
    the transaction; field values are last-writer-wins, the version is not.
    """
    with transaction.atomic():
        fresh = Farm.objects.get(pk=farm.pk)
        for field in ["name", *PROFILE_TEXT_FIELDS]:
            if field in cleaned_data:
                setattr(fresh, field, cleaned_data[field])
        fresh.profile_version = fresh.profile_version + 1
        fresh.save()
        return fresh


def _ordered_extras_list(farm):
    """Ordered extras for export/snapshot: [{"key","value"}] by position,id."""
    return [
        {"key": e.key, "value": e.value}
        for e in FarmExtraField.objects.filter(farm=farm).order_by("position", "id")
    ]


def _validate_extra_pairs(pairs):
    """Boundary invariants (Task 1): at most 20 rows, key<=64, value<=500,
    non-blank label, Unicode casefold uniqueness. Raises ValidationError."""
    from django.core.exceptions import ValidationError as _VE

    rows = [((k or "").strip(), (v or "").strip()) for k, v in pairs]
    rows = [(k, v) for k, v in rows if k or v]
    if len(rows) > 20:
        raise _VE(_("Legfeljebb 20 saját mező vehető fel."))
    seen = set()
    for k, v in rows:
        if not k:
            raise _VE(_("A megnevezés kötelező."))
        if len(k) > 64 or len(v) > 500:
            raise _VE(_("Túl hosszú saját mező (megnevezés legfeljebb 64, érték legfeljebb 500 karakter)."))
        norm = k.casefold()
        if norm in seen:
            raise _VE(_("Ez a megnevezés már szerepel."))
        seen.add(norm)


def save_farm_profile_with_extras(farm, core_cleaned_data, formset):
    """Atomic combined core+extras save (Task 1).

    Caller must validate BOTH the core form and the extras formset before
    calling (validate-both-first); this helper re-checks tamper (any row id
    not belonging to this farm) and performs deletes-first plus a temp-key
    rename pass to avoid transient Lower(key) unique collisions on swaps.
    Bumps profile_version exactly once. IntegrityError becomes a friendly
    ValidationError so the view re-renders instead of 500. Returns fresh farm.
    """
    from django.core.exceptions import ValidationError as _VE

    with transaction.atomic():
        fresh = Farm.objects.get(pk=farm.pk)
        own_ids = set(
            FarmExtraField.objects.filter(farm=farm).values_list("pk", flat=True)
        )
        # Tamper check: any bound row id outside this farm is rejected and
        # the whole transaction rolls back (no partial writes).
        for f in getattr(formset, "forms", []):
            inst = getattr(f, "instance", None)
            pk = getattr(inst, "pk", None)
            if pk is not None:
                try:
                    in_own = FarmExtraField.objects.filter(pk=pk, farm=fresh).exists()
                except Exception:
                    in_own = False
                if not in_own:
                    raise _VE(_("Érvénytelen mezőazonosító."))
        # Deletes first (avoids unique collisions with renamed rows).
        for f in getattr(formset, "forms", []):
            cd = getattr(f, "cleaned_data", None)
            if not cd or not cd.get("DELETE"):
                continue
            inst = getattr(f, "instance", None)
            if inst is not None and inst.pk in own_ids:
                FarmExtraField.objects.filter(pk=inst.pk, farm=fresh).delete()
        # Collect surviving rows in form order (updates then creates).
        survivors = []
        for f in getattr(formset, "forms", []):
            cd = getattr(f, "cleaned_data", None)
            if not cd or cd.get("DELETE"):
                continue
            key = (cd.get("key") or "").strip()
            value = (cd.get("value") or "").strip()
            if not key and not value:
                continue  # blank extra row: no-op
            inst = getattr(f, "instance", None)
            survivors.append((inst if inst is not None and inst.pk in own_ids else None, key, value))
        # Boundary invariants BEFORE any write (full rollback on violation).
        _validate_extra_pairs([(k, v) for _inst, k, v in survivors])
        try:
            with transaction.atomic():
                # Temp-key pass: move touched rows aside so swaps (A<->B)
                # never collide transiently on the Lower(key) constraint.
                import uuid as _uuid

                touched = [inst for inst, _k, _v in survivors if inst is not None]
                for inst in touched:
                    FarmExtraField.objects.filter(pk=inst.pk, farm=fresh).update(
                        key="__tmp-%s" % _uuid.uuid4().hex[:12]
                    )
                for pos, (inst, key, value) in enumerate(survivors):
                    if inst is not None:
                        FarmExtraField.objects.filter(pk=inst.pk, farm=fresh).update(
                            key=key, value=value, position=pos
                        )
                    else:
                        FarmExtraField.objects.create(
                            farm=fresh, key=key, value=value, position=pos
                        )
        except IntegrityError:
            raise _VE(_("Ez a megnevezés már szerepel."))
        for field in ["name", *PROFILE_TEXT_FIELDS]:
            if field in (core_cleaned_data or {}):
                setattr(fresh, field, core_cleaned_data[field])
        fresh.profile_version = fresh.profile_version + 1
        fresh.save()
        return fresh


def create_farm_with_extras(user, core_cleaned_data, extra_rows):
    """Create a farm plus its extras atomically (fresh create flow).

    extra_rows: iterable of (key, value) already validated/stripped.
    Bumps nothing (new farm starts at version 1).
    """
    from django.core.exceptions import ValidationError as _VE

    name = (core_cleaned_data.get("name") or "").strip()
    pairs = [((k or "").strip(), (v or "").strip()) for k, v in (extra_rows or [])]
    pairs = [(k, v) for k, v in pairs if k or v]
    with transaction.atomic():
        _validate_extra_pairs(pairs)
        try:
            farm = Farm.objects.create(
                owner=user,
                name=name,
                **{f: core_cleaned_data.get(f) for f in PROFILE_TEXT_FIELDS if f in core_cleaned_data},
            )
            for pos, (k, v) in enumerate(pairs):
                FarmExtraField.objects.create(farm=farm, key=k, value=v, position=pos)
        except IntegrityError:
            raise _VE(_("Ez a megnevezés már szerepel."))
        return farm


def pending_deadline():
    """Safe pending deadline: never shorter than provider timeout + 60s.

    Returns a timedelta of max(stale setting, timeout + 60s) so a live
    job is never falsely reported as interrupted.
    """
    try:
        _timeout = float(settings.ADVICE_REQUEST_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        _timeout = 30.0
    try:
        _stale_min = int(settings.ADVICE_PENDING_STALE_MINUTES)
    except (TypeError, ValueError):
        _stale_min = 15
    return max(timedelta(minutes=_stale_min), timedelta(seconds=_timeout + 60))


def advice_phase(run):
    """Explicit label for a run; DB status stays pending|success|failed.

    queued: pending, never dispatch-authorized. running: pending and
    dispatch-authorized (provider call in flight or orphaned). error:
    failed. done: success.
    """
    status = getattr(run, "status", None)
    if status == "success":
        return "done"
    if status == "failed":
        return "error"
    if getattr(run, "dispatch_authorized_at", None) is not None:
        return "running"
    return "queued"


def is_pending_overdue(run):
    """Derived overdue check with no DB writes (read-only status path).

    Only a dispatch-AUTHORIZED run can be overdue: the claim timestamp
    (dispatch_authorized_at) bounds liveness. An unclaimed (merely
    reserved) run is never overdue; it stays queued until a worker
    claims it, even across restarts.
    """
    if run.status != "pending":
        return False
    claimed_at = getattr(run, "dispatch_authorized_at", None)
    if claimed_at is None:
        return False
    return (timezone.now() - claimed_at) > pending_deadline()


def reap_stale_authorized(now=None, farm=None):
    """Shared stale reaper: fail stale AUTHORIZED orphans, no provider call.

    The single implementation behind both the per-farm reservation
    cleanup and the worker reaper, so the two can never disagree on the
    clock or the deadline. Only pending runs with a dispatch claim older
    than the safe deadline are finalized (never re-dispatched:
    double-bill risk). Unclaimed runs are left queued unconditionally,
    so a restart never loses them. Returns the number of runs reaped.
    """
    now = now or timezone.now()
    cutoff = now - pending_deadline()
    qs = AdviceRun.objects.filter(
        status="pending",
        dispatch_authorized_at__isnull=False,
        dispatch_authorized_at__lt=cutoff,
    )
    if farm is not None:
        qs = qs.filter(farm_id=farm.pk if hasattr(farm, "pk") else farm)
    return qs.update(
        status="failed",
        error_code="attempt_interrupted",
        error_message_safe=str(SAFE_MESSAGES["attempt_interrupted"]),
        completed_at=now,
    )


def cleanup_stale_runs(farm):
    """Per-farm wrapper over reap_stale_authorized in its OWN transaction.

    Committed independently of any later reservation, so the cleanup
    persists even when that reservation is rejected (Blocking5).
    """
    with transaction.atomic():
        return reap_stale_authorized(farm=farm)


# Backwards-compatible alias.
cleanup_stale = cleanup_stale_runs


def _is_stale(run):
    """True only for an authorized run past the safe dispatch deadline."""
    if run.status != "pending":
        return False
    claimed_at = getattr(run, "dispatch_authorized_at", None)
    if claimed_at is None:
        return False
    cutoff = timezone.now() - pending_deadline()
    return claimed_at < cutoff


def _reserve_advice_run(user, farm, idempotency_key):
    """Reservation only. Returns (run, created).

    Reservation is NOT dispatch authorization: it creates a pending run
    (or replays an existing one) but never calls the provider and never
    sets dispatch_authorized_at. The farm is re-fetched and ownership is
    validated inside the IMMEDIATE reservation transaction (Blocking1).
    """
    if not idempotency_key or len(idempotency_key) > 80:
        raise AdviceRejected("invalid_input")
    # Cleanup first, in its own committed transaction (Blocking5).
    # Only authorized orphans are finalized; unclaimed queue survives.
    cleanup_stale_runs(farm)
    with transaction.atomic():
        try:
            fresh_farm = Farm.objects.select_for_update().get(pk=farm.pk)
        except Farm.DoesNotExist:
            raise AdviceRejected("invalid_input")
        if _role_for(user, fresh_farm) not in ("owner", "manager"):
            raise AdviceRejected("invalid_input")
        existing = AdviceRun.objects.filter(farm_id=fresh_farm.pk, idempotency_key=idempotency_key).first()
        if existing:
            if existing.status != "pending":
                return existing, False
            if _is_stale(existing):
                AdviceRun.objects.filter(pk=existing.pk).update(
                    status="failed", error_code="attempt_interrupted",
                    error_message_safe=SAFE_MESSAGES["attempt_interrupted"],
                    completed_at=timezone.now())
                existing.refresh_from_db()
                return existing, False
            return existing, False
        # Per-farm single pending queue: a distinct key while a run is
        # pending returns that run (same status_url), charging no quota.
        queued = AdviceRun.objects.filter(
            farm_id=fresh_farm.pk, status="pending"
        ).order_by("attempt_started_at").first()
        if queued is not None:
            return queued, False
        # Preliminary consent check at reservation time. This is not the
        # dispatch authorization; consent is re-loaded at that boundary.
        if not current_consent_granted(fresh_farm):
            raise AdviceRejected("consent_required")
        from .quota import check_combined_quotas, QuotaExceeded
        try:
            check_combined_quotas(fresh_farm)
        except QuotaExceeded as _qe:
            raise AdviceRejected(_qe.code)
        _base_json = fresh_farm.profile_dict()
        _extras = _ordered_extras_list(fresh_farm)
        if _extras:
            _base_json = {**_base_json, "extras": _extras}
        snapshot, _ = FarmProfileSnapshot.objects.get_or_create(
            farm=fresh_farm, profile_version=fresh_farm.profile_version,
            defaults={"profile_json": _base_json, "profile_schema_version": 1})
        _lang = (get_language() or "hu").lower()
        if _lang not in ("hu", "en"):
            _lang = "hu"
        run = AdviceRun.objects.create(
            farm=fresh_farm, initiated_by=user, profile_snapshot=snapshot,
            idempotency_key=idempotency_key, language=_lang, status="pending", source="live",
            requested_model=settings.META_MODEL, prompt_version=settings.PROMPT_VERSION,
            output_schema_version=settings.OUTPUT_SCHEMA_VERSION, attempt_started_at=timezone.now())
        return run, True


def reserve_advice_run(user, farm, idempotency_key):
    run, _created = _reserve_advice_run(user, farm, idempotency_key)
    return run


def authorize_dispatch(run):
    """Explicit dispatch authorization boundary (Blocking2).

    In a short transaction, re-loads the CURRENT consent. If granted,
    records the authorizing event on the run (timestamp + consent event
    id) and returns True. If revoked since reservation, marks the run
    failed and returns False; the provider must then never be called.
    Once True, the run is dispatch-authorized: the call may be sent, an
    in-flight call cannot be unsent, and later revocation applies only
    to future calls. No network I/O happens in this transaction.
    """
    with transaction.atomic():
        fresh = AdviceRun.objects.select_related("farm", "initiated_by").get(pk=run.pk)
        if fresh.status != "pending":
            return False
        if fresh.dispatch_authorized_at is not None:
            # Re-authorization never re-grants: dispatch_authorized_at is
            # the atomic exactly-once claim, so only the first winner
            # proceeds to the provider call.
            return False
        if _role_for(fresh.initiated_by, fresh.farm) not in ("owner", "manager"):
            AdviceRun.objects.filter(pk=fresh.pk).update(
                status="failed", error_code="invalid_input",
                error_message_safe=SAFE_MESSAGES["invalid_input"],
                completed_at=timezone.now())
            return False
        ev = latest_consent_event(fresh.farm)
        if not (ev and ev.event_type == "granted"):
            AdviceRun.objects.filter(pk=fresh.pk).update(
                status="failed", error_code="consent_withdrawn_before_call",
                error_message_safe=SAFE_MESSAGES["consent_withdrawn_before_call"],
                completed_at=timezone.now())
            return False
        fresh.dispatch_authorized_at = timezone.now()
        fresh.dispatch_consent_event_id = ev.pk
        fresh.save(update_fields=["dispatch_authorized_at", "dispatch_consent_event_id", "updated_at"])
        return True


def execute_reserved_run(run, client=None):
    """Authorize, then call the provider OUTSIDE any DB transaction, then finalize."""
    fresh = AdviceRun.objects.select_related("farm", "profile_snapshot").get(pk=run.pk)
    if fresh.status != "pending":
        return fresh
    # Never skip the claim: authorize_dispatch is the atomic exactly-once
    # gate, so an already-authorized run fails here and never double-calls.
    if not authorize_dispatch(fresh):
        return AdviceRun.objects.get(pk=run.pk)
    fresh.refresh_from_db()
    client = client or MetaProviderClient()
    profile = dict(fresh.profile_snapshot.profile_json)
    profile["response_language"] = fresh.language or "hu"
    # --- Provider call outside any DB transaction (network I/O) ---
    try:
        import inspect as _inspect
        try:
            _params = _inspect.signature(client.generate_advice).parameters
            _accepts = ("language" in _params) or any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in _params.values()) or len([p for p in _params.values() if p.kind in (_inspect.Parameter.POSITIONAL_ONLY, _inspect.Parameter.POSITIONAL_OR_KEYWORD)]) >= 2
        except (TypeError, ValueError):
            _accepts = False
        parsed = client.generate_advice(profile, language=fresh.language) if _accepts else client.generate_advice(profile)
    except ProviderTimeout:
        _fail(run, "provider_timeout")
        return AdviceRun.objects.get(pk=run.pk)
    except ProviderInvalidOutput:
        _fail(run, "invalid_structured_output")
        return AdviceRun.objects.get(pk=run.pk)
    except ProviderError:
        _fail(run, "provider_error")
        return AdviceRun.objects.get(pk=run.pk)
    except Exception:
        _fail(run, "provider_error")
        return AdviceRun.objects.get(pk=run.pk)
    try:
        steps, assumptions, questions, is_general = validate_parsed(parsed)
        validate_provider_metadata(parsed)
    except ProviderInvalidOutput:
        _fail(run, "invalid_structured_output")
        return AdviceRun.objects.get(pk=run.pk)
    if screen_parsed(parsed):
        _fail(run, "prohibited_content")
        return AdviceRun.objects.get(pk=run.pk)
    # Role revocation is NOT honest-inflight: a revoked requester must leak nothing.
    _role_now = _role_for(
        __import__("django.contrib.auth", fromlist=["get_user_model"]).get_user_model().objects.filter(pk=fresh.initiated_by_id).first(),
        Farm.objects.filter(pk=fresh.farm_id).first())
    if _role_now not in ("owner", "manager"):
        _fail(run, "invalid_input")
        return AdviceRun.objects.get(pk=run.pk)
    # Honest semantics: revocation after the authorization boundary does
    # not discard this already-authorized, already-sent call.
    farm_now = Farm.objects.filter(pk=fresh.farm_id).first()
    if farm_now is not None and len(farm_now.completeness_missing()) >= 4:
        is_general = True
    try:
        with transaction.atomic():
            fresh2 = AdviceRun.objects.select_related("farm").get(pk=run.pk)
            if fresh2.status != "pending":
                return fresh2
            for i, s in enumerate(steps, start=1):
                AdviceStep.objects.create(advice_run=fresh2, farm=fresh2.farm, position=i, title=s["title"], action_text=s["action_text"], rationale=s["rationale"], assumptions=s["assumptions"], risk_note=s["risk_note"], expert_referral=s["expert_referral"])
            fresh2.result_assumptions = assumptions
            fresh2.result_focused_questions = questions
            fresh2.result_is_general = is_general
            fresh2.actual_model = (parsed.get("_actual_model") or None)
            fresh2.usage_input_tokens = parsed.get("_usage_input_tokens")
            fresh2.usage_output_tokens = parsed.get("_usage_output_tokens")
            fresh2.status = "success"
            fresh2.completed_at = timezone.now()
            fresh2.save()
            return fresh2
    except Exception:
        # The finalize transaction rolled back, so no partial steps remain.
        # Record a sanitized terminal failure in a separate transaction;
        # the run must never be left pending by a finalization error.
        try:
            AdviceStep.objects.filter(advice_run_id=run.pk).delete()
        except Exception:
            pass
        _fail(run, "provider_error")
        return AdviceRun.objects.get(pk=run.pk)


def request_advice(user, farm, idempotency_key, client=None):
    """Reserve -> authorize dispatch -> call outside tx -> finalize.

    Raises AdviceRejected for quota/consent/owner problems detected at
    reservation (no run is created then, but stale cleanup persists).
    A replayed non-pending run is returned as-is with no provider call.
    """
    if _role_for(user, farm) not in ("owner", "manager"):
        raise AdviceRejected("Tanácsot csak a tulajdonos vagy a menedzser kérhet.")
    run, created = _reserve_advice_run(user, farm, idempotency_key)
    if run.status != "pending" or not created:
        return run
    return execute_reserved_run(run, client=client)


def _fail(run, code):
    AdviceRun.objects.filter(pk=run.pk).update(status="failed", error_code=code, error_message_safe=SAFE_MESSAGES.get(code, _("Hiba történt.")), completed_at=timezone.now())


def new_idempotency_key():
    return uuid.uuid4().hex
