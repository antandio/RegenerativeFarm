"""Durable chat worker: DB-backed claim queue, no web-process threads.

English comments per project convention. Mirrors portal/advice_worker.py.

Worker protocol (run_worker_once):
- Reap first, without any provider call: only AUTHORIZED orphans
  (pending, dispatch_authorized_at set, older than the safe dispatch
  deadline) are failed with outcome_unknown and never replayed. Unclaimed
  (unauthorized) rows are left queued unconditionally: they survive a
  restart and are processed when a worker next claims them, with consent
  re-checked at claim time.
- Claim next: the oldest unclaimed pending non-deleted row is claimed
  exactly once via claim_chat_dispatch (atomic conditional UPDATE); only
  the winner executes. A lost claim means another worker won, so this
  call stops without touching that row.
- Execute outside any DB transaction: rechecks active membership/role,
  consent and the immutable parcel snapshot, rebuilds the frozen farm
  context plus bounded own memory (10 rows, untrusted prior output),
  calls the provider, then saves transactionally with a tombstone race
  check so late answers never resurrect deleted rows.
- Fresh authorized rows (a live claim in flight) are left alone so a
  concurrent worker never fails a live run.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def build_worker_client():
    """Construct the real provider client from settings (key stays private)."""
    from .chat import MetaChatClient

    return MetaChatClient()


def reap_orphans(now=None):
    """Fail stale AUTHORIZED orphans only, no provider call.

    Returns the number of rows reaped. Unclaimed rows always survive;
    fresh authorized rows (a live claim) are never touched.
    """
    from .chat_models import ChatAttempt
    from .chat import SAFE_CHAT

    at = now or timezone.now()
    cutoff = at - timedelta(minutes=settings.ADVICE_PENDING_STALE_MINUTES)
    with transaction.atomic():
        return ChatAttempt.objects.filter(
            status="pending", deleted_at__isnull=True,
            dispatch_authorized_at__isnull=False,
            attempt_started_at__lt=cutoff,
        ).update(status="failed", error_code="outcome_unknown",
                 error_message_safe=str(SAFE_CHAT["outcome_unknown"]),
                 completed_at=at)


def claim_next(now=None):
    """Return the oldest unclaimed pending non-deleted row, or None.

    No freshness filter: a long-queued unclaimed row is still claimed
    and processed after a restart.
    """
    from .chat_models import ChatAttempt

    return (
        ChatAttempt.objects.filter(
            status="pending",
            dispatch_authorized_at__isnull=True,
            deleted_at__isnull=True,
        )
        .order_by("attempt_started_at")
        .first()
    )


def build_worker_context(run):
    """Rebuild provider input: frozen parcel scope + bounded own memory.

    English comments per project convention. Runs outside any DB
    transaction. Raises ChatRejected when the immutable parcel snapshot
    no longer matches the current farm state.
    """
    from .chat import (build_chat_context, build_chat_memory,
                       format_chat_memory_block, _snapshot_selection,
                       _verify_subset_snapshot_current)
    from .models import Farm

    fresh_farm = Farm.objects.get(pk=run.farm_id)
    mode, ids = _snapshot_selection(run.selection_snapshot)
    if mode == "invalid":
        from .chat import ChatRejected
        raise ChatRejected("invalid_input")
    if mode == "subset":
        _verify_subset_snapshot_current(fresh_farm, run.selection_snapshot)
    _ctx, ctx_str = build_chat_context(
        fresh_farm, selected_property_ids=ids or None)
    mem = build_chat_memory(fresh_farm, run.initiated_by, exclude_pk=run.pk)
    block = format_chat_memory_block(mem, language=run.language)
    if block:
        ctx_str = ctx_str + "\n\n" + block
    return ctx_str


def run_worker_once(client=None):
    """Reap, claim and execute at most one run. Returns an outcome.

    Outcome is one of "success", "failed" (claimed run terminal state),
    "reaped" (only stale work was failed), or "idle" (nothing to do).
    Provider crashes fail the claimed run safely without leaking secrets.
    A deleted row is skipped before any provider call.
    """
    from .chat import (authorize_chat_dispatch, claim_chat_dispatch,
                       execute_chat_attempt, ChatRejected, _fail_attempt,
                       _snapshot_selection)
    from .chat_models import ChatAttempt
    from .models import Farm, current_consent_granted, latest_consent_event
    from .permissions import role_for

    reaped = reap_orphans()
    if reaped:
        return "reaped"
    run = claim_next()
    if run is None:
        return "idle"
    run_id = run.pk
    try:
        current = ChatAttempt.objects.select_related("farm").get(pk=run_id)
        if current.deleted_at is not None or current.status != "pending":
            return "idle"
        # Pre-claim rechecks on fresh rows: active membership/role,
        # current consent and the frozen parcel snapshot. Violations fail
        # safely without any provider call.
        farm = Farm.objects.filter(pk=current.farm_id).first()
        user = current.initiated_by
        if (farm is None
                or role_for(user, farm) not in ("owner", "manager", "worker")
                or not current_consent_granted(farm)):
            if not claim_chat_dispatch(current):
                return "idle"
            code = ("consent_withdrawn_before_call"
                    if farm is not None and current_consent_granted(farm)
                    else ("forbidden"
                          if farm is not None and role_for(user, farm) not in
                          ("owner", "manager", "worker")
                          else "consent_withdrawn_before_call"))
            _fail_attempt(current, code)
            return "failed"
        _mode, _ids = _snapshot_selection(current.selection_snapshot)
        if _mode == "invalid":
            if not claim_chat_dispatch(current):
                return "idle"
            _fail_attempt(current, "invalid_input")
            return "failed"
        if _mode == "subset":
            try:
                from .chat import _verify_subset_snapshot_current
                _verify_subset_snapshot_current(farm, current.selection_snapshot)
            except Exception:
                if not claim_chat_dispatch(current):
                    return "idle"
                _fail_attempt(current, "invalid_input")
                return "failed"
        # Exactly-once claim; only the winner executes.
        if not claim_chat_dispatch(current):
            return "idle"
        # Record the consent event backing this dispatch for audit.
        ev = latest_consent_event(farm)
        if ev is not None:
            ChatAttempt.objects.filter(pk=run_id).update(
                dispatch_consent_event_id=ev.pk)
        context_str = build_worker_context(
            ChatAttempt.objects.get(pk=run_id))
        try:
            done = execute_chat_attempt(
                ChatAttempt.objects.get(pk=run_id),
                client=client if client is not None else build_worker_client(),
                context_str=context_str)
        except ChatRejected as exc:
            _fail_attempt(current, exc.code)
            return "failed"
    except Exception:
        logger.error("chat worker crashed")
        try:
            from .chat import _fail_attempt as _safe_fail
            _safe_fail(ChatAttempt.objects.get(pk=run_id), "provider_error")
        except Exception:
            pass
        return "failed"
    if done.status == "success":
        return "success"
    if done.status == "failed":
        return "failed"
    return "idle"
