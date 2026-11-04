"""Durable advice worker: DB-backed claim queue, no web-process threads.

English comments per project convention. No secrets are logged here.
No new model fields: the existing dispatch_authorized_at is the atomic
exactly-once dispatch claim (authorize_dispatch returns False on
re-authorization, so only one worker wins the claim).

Worker protocol (run_worker_once):
- Reap first, without any provider call: only AUTHORIZED orphans
  (pending, dispatch_authorized_at set, older than the safe dispatch
  deadline) are marked failed and never replayed. Unclaimed
  (unauthorized) runs are left queued unconditionally: they survive a
  restart and are processed when a worker next claims them, with
  consent re-checked at claim time.
- Claim next: the oldest unclaimed pending run is executed via
  execute_reserved_run, which re-authorizes atomically and calls the
  provider OUTSIDE any DB transaction. No freshness filter: even a
  long-queued unclaimed run is claimed and processed.
- Fresh authorized runs (claim in flight) are left alone so a
  concurrent worker never fails a live run.
"""
import logging

from .advice_jobs import _fail_safe

logger = logging.getLogger(__name__)


def build_worker_client():
    """Construct the real provider client from settings (key stays private)."""
    from .provider import MetaProviderClient

    return MetaProviderClient()


def reap_orphans(now=None):
    """Fail stale AUTHORIZED orphans only, no provider call.

    Delegates to the shared services.reap_stale_authorized helper (the
    same implementation the per-farm reservation cleanup uses, so clocks
    and deadlines cannot disagree). Returns the number of runs reaped.
    Unclaimed runs always survive; fresh authorized runs (a live claim)
    are never touched.
    """
    from .services import reap_stale_authorized

    return reap_stale_authorized(now=now)


def claim_next(now=None):
    """Return the oldest unclaimed pending run, or None.

    No freshness filter: a long-queued unclaimed run is still claimed
    and processed after a restart.
    """
    from .models import AdviceRun

    return (
        AdviceRun.objects.filter(
            status="pending",
            dispatch_authorized_at__isnull=True,
        )
        .order_by("attempt_started_at")
        .first()
    )


def run_worker_once(client=None):
    """Reap, then claim and execute at most one run. Returns an outcome.

    Outcome is one of "success", "failed" (claimed run terminal state),
    "reaped" (only stale work was failed), or "idle" (nothing to do).
    Provider crashes fail the claimed run safely without leaking secrets.
    """
    from .services import execute_reserved_run

    reaped = reap_orphans()
    run = claim_next()
    if run is None:
        return "reaped" if reaped else "idle"
    run_id = run.pk
    try:
        done = execute_reserved_run(
            run, client=client if client is not None else build_worker_client()
        )
    except Exception:
        logger.error("advice worker crashed")
        _fail_safe(run_id)
        return "failed"
    if done.status == "success":
        return "success"
    if done.status == "failed":
        return "failed"
    return "reaped"
