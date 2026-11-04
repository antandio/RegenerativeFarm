"""Async advice worker: exactly one active job per gunicorn process.

English comments per project convention. No secrets are logged here.
No new model fields; the existing dispatch_authorized_at acts as the
atomic exactly-once dispatch claim (authorize_dispatch returns False
on re-authorization, so only one worker wins the claim).
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# One active job per process, no queue: nonblocking acquire, saturation
# fails the new run safely instead of leaving it pending forever.
_slot = threading.Semaphore(1)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advice")


def _fail_safe(run_id, code="provider_error"):
    """Mark a run terminal without provider output or secret leakage."""
    try:
        from django.utils import timezone
        from .services import SAFE_MESSAGES
        from .models import AdviceRun
        msg = str(SAFE_MESSAGES.get(code, ""))
        AdviceRun.objects.filter(pk=run_id, status="pending").update(
            status="failed", error_code=code,
            error_message_safe=msg or str(SAFE_MESSAGES["provider_error"]),
            completed_at=timezone.now(),
        )
    except Exception:
        logger.error("advice worker safe-fail failed")


def _run_advice_job(run_id):
    """Worker body: independent DB connection, run language, claim, call."""
    from django.db import close_old_connections
    from django.utils import translation
    close_old_connections()
    try:
        from .models import AdviceRun
        from .services import execute_reserved_run
        try:
            run = AdviceRun.objects.get(pk=run_id)
        except AdviceRun.DoesNotExist:
            return
        # Run language override: provider output follows the run, not the
        # worker thread locale. Deadline/staleness is derived only from
        # ADVICE_PENDING_STALE_MINUTES, which must exceed the maximum
        # configured provider timeout plus buffer, so a live job is never
        # falsely reported as interrupted. No automatic replays here.
        with translation.override(run.language or "hu"):
            execute_reserved_run(run)
    except Exception:
        logger.error("advice worker crashed")
        _fail_safe(run_id)
    finally:
        try:
            close_old_connections()
        finally:
            _slot.release()


def schedule_advice_run(run_id):
    """Schedule background execution; call only via transaction.on_commit.

    Returns True when the job was accepted, False when saturated (the run
    is then failed safely so it never stays pending forever).
    """
    if not _slot.acquire(blocking=False):
        _fail_safe(run_id)
        return False
    try:
        _executor.submit(_run_advice_job, run_id)
    except Exception:
        _slot.release()
        _fail_safe(run_id)
        return False
    return True
