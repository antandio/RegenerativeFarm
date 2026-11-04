"""Sowing reconciliation service: DB-only enqueue plus safe comparison.

English comments per project convention. No secrets are logged here.
External provider calls stay mocked in tests; production uses the existing
configured MetaProviderClient with a minimal data-only payload.
"""
import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import get_language

logger = logging.getLogger(__name__)

# Review states that consume the actual sowing: one event hides at most one
# plan through either the model match path or the manual hide path.
CONSUMED_REVIEW_STATES = ("resolved_matched", "dismissed_deleted")

SOWING_WINDOW_DAYS = 30
SOWING_DECISIONS = ("match", "mismatch", "uncertain")
SOWING_REASONS = (
    "crop_date_match",
    "crop_mismatch",
    "date_mismatch",
    "ambiguous",
    "provider_uncertain",
)
SOWING_ALLOWED_KEYS = frozenset({
    "decision", "planned_cycle_id", "reason_code",
    "_actual_model", "_usage_input_tokens", "_usage_output_tokens",
})
SOWING_DECISION_REASONS = {
    "match": frozenset({"crop_date_match"}),
    "mismatch": frozenset({"crop_mismatch", "date_mismatch"}),
    "uncertain": frozenset({"ambiguous", "provider_uncertain"}),
}


def canonical_input_hash(farm_id, event_id, property_id, crop_snapshot,
                         event_date, plan_id, plan_crop, plan_sow_date):
    """Stable hash over crop/date identity only; free text never feeds it."""
    def _iso(v):
        try:
            return v.isoformat() if hasattr(v, "isoformat") else str(v)
        except Exception:
            return str(v)
    payload = {
        "farm": farm_id,
        "event": event_id,
        "property": property_id,
        "crop": (crop_snapshot or "").strip(),
        "event_date": _iso(event_date),
        "plan": plan_id,
        "plan_crop": (plan_crop or "").strip(),
        "plan_date": _iso(plan_sow_date),
    }
    dump = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _linked_valid(farm, event, linked):
    if linked is None:
        return False
    if linked.farm_id != farm.pk:
        return False
    if linked.property_id != event.property_id:
        return False
    if linked.state != "planned":
        return False
    if linked.is_hidden:
        return False
    if linked.sow_date is None or event.event_date is None:
        return False
    return abs((linked.sow_date - event.event_date).days) <= SOWING_WINDOW_DAYS


def find_candidate(farm, event):
    """Prefer the explicit linked plan, else the nearest dated plan in window."""
    from .models import CropCycle
    if farm is None or event is None:
        return None
    if getattr(event, "pk", None) is None:
        return None
    if event.farm_id != farm.pk:
        return None
    if event.event_type != "sowing":
        return None
    if event.event_date is None:
        return None
    linked_id = getattr(event, "crop_cycle_id", None)
    if linked_id:
        try:
            linked = CropCycle.objects.get(pk=linked_id)
        except CropCycle.DoesNotExist:
            linked = None
        if linked is not None and _linked_valid(farm, event, linked):
            return linked
    low = event.event_date - timedelta(days=SOWING_WINDOW_DAYS)
    high = event.event_date + timedelta(days=SOWING_WINDOW_DAYS)
    qs = CropCycle.objects.filter(
        farm_id=farm.pk, property_id=event.property_id, state="planned",
        is_hidden=False, sow_date__isnull=False,
        sow_date__gte=low, sow_date__lte=high).order_by("sow_date", "id")
    best = None
    best_key = None
    for cand in qs:
        if cand.farm_id != farm.pk or cand.property_id != event.property_id:
            continue
        diff = abs((cand.sow_date - event.event_date).days)
        if diff > SOWING_WINDOW_DAYS:
            continue
        # Nearest date wins; an exact date tie goes to the most recently
        # created candidate, which reflects the latest planning intent.
        key = (diff, cand.sow_date, -cand.pk)
        if best_key is None or key < best_key:
            best_key = key
            best = cand
    return best


def _eligible_for_dispatch(actor, farm):
    from .models import current_consent_granted
    from .permissions import role_for
    try:
        if role_for(actor, farm) not in ("owner", "manager"):
            return False
        return bool(current_consent_granted(farm))
    except Exception:
        return False


def _queued_stale(queued_at):
    """True when a scheduling reservation outlived the safe deadline."""
    if queued_at is None:
        return False
    from .services import pending_deadline
    try:
        return (timezone.now() - queued_at) > pending_deadline()
    except Exception:
        return True


def _queued_fresh(queued_at):
    """True when a queued reservation is still within its deadline."""
    return queued_at is not None and not _queued_stale(queued_at)


def _schedule_now(pk):
    """Schedule once after the reservation transaction has committed.

    Returns True only when the worker accepted the job. Saturation
    releases the reservation inside the worker module and returns False,
    leaving the comparison pending and recoverable.
    """
    try:
        from .sowing_jobs import schedule_sowing_comparison
        return bool(schedule_sowing_comparison(pk))
    except Exception:
        logger.error("sowing schedule failed")
        return False


def _schedule_on_commit(pk):
    def _cb():
        try:
            from .sowing_jobs import schedule_sowing_comparison
            schedule_sowing_comparison(pk)
        except Exception:
            logger.error("sowing schedule callback failed")
            try:
                from .sowing_jobs import _fail_safe
                _fail_safe(pk)
            except Exception:
                pass
    try:
        transaction.on_commit(_cb)
    except Exception:
        logger.error("sowing on_commit registration failed")


def enqueue_sowing_reconciliation(farm, event, actor):
    """Create one pending comparison after a journal save (DB only).

    Never raises and never performs network I/O; scheduling goes through
    transaction.on_commit so the journal save cannot break.
    """
    from .models import SowingComparison
    from .services import SAFE_MESSAGES
    try:
        if farm is None or event is None or actor is None:
            return None
        if getattr(event, "pk", None) is None:
            return None
        if event.farm_id != farm.pk:
            return None
        if event.event_type != "sowing":
            return None
        crop = (getattr(event, "crop_name_snapshot", "") or "").strip()
        if not crop or event.event_date is None:
            return None
        # One actual consumes at most one plan: a previously consumed event
        # (model match or manual hide) blocks any new comparison for this
        # event, even after later edits that would otherwise pick the next
        # future plan. Evidence stays.
        resolved = SowingComparison.objects.filter(
            farm_id=farm.pk, farming_event_id=event.pk, status="success",
            review_state__in=CONSUMED_REVIEW_STATES).first()
        if resolved is not None:
            return resolved
        cand = find_candidate(farm, event)
        if cand is None:
            return None
        if cand.farm_id != farm.pk or cand.property_id != event.property_id:
            return None
        ihash = canonical_input_hash(
            farm.pk, event.pk, event.property_id, crop, event.event_date,
            cand.pk, (cand.crop_name or "").strip(), cand.sow_date)
        key = "sow:%s:%s:%s" % (event.pk, cand.pk, ihash[:16])
        existing = SowingComparison.objects.filter(
            farm_id=farm.pk, idempotency_key=key).first()
        if existing is not None:
            if (existing.status == "pending"
                    and existing.dispatch_authorized_at is None
                    and not _queued_fresh(existing.queued_at)
                    and _eligible_for_dispatch(actor, farm)):
                SowingComparison.objects.filter(pk=existing.pk).update(
                    queued_at=timezone.now())
                _schedule_on_commit(existing.pk)
            return existing
        with transaction.atomic():
            raced = SowingComparison.objects.filter(
                farm_id=farm.pk, idempotency_key=key).first()
            if raced is not None:
                if (raced.status == "pending"
                        and raced.dispatch_authorized_at is None
                        and not _queued_fresh(raced.queued_at)
                        and _eligible_for_dispatch(actor, farm)):
                    SowingComparison.objects.filter(pk=raced.pk).update(
                        queued_at=timezone.now())
                    _schedule_on_commit(raced.pk)
                return raced
            now = timezone.now()
            # Stale pending rows for the same event are superseded; resolved
            # or mismatch reviews are never touched here.
            SowingComparison.objects.filter(
                farm_id=farm.pk, farming_event_id=event.pk, status="pending",
                review_state="pending_review",
                dispatch_authorized_at__isnull=True).exclude(
                idempotency_key=key).update(
                status="superseded", review_state="superseded",
                error_code="stale_superseded",
                error_message_safe=str(SAFE_MESSAGES.get("attempt_interrupted", "")),
                completed_at=now, queued_at=None)
            lang = (get_language() or "hu").lower()
            if lang not in ("hu", "en"):
                lang = "hu"
            eligible = _eligible_for_dispatch(actor, farm)
            row = SowingComparison.objects.create(
                farm_id=farm.pk, initiated_by=actor,
                farming_event_id=event.pk, planned_cycle_id=cand.pk,
                property_id=event.property_id,
                crop_snapshot=crop[:120], event_date=event.event_date,
                planned_crop_snapshot=(cand.crop_name or "").strip()[:120],
                planned_sow_date=cand.sow_date,
                input_hash=ihash, idempotency_key=key, language=lang,
                status="pending", review_state="pending_review",
                requested_model=settings.META_MODEL,
                attempt_started_at=now,
                queued_at=now if eligible else None)
            if eligible:
                _schedule_on_commit(row.pk)
            return row
    except Exception:
        logger.error("sowing enqueue failed")
        return None


def validate_sowing_parsed(parsed, expected_plan_id):
    """Strict allowlist validation for the comparison response."""
    from .provider import ProviderInvalidOutput
    from .services import validate_provider_metadata
    if not isinstance(parsed, dict) or isinstance(parsed, bool):
        raise ProviderInvalidOutput("bad")
    for key in parsed.keys():
        if key not in SOWING_ALLOWED_KEYS:
            raise ProviderInvalidOutput("unknown field")
    dec = parsed.get("decision")
    if not isinstance(dec, str) or isinstance(dec, bool):
        raise ProviderInvalidOutput("decision")
    if dec not in SOWING_DECISIONS:
        raise ProviderInvalidOutput("decision")
    pid = parsed.get("planned_cycle_id")
    if type(pid) is not int:
        raise ProviderInvalidOutput("plan id")
    if pid != expected_plan_id:
        raise ProviderInvalidOutput("plan id")
    reason = parsed.get("reason_code")
    if not isinstance(reason, str) or isinstance(reason, bool):
        raise ProviderInvalidOutput("reason")
    if reason not in SOWING_REASONS:
        raise ProviderInvalidOutput("reason")
    if reason not in SOWING_DECISION_REASONS[dec]:
        raise ProviderInvalidOutput("reason")
    validate_provider_metadata(parsed)
    return dec, reason, pid


def _fail_in_tx(pk, code):
    from .models import SowingComparison
    from .services import SAFE_MESSAGES
    SowingComparison.objects.filter(pk=pk).update(
        status="failed", review_state="pending_review", decision="",
        error_code=code,
        error_message_safe=str(SAFE_MESSAGES.get(code, "")),
        completed_at=timezone.now(), queued_at=None)


def _supersede_in_tx(pk):
    from .models import SowingComparison
    from .services import SAFE_MESSAGES
    SowingComparison.objects.filter(pk=pk).update(
        status="superseded", review_state="superseded",
        error_code="stale_superseded",
        error_message_safe=str(SAFE_MESSAGES.get("attempt_interrupted", "")),
        completed_at=timezone.now(), queued_at=None)


def _fail_attempt_in_tx(token, code):
    from .models import SowingComparisonAttempt
    from .services import SAFE_MESSAGES
    SowingComparisonAttempt.objects.filter(
        attempt_token=token, completed_at__isnull=True).update(
        status="failed", error_code=code,
        error_message_safe=str(SAFE_MESSAGES.get(code, "")),
        completed_at=timezone.now())


def _complete_attempt_in_tx(token, decision, reason, parsed):
    from .models import SowingComparisonAttempt
    SowingComparisonAttempt.objects.filter(
        attempt_token=token, completed_at__isnull=True).update(
        status="success", decision=decision, reason_code=reason,
        actual_model=parsed.get("_actual_model"),
        usage_input_tokens=parsed.get("_usage_input_tokens"),
        usage_output_tokens=parsed.get("_usage_output_tokens"),
        completed_at=timezone.now())


def _fail_outside(pk, code, attempt_token=None):
    with transaction.atomic():
        if attempt_token is not None:
            # Only the still-current attempt may fail the comparison; a late
            # error from a superseded attempt must not overwrite newer state.
            from .models import SowingComparison, SowingComparisonAttempt
            try:
                att = SowingComparisonAttempt.objects.get(
                    attempt_token=attempt_token)
            except SowingComparisonAttempt.DoesNotExist:
                att = None
            if att is not None:
                _fail_attempt_in_tx(attempt_token, code)
                current = SowingComparison.objects.filter(
                    pk=pk, current_attempt_id=att.pk,
                    status="pending").exists()
                if current:
                    _fail_in_tx(pk, code)
            else:
                _fail_in_tx(pk, code)
        else:
            _fail_in_tx(pk, code)


def _lock_event_for_update(event_id):
    """Per-event serialization lock shared by all consume paths.

    SQLite relies on IMMEDIATE transactions; the explicit row lock also
    serializes competing events on databases with row-level locking.
    Returns True when the event row exists and is locked.
    """
    from .models import FarmingEvent
    try:
        FarmingEvent.objects.select_for_update().get(pk=event_id)
        return True
    except FarmingEvent.DoesNotExist:
        return False
    except Exception:
        return False


def _event_consumed(farm_id, event_id, exclude_pk=None):
    from .models import SowingComparison
    qs = SowingComparison.objects.filter(
        farm_id=farm_id, farming_event_id=event_id, status="success",
        review_state__in=CONSUMED_REVIEW_STATES)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _pair_still_valid(row):
    """Scope and window re-check against current event and candidate rows."""
    from .models import CropCycle, FarmingEvent
    try:
        event = FarmingEvent.objects.get(pk=row.farming_event_id)
    except FarmingEvent.DoesNotExist:
        return False
    except Exception:
        return False
    if event.farm_id != row.farm_id:
        return False
    if event.event_type != "sowing":
        return False
    if event.property_id != row.property_id:
        return False
    if row.planned_cycle_id is None:
        return False
    try:
        cand = CropCycle.objects.get(pk=row.planned_cycle_id)
    except CropCycle.DoesNotExist:
        return False
    except Exception:
        return False
    if cand.farm_id != row.farm_id:
        return False
    if cand.property_id != event.property_id:
        return False
    if cand.state != "planned":
        return False
    if cand.is_hidden:
        return False
    if cand.sow_date is None or event.event_date is None:
        return False
    return abs((cand.sow_date - event.event_date).days) <= SOWING_WINDOW_DAYS


def authorize_sowing_dispatch(comparison):
    """Short atomic attempt claim; no network I/O in this transaction.

    Creates one immutable SowingComparisonAttempt per dispatch and points the
    comparison at it. A live (non-stale) current attempt blocks a second
    claim; a stale one is closed as interrupted before the fresh claim.
    Returns the new attempt on success, False when no dispatch is allowed.
    """
    from .models import (CropCycle, FarmingEvent, SowingComparison,
                         SowingComparisonAttempt, latest_consent_event)
    try:
        pk = comparison.pk if hasattr(comparison, "pk") else int(comparison)
    except Exception:
        return False
    with transaction.atomic():
        try:
            fresh = SowingComparison.objects.select_for_update().select_related(
                "farm", "initiated_by").get(pk=pk)
        except SowingComparison.DoesNotExist:
            return False
        except Exception:
            return False
        if fresh.status != "pending" or fresh.review_state != "pending_review":
            return False
        if not _lock_event_for_update(fresh.farming_event_id):
            _supersede_in_tx(fresh.pk)
            return False
        # A consumed actual never dispatches again: another row already hid
        # its one plan, so this stale/extra row closes without a paid call.
        if _event_consumed(fresh.farm_id, fresh.farming_event_id,
                           exclude_pk=fresh.pk):
            _supersede_in_tx(fresh.pk)
            return False
        # Live-attempt guard: staleness derives from the latest dispatch
        # timestamp, never from the enqueue time.
        if fresh.current_attempt_id is not None:
            try:
                cur = SowingComparisonAttempt.objects.get(
                    pk=fresh.current_attempt_id)
            except SowingComparisonAttempt.DoesNotExist:
                cur = None
            if cur is not None and cur.status == "pending":
                from .services import pending_deadline
                try:
                    stale = (timezone.now() - cur.dispatched_at) > pending_deadline()
                except Exception:
                    stale = True
                if not stale:
                    return False
                _fail_attempt_in_tx(cur.attempt_token, "attempt_interrupted")
        from .permissions import role_for
        try:
            role = role_for(fresh.initiated_by, fresh.farm)
        except Exception:
            role = None
        if role not in ("owner", "manager"):
            _fail_in_tx(fresh.pk, "invalid_input")
            return False
        try:
            ev = latest_consent_event(fresh.farm)
        except Exception:
            ev = None
        if not (ev and ev.event_type == "granted"):
            _fail_in_tx(fresh.pk, "consent_withdrawn_before_call")
            return False
        from .quota import QuotaExceeded, check_combined_quotas
        try:
            check_combined_quotas(fresh.farm)
        except QuotaExceeded as qe:
            _fail_in_tx(fresh.pk, qe.code)
            return False
        except Exception:
            _fail_in_tx(fresh.pk, "provider_error")
            return False
        if not _pair_still_valid(fresh):
            _supersede_in_tx(fresh.pk)
            return False
        try:
            event = FarmingEvent.objects.get(pk=fresh.farming_event_id)
            cand = CropCycle.objects.get(pk=fresh.planned_cycle_id)
        except Exception:
            _supersede_in_tx(fresh.pk)
            return False
        ihash = canonical_input_hash(
            fresh.farm_id, event.pk, event.property_id,
            (event.crop_name_snapshot or "").strip(), event.event_date,
            cand.pk, (cand.crop_name or "").strip(), cand.sow_date)
        if ihash != fresh.input_hash:
            _supersede_in_tx(fresh.pk)
            return False
        now = timezone.now()
        att = SowingComparisonAttempt.objects.create(
            comparison_id=fresh.pk, farm_id=fresh.farm_id,
            initiated_by_id=fresh.initiated_by_id,
            attempt_token=uuid.uuid4().hex, dispatched_at=now,
            dispatch_consent_event_id=ev.pk,
            requested_model=fresh.requested_model, status="pending")
        # Mirror the latest dispatch on the comparison; never cleared.
        # Claiming the attempt consumes the scheduling reservation, so
        # staleness from here on derives from the dispatch timestamp only.
        SowingComparison.objects.filter(pk=fresh.pk).update(
            dispatch_authorized_at=now, dispatch_consent_event_id=ev.pk,
            current_attempt_id=att.pk, queued_at=None)
        return att


def _finalize_decision(pk, decision, reason, parsed, attempt_token=None):
    from .models import (AuditEvent, CropCycle, FarmingEvent, SowingComparison,
                         SowingComparisonAttempt, latest_consent_event)
    try:
        with transaction.atomic():
            try:
                row = SowingComparison.objects.select_for_update().select_related(
                    "farm", "initiated_by").get(pk=pk)
            except SowingComparison.DoesNotExist:
                return None
            # Attempt guard: a late result from a superseded attempt must not
            # mutate the comparison or replace newer review state.
            if attempt_token is not None:
                try:
                    att = SowingComparisonAttempt.objects.get(
                        attempt_token=attempt_token)
                except SowingComparisonAttempt.DoesNotExist:
                    att = None
                if att is None or row.current_attempt_id != att.pk:
                    return SowingComparison.objects.get(pk=pk)
            if row.status != "pending":
                return row
            if not _lock_event_for_update(row.farming_event_id):
                if attempt_token is not None:
                    _fail_attempt_in_tx(attempt_token, "provider_error")
                _supersede_in_tx(pk)
                return SowingComparison.objects.get(pk=pk)
            from .permissions import role_for
            try:
                role = role_for(row.initiated_by, row.farm)
            except Exception:
                role = None
            if role not in ("owner", "manager"):
                if attempt_token is not None:
                    _fail_attempt_in_tx(attempt_token, "invalid_input")
                _fail_in_tx(pk, "invalid_input")
                return SowingComparison.objects.get(pk=pk)
            try:
                ev = latest_consent_event(row.farm)
            except Exception:
                ev = None
            if not (ev and ev.event_type == "granted"):
                if attempt_token is not None:
                    _fail_attempt_in_tx(attempt_token, "consent_withdrawn_before_call")
                _fail_in_tx(pk, "consent_withdrawn_before_call")
                return SowingComparison.objects.get(pk=pk)
            if not _pair_still_valid(row):
                if attempt_token is not None:
                    _complete_attempt_in_tx(attempt_token, decision, reason, parsed)
                _supersede_in_tx(pk)
                return SowingComparison.objects.get(pk=pk)
            try:
                event = FarmingEvent.objects.get(pk=row.farming_event_id)
                cand = CropCycle.objects.get(pk=row.planned_cycle_id)
            except Exception:
                if attempt_token is not None:
                    _complete_attempt_in_tx(attempt_token, decision, reason, parsed)
                _supersede_in_tx(pk)
                return SowingComparison.objects.get(pk=pk)
            ihash = canonical_input_hash(
                row.farm_id, event.pk, event.property_id,
                (event.crop_name_snapshot or "").strip(), event.event_date,
                cand.pk, (cand.crop_name or "").strip(), cand.sow_date)
            if ihash != row.input_hash:
                if attempt_token is not None:
                    _complete_attempt_in_tx(attempt_token, decision, reason, parsed)
                _supersede_in_tx(pk)
                return SowingComparison.objects.get(pk=pk)
            # Final mutation gate: never hide a second plan for one actual.
            if _event_consumed(row.farm_id, row.farming_event_id, exclude_pk=row.pk):
                if attempt_token is not None:
                    _complete_attempt_in_tx(attempt_token, decision, reason, parsed)
                _supersede_in_tx(pk)
                return SowingComparison.objects.get(pk=pk)
            now = timezone.now()
            actual_model = parsed.get("_actual_model")
            usage_in = parsed.get("_usage_input_tokens")
            usage_out = parsed.get("_usage_output_tokens")
            if attempt_token is not None:
                _complete_attempt_in_tx(attempt_token, decision, reason, parsed)
            if decision == "match":
                cand.is_hidden = True
                cand.hidden_at = now
                cand.hidden_reason = "sowing_match"
                cand.save()
                AuditEvent.objects.create(
                    farm=row.farm, actor=row.initiated_by,
                    action="sowing.match.hide", object_type="crop",
                    object_id=str(cand.pk),
                    meta={"event_id": row.farming_event_id,
                          "planned_cycle_id": cand.pk,
                          "decision": decision, "reason_code": reason})
                SowingComparison.objects.filter(pk=pk).update(
                    status="success", decision=decision, reason_code=reason,
                    review_state="resolved_matched",
                    actual_model=actual_model,
                    usage_input_tokens=usage_in,
                    usage_output_tokens=usage_out,
                    completed_at=now)
            elif decision == "mismatch":
                AuditEvent.objects.create(
                    farm=row.farm, actor=row.initiated_by,
                    action="sowing.mismatch.review", object_type="crop",
                    object_id=str(cand.pk),
                    meta={"event_id": row.farming_event_id,
                          "planned_cycle_id": cand.pk,
                          "decision": decision, "reason_code": reason})
                SowingComparison.objects.filter(pk=pk).update(
                    status="success", decision=decision, reason_code=reason,
                    review_state="confirmed_mismatch",
                    actual_model=actual_model,
                    usage_input_tokens=usage_in,
                    usage_output_tokens=usage_out,
                    completed_at=now)
            else:
                SowingComparison.objects.filter(pk=pk).update(
                    status="success", decision=decision, reason_code=reason,
                    review_state="pending_review",
                    actual_model=actual_model,
                    usage_input_tokens=usage_in,
                    usage_output_tokens=usage_out,
                    completed_at=now)
            return SowingComparison.objects.get(pk=pk)
    except Exception:
        logger.error("sowing finalize failed")
        try:
            _fail_outside(pk, "provider_error", attempt_token=attempt_token)
        except Exception:
            pass
        try:
            from .models import SowingComparison
            return SowingComparison.objects.get(pk=pk)
        except Exception:
            return None


def execute_sowing_comparison(comparison, client=None):
    """Claim, call the provider outside any transaction, then finalize."""
    from .models import CropCycle, FarmingEvent, SowingComparison
    from .provider import (MetaProviderClient, ProviderError,
                           ProviderInvalidOutput, ProviderTimeout)
    try:
        pk = comparison.pk if hasattr(comparison, "pk") else int(comparison)
    except Exception:
        return comparison
    try:
        fresh = SowingComparison.objects.select_related("farm").get(pk=pk)
    except SowingComparison.DoesNotExist:
        return comparison
    except Exception:
        return comparison
    if fresh.status != "pending":
        return fresh
    att = authorize_sowing_dispatch(fresh)
    if not att:
        try:
            return SowingComparison.objects.get(pk=pk)
        except Exception:
            return fresh
    token = att.attempt_token
    try:
        fresh = SowingComparison.objects.select_related("farm").get(pk=pk)
        event = FarmingEvent.objects.get(pk=fresh.farming_event_id)
        if fresh.planned_cycle_id is None:
            raise CropCycle.DoesNotExist()
        cand = CropCycle.objects.get(pk=fresh.planned_cycle_id)
    except Exception:
        with transaction.atomic():
            _fail_attempt_in_tx(token, "provider_error")
            _supersede_in_tx(pk)
        try:
            return SowingComparison.objects.get(pk=pk)
        except Exception:
            return fresh
    # Existence re-check only: abort before any HTTP call when the mutable
    # rows drifted from the authorized snapshot. The payload below uses the
    # stored snapshots exclusively, never the reread mutable values.
    ihash = canonical_input_hash(
        fresh.farm_id, event.pk, event.property_id,
        (event.crop_name_snapshot or "").strip(), event.event_date,
        cand.pk, (cand.crop_name or "").strip(), cand.sow_date)
    if ihash != fresh.input_hash:
        with transaction.atomic():
            _fail_attempt_in_tx(token, "provider_error")
            _supersede_in_tx(pk)
        try:
            return SowingComparison.objects.get(pk=pk)
        except Exception:
            return fresh
    actual_crop = (fresh.crop_snapshot or "").strip()
    actual_date = fresh.event_date.isoformat()
    planned_crop = (fresh.planned_crop_snapshot or "").strip()
    planned_date = fresh.planned_sow_date.isoformat()
    delta = (fresh.planned_sow_date - fresh.event_date).days
    plan_id = fresh.planned_cycle_id
    if client is None:
        client = MetaProviderClient()
    # Provider call outside any DB transaction (network I/O).
    try:
        parsed = client.generate_sowing_comparison(
            actual_crop, actual_date, plan_id, planned_crop,
            planned_date, delta, language=fresh.language)
    except ProviderTimeout:
        _fail_outside(pk, "provider_timeout", attempt_token=token)
        return SowingComparison.objects.get(pk=pk)
    except ProviderInvalidOutput:
        _fail_outside(pk, "invalid_structured_output", attempt_token=token)
        return SowingComparison.objects.get(pk=pk)
    except ProviderError:
        _fail_outside(pk, "provider_error", attempt_token=token)
        return SowingComparison.objects.get(pk=pk)
    except Exception:
        logger.error("sowing provider call failed")
        _fail_outside(pk, "provider_error", attempt_token=token)
        return SowingComparison.objects.get(pk=pk)
    try:
        decision, reason, _pid = validate_sowing_parsed(parsed, plan_id)
    except ProviderInvalidOutput:
        _fail_outside(pk, "invalid_structured_output", attempt_token=token)
        return SowingComparison.objects.get(pk=pk)
    except Exception:
        _fail_outside(pk, "invalid_structured_output", attempt_token=token)
        return SowingComparison.objects.get(pk=pk)
    done = _finalize_decision(pk, decision, reason, parsed, attempt_token=token)
    if done is None:
        try:
            return SowingComparison.objects.get(pk=pk)
        except Exception:
            return fresh
    return done


def _is_sowing_stale(row):
    """Staleness from the latest dispatch, never from enqueue time.

    Undispatched rows are never stale. Dispatched rows compare now against
    the current attempt dispatch plus the safe deadline (provider timeout
    plus buffer via pending_deadline).
    """
    from .services import pending_deadline
    try:
        attempt_id = getattr(row, "current_attempt_id", None)
        dispatched = None
        if attempt_id is not None:
            from .models import SowingComparisonAttempt
            try:
                dispatched = SowingComparisonAttempt.objects.filter(
                    pk=attempt_id).values_list(
                    "dispatched_at", flat=True).first()
            except Exception:
                dispatched = None
        if dispatched is None:
            dispatched = getattr(row, "dispatch_authorized_at", None)
        if dispatched is None:
            return False
        return (timezone.now() - dispatched) > pending_deadline()
    except Exception:
        return True


def _retry_supersede_and_reenqueue(user, farm, row):
    """Supersede an invalid row and try to enqueue its replacement.

    Returns (row_or_new, scheduled). A freshly enqueued replacement counts
    as scheduled when the retrying actor is eligible for dispatch.
    """
    from .models import FarmingEvent, SowingComparison
    SowingComparison.objects.filter(pk=row.pk).update(
        status="superseded", review_state="superseded", queued_at=None)
    try:
        event = FarmingEvent.objects.get(
            pk=row.farming_event_id, farm_id=farm.pk)
    except Exception:
        return SowingComparison.objects.get(pk=row.pk), False
    new = enqueue_sowing_reconciliation(farm, event, user)
    if new is not None and new.pk != row.pk:
        return new, bool(_eligible_for_dispatch(user, farm))
    return SowingComparison.objects.get(pk=row.pk), False


def _retry_hash_matches(row):
    from .models import CropCycle, FarmingEvent
    try:
        event = FarmingEvent.objects.get(pk=row.farming_event_id)
        cand = CropCycle.objects.get(pk=row.planned_cycle_id)
    except Exception:
        return False, None
    ihash = canonical_input_hash(
        row.farm_id, event.pk, event.property_id,
        (event.crop_name_snapshot or "").strip(), event.event_date,
        cand.pk, (cand.crop_name or "").strip(), cand.sow_date)
    return ihash == row.input_hash, event


def retry_sowing_comparison(user, farm, comparison_id):
    """Explicit owner/manager retry for failed, uncertain, or stale rows.

    Returns (row, scheduled): scheduled is True only when a fresh attempt
    was actually scheduled. The whole decision runs in one transaction
    holding the comparison row lock plus the per-event lock, and every
    reopen is a compare-and-swap on the expected status, so exactly one
    concurrent retry wins the queued_at reservation and schedules; the
    loser is a no-op returning (row, False) and can never resurrect a
    concurrently dismissed row. A live in-flight attempt and a fresh
    queued reservation both make retry a no-op; a stale queued
    reservation (callback lost) is recoverable here. Dispatch history is
    never cleared; the next execution claims a fresh immutable attempt.
    """
    from django.core.exceptions import PermissionDenied
    from django.http import Http404
    from .models import SowingComparison, SowingComparisonAttempt
    from .permissions import role_for
    if farm is None:
        raise Http404()
    if role_for(user, farm) not in ("owner", "manager"):
        raise PermissionDenied()
    schedule_pk = None
    with transaction.atomic():
        try:
            row = SowingComparison.objects.select_for_update().get(
                pk=comparison_id, farm_id=farm.pk)
        except SowingComparison.DoesNotExist:
            raise Http404()
        except Exception:
            raise Http404()
        _lock_event_for_update(row.farming_event_id)
        now = timezone.now()
        if row.status == "success":
            # Only an ambiguous outcome is retryable; terminal
            # match/mismatch and dismissals use retain/hide, not retry.
            if not (row.decision == "uncertain"
                    and row.review_state == "pending_review"):
                return row, False
            if not _pair_still_valid(row):
                return _retry_supersede_and_reenqueue(user, farm, row)
            ok, _event = _retry_hash_matches(row)
            if not ok:
                return _retry_supersede_and_reenqueue(user, farm, row)
            changed = SowingComparison.objects.filter(
                pk=row.pk, status="success", decision="uncertain",
                review_state="pending_review").update(
                initiated_by_id=user.pk, status="pending",
                review_state="pending_review", decision="", reason_code="",
                error_code=None, error_message_safe=None, completed_at=None,
                queued_at=now)
            if changed != 1:
                return SowingComparison.objects.get(pk=row.pk), False
            schedule_pk = row.pk
        elif row.status == "pending":
            # A live dispatched attempt owns the row: retry must neither
            # reschedule nor mistake the queued reservation for activity.
            if row.current_attempt_id is not None:
                try:
                    cur = SowingComparisonAttempt.objects.get(
                        pk=row.current_attempt_id)
                except SowingComparisonAttempt.DoesNotExist:
                    cur = None
                except Exception:
                    cur = None
                if (cur is not None and cur.status == "pending"
                        and not _is_sowing_stale(row)):
                    return row, False
            # Already queued within its deadline: duplicate is a no-op.
            if _queued_fresh(row.queued_at):
                return row, False
            if not _pair_still_valid(row):
                if row.current_attempt_id is None:
                    return _retry_supersede_and_reenqueue(user, farm, row)
                _supersede_in_tx(row.pk)
                return SowingComparison.objects.get(pk=row.pk), False
            ok, _event = _retry_hash_matches(row)
            if not ok:
                return _retry_supersede_and_reenqueue(user, farm, row)
            # Safe explicit retry adopts the actor: a worker-initiated
            # row later retried by an owner/manager dispatches under the
            # retrying actor, otherwise the worker role would block it.
            if row.current_attempt_id is None:
                changed = SowingComparison.objects.filter(
                    pk=row.pk, status="pending",
                    current_attempt_id__isnull=True).update(
                    initiated_by_id=user.pk, queued_at=now)
            else:
                changed = SowingComparison.objects.filter(
                    pk=row.pk, status="pending").update(
                    initiated_by_id=user.pk, error_code=None,
                    error_message_safe=None, queued_at=now)
            if changed != 1:
                return SowingComparison.objects.get(pk=row.pk), False
            schedule_pk = row.pk
        elif row.status == "superseded":
            from .models import FarmingEvent
            try:
                event = FarmingEvent.objects.get(
                    pk=row.farming_event_id, farm_id=farm.pk)
            except Exception:
                return row, False
            new = enqueue_sowing_reconciliation(farm, event, user)
            if new is not None and new.pk != row.pk:
                return new, bool(_eligible_for_dispatch(user, farm))
            return SowingComparison.objects.get(pk=row.pk), False
        else:
            # Failed rows with the same valid input reopen the review
            # identity; the next execution claims a fresh attempt.
            if not _pair_still_valid(row):
                return _retry_supersede_and_reenqueue(user, farm, row)
            ok, _event = _retry_hash_matches(row)
            if not ok:
                return _retry_supersede_and_reenqueue(user, farm, row)
            changed = SowingComparison.objects.filter(
                pk=row.pk, status="failed").update(
                initiated_by_id=user.pk, status="pending",
                review_state="pending_review", decision="", reason_code="",
                error_code=None, error_message_safe=None, completed_at=None,
                queued_at=now)
            if changed != 1:
                return SowingComparison.objects.get(pk=row.pk), False
            schedule_pk = row.pk
    # The reservation transaction has committed at this point (in tests
    # its savepoint is released): schedule exactly once for the winner.
    # Saturation releases the reservation and reports not scheduled.
    if schedule_pk is not None:
        scheduled = _schedule_now(schedule_pk)
        return SowingComparison.objects.get(pk=schedule_pk), scheduled
    return row, False
