"""Durable agent worker: DB claim queue, no web-process threads.

English comments per project convention. Mirrors chat/advice workers.

Protocol (run_worker_once):
- Reap first, no provider call: only stale claimed rows (running with an
  expired lease, not deleted/cancelled) become outcome_unknown and are
  never replayed. Unclaimed queued rows survive and are processed later.
- Claim next: the oldest unclaimed queued non-deleted, non-cancelled row
  is claimed exactly once via an atomic conditional UPDATE (no
  select_for_update fiction); only the winner executes. Processing calls
  _advance directly, never start_run (which would hit the duplicate
  shortcut instead of doing the work).
- Execute outside any DB transaction with the real Spark client, one
  attempt per dispatch. Late writes never resurrect deleted/cancelled
  rows (guarded saves + tombstone re-read).
"""
import logging
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _lock_path():
    configured = getattr(settings, "AGENT_WORKER_LOCK_FILE", "") or ""
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "agent-worker.lock"


def _lease_seconds():
    try:
        return int(getattr(settings, "AGENT_WORKER_LEASE_SECONDS", 600) or 600)
    except Exception:
        return 600


def reap_stale(now=None):
    """Fail stale claimed rows only, no provider call. Returns count."""
    from portal.agent.models import AgentRun

    at = now or timezone.now()
    return AgentRun.objects.filter(
        status="running",
        lease_expires_at__lt=at,
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    ).update(status="outcome_unknown", stage=7,
             error_code="lease_orphan",
             error_message_safe="Lease expired; outcome unknown, no replay.",
             completed_at=at, updated_at=at)


def claim_next(now=None, lease_seconds=None):
    """Claim the oldest unclaimed queued run, or None.

    No freshness filter: a long-queued unclaimed row is still claimed
    and processed after a restart, with consent re-checked at claim.
    """
    from portal.agent.models import AgentRun

    at = now or timezone.now()
    lease = int(lease_seconds if lease_seconds is not None
                else _lease_seconds())
    row = (AgentRun.objects.filter(
        status="queued",
        dispatch_authorized_at__isnull=True,
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    ).order_by("created_at", "id").first())
    if row is None:
        return None
    dispatch_id = uuid.uuid4().hex
    updated = AgentRun.objects.filter(
        pk=row.pk, status="queued",
        dispatch_authorized_at__isnull=True,
        deleted_at__isnull=True,
        cancelled_at__isnull=True,
    ).update(status="running", stage=1,
             dispatch_authorized_at=at,
             lease_expires_at=at + timedelta(seconds=lease),
             updated_at=at)
    if not updated:
        return None
    claimed = AgentRun.objects.filter(pk=row.pk).first()
    if claimed is not None:
        try:
            snap = dict(claimed.input_snapshot or {})
        except Exception:
            snap = {}
        snap["dispatch_id"] = dispatch_id
        AgentRun.objects.filter(pk=row.pk).update(input_snapshot=snap)
        claimed.input_snapshot = snap
    return claimed


def _auto_prepare_receipt(run, farm, actor):
    """Prepare the technical immutable preview after verified commands.

    Never takes client commands: only the verified candidate's own
    commands are prepared, and only for preview/done runs.
    """
    try:
        from portal.agent import changeset as cs_mod
        commands = list(cs_mod._candidate_commands(run) or [])
    except Exception:
        return None
    if not commands:
        return None
    fresh = type(run).objects.filter(pk=run.pk).first()
    if fresh is None or fresh.status not in ("preview", "done"):
        return None
    try:
        from portal.agent import changeset as cs_mod2
        existing = cs_mod2.AgentChangeset.objects.filter(
            run=fresh, state="preview").first()
        if existing is not None:
            return existing
        key = f"run-{fresh.pk}-{fresh.payload_hash[:12]}"
        cs, _token = cs_mod2.prepare_changeset(
            actor, farm, commands, run=fresh, idempotency_key=key)
        return cs
    except Exception:
        logger.exception("agent auto-prepare failed for run %s",
                         getattr(run, "pk", "?"))
        return None


def run_worker_once(client=None, transport=None, now=None,
                    lease_seconds=None, lock_file=None):
    """Reap stale, claim oldest, execute one run. Returns a short status."""
    from portal.management.commands.run_advice_worker import (
        worker_singleton_lock)

    at = now or timezone.now()
    with worker_singleton_lock(Path(lock_file) if lock_file
                               else _lock_path()):
        reaped = reap_stale(now=at)
        run = claim_next(now=at, lease_seconds=lease_seconds)
        if run is None:
            return f"idle reaped={reaped}"
        from portal.agent import run as run_mod
        from portal.models import Farm
        try:
            farm = Farm.objects.get(pk=run.farm_id)
        except Exception:
            return f"idle reaped={reaped}"
        from django.contrib.auth import get_user_model
        User = get_user_model()
        actor = User.objects.filter(pk=run.initiated_by_id).first()
        if actor is None:
            return f"idle reaped={reaped}"
        snap = run.input_snapshot or {}
        selected_doc_ids = list(snap.get("selected_doc_ids") or [])
        limits = run.budgets or None
        if client is None:
            from portal.agent.client import AgentClient
            client = AgentClient(transport=transport)
        try:
            run_mod._advance(run, farm, actor, run_mod.budgets.get_limits(
                limits), client, transport, selected_doc_ids)
        except Exception:
            logger.exception("agent worker run %s failed", run.pk)
        try:
            fresh_farm = Farm.objects.get(pk=run.farm_id)
            fresh_actor = User.objects.filter(
                pk=run.initiated_by_id).first()
            if fresh_actor is not None:
                _auto_prepare_receipt(run, fresh_farm, fresh_actor)
        except Exception:
            pass
        return f"processed run={run.pk} reaped={reaped}"
