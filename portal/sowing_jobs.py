"""Async sowing worker: exactly one active job per process.

English comments per project convention. No secrets are logged here.
Mirrors the advice worker pattern: one slot, one thread, safe terminal
failures, and provider calls outside any held slot only via the job body.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_slot = threading.Semaphore(1)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sowing")


def _clear_queued(comparison_id):
    """Release a scheduling reservation, leaving the row recoverable."""
    try:
        from .models import SowingComparison
        SowingComparison.objects.filter(
            pk=comparison_id, status="pending",
            queued_at__isnull=False).update(queued_at=None)
    except Exception:
        logger.error("sowing worker queue release failed")


def _fail_safe(comparison_id, code="provider_error", attempt_token=None):
    """Fail only this worker's own current attempt, atomically.

    Bound to an attempt: an explicit token must still be the comparison's
    current pending attempt, and without a token only a current pending
    attempt owned by the calling worker may fail the comparison. A merely
    queued or undispatched comparison is never failed here; its
    reservation is released so an explicit retry can recover it.
    """
    try:
        from django.db import transaction
        from django.utils import timezone
        from .models import SowingComparison, SowingComparisonAttempt
        from .services import SAFE_MESSAGES
        msg = str(SAFE_MESSAGES.get(code, ""))
        if not msg:
            msg = str(SAFE_MESSAGES.get("provider_error", ""))
        now = timezone.now()
        with transaction.atomic():
            try:
                row = SowingComparison.objects.select_for_update().get(
                    pk=comparison_id)
            except SowingComparison.DoesNotExist:
                return
            except Exception:
                return
            if row.status != "pending":
                return
            att = None
            if attempt_token is not None:
                try:
                    att = SowingComparisonAttempt.objects.get(
                        attempt_token=attempt_token)
                except SowingComparisonAttempt.DoesNotExist:
                    att = None
                except Exception:
                    att = None
                # Not this worker's actual current attempt: touch nothing.
                if (att is None or att.comparison_id != row.pk
                        or row.current_attempt_id != att.pk
                        or att.status != "pending"):
                    return
            elif row.current_attempt_id is not None:
                try:
                    att = SowingComparisonAttempt.objects.get(
                        pk=row.current_attempt_id)
                except SowingComparisonAttempt.DoesNotExist:
                    att = None
                except Exception:
                    att = None
                if att is None or att.status != "pending":
                    # Queued but never claimed: recoverable, not failed.
                    if row.queued_at is not None:
                        SowingComparison.objects.filter(
                            pk=row.pk).update(queued_at=None)
                    return
            else:
                # No attempt was ever claimed: recoverable, not failed.
                if row.queued_at is not None:
                    SowingComparison.objects.filter(
                        pk=row.pk).update(queued_at=None)
                return
            SowingComparisonAttempt.objects.filter(
                pk=att.pk, status="pending").update(
                status="failed", error_code=code, error_message_safe=msg,
                completed_at=now)
            SowingComparison.objects.filter(
                pk=row.pk, status="pending",
                current_attempt_id=att.pk).update(
                status="failed", review_state="pending_review", decision="",
                error_code=code, error_message_safe=msg, completed_at=now,
                queued_at=None)
    except Exception:
        logger.error("sowing worker safe-fail failed")


def _handle_saturation(comparison_id):
    """Slot-busy handling: never kill a live attempt or a queued retry.

    A comparison with a live pending attempt keeps running untouched. A
    queued retry reservation is released so the row stays pending and a
    later explicit retry can dispatch it. Only a comparison that was
    never dispatched and never queued keeps the historical fail-safe.
    """
    try:
        from django.db import transaction
        from django.utils import timezone
        from .models import SowingComparison, SowingComparisonAttempt
        from .services import SAFE_MESSAGES
        with transaction.atomic():
            try:
                row = SowingComparison.objects.select_for_update().get(
                    pk=comparison_id)
            except SowingComparison.DoesNotExist:
                return
            except Exception:
                return
            if row.status != "pending":
                return
            if row.current_attempt_id is not None:
                try:
                    cur = SowingComparisonAttempt.objects.get(
                        pk=row.current_attempt_id)
                except SowingComparisonAttempt.DoesNotExist:
                    cur = None
                except Exception:
                    cur = None
                if cur is not None and cur.status == "pending":
                    return
            if (row.current_attempt_id is not None
                    or row.dispatch_authorized_at is not None
                    or row.queued_at is not None):
                SowingComparison.objects.filter(pk=row.pk).update(
                    queued_at=None)
                return
            msg = str(SAFE_MESSAGES.get("provider_error", ""))
            SowingComparison.objects.filter(
                pk=row.pk, status="pending").update(
                status="failed", review_state="pending_review", decision="",
                error_code="provider_error", error_message_safe=msg,
                completed_at=timezone.now(), queued_at=None)
    except Exception:
        logger.error("sowing worker saturation handling failed")


def _run_sowing_job(comparison_id):
    """Worker body: independent DB connection, language, claim, call."""
    from django.db import close_old_connections
    from django.utils import translation
    close_old_connections()
    try:
        from .crop_sowing import execute_sowing_comparison
        from .models import SowingComparison
        try:
            row = SowingComparison.objects.get(pk=comparison_id)
        except SowingComparison.DoesNotExist:
            return
        with translation.override(row.language or "hu"):
            execute_sowing_comparison(row.pk)
    except Exception:
        logger.error("sowing worker crashed")
        _fail_safe(comparison_id)
    finally:
        try:
            close_old_connections()
        finally:
            _slot.release()


def schedule_sowing_comparison(comparison_id):
    """Schedule background execution; call only via transaction.on_commit.

    Returns True when the job was accepted. On saturation the queued
    reservation is released and False is returned; the comparison stays
    pending and recoverable instead of being failed or reported queued.
    """
    if not _slot.acquire(blocking=False):
        _handle_saturation(comparison_id)
        return False
    try:
        _executor.submit(_run_sowing_job, comparison_id)
    except Exception:
        _slot.release()
        _clear_queued(comparison_id)
        return False
    return True
