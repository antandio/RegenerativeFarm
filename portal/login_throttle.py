"""Shared SQLite login throttle.

All gunicorn workers share the same SQLite file, so counts in the
LoginThrottle table are shared across processes (the old per-process
cache counter was not, and raced between workers). Correctness relies
on short IMMEDIATE transactions (the DB is configured with
transaction_mode=IMMEDIATE, which serializes writers) plus F()
increments, so concurrent failures cannot lose an update. Expired
windows are cleaned up opportunistically on every access. No Redis or
other external dependency is introduced.
"""
import hashlib
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import LoginThrottle

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)


def throttle_key_hash(ip, email):
    raw = f"{ip or '?'}|{(email or '').strip().lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cleanup_expired(now):
    LoginThrottle.objects.filter(expires_at__lte=now).delete()


def is_login_throttled(ip, email):
    key = throttle_key_hash(ip, email)
    now = timezone.now()
    with transaction.atomic():
        _cleanup_expired(now)
        row = LoginThrottle.objects.filter(key_hash=key).first()
        return bool(row and row.fail_count >= MAX_FAILURES)


def record_login_failure(ip, email):
    key = throttle_key_hash(ip, email)
    now = timezone.now()
    with transaction.atomic():
        _cleanup_expired(now)
        row, _created = LoginThrottle.objects.get_or_create(
            key_hash=key,
            defaults={"fail_count": 0, "window_started_at": now, "expires_at": now + WINDOW},
        )
        # F() increment: atomic at the DB level even outside the
        # serializing transaction, as defense in depth.
        LoginThrottle.objects.filter(pk=row.pk).update(
            fail_count=F("fail_count") + 1, updated_at=now
        )


def clear_login_failures(ip, email):
    LoginThrottle.objects.filter(key_hash=throttle_key_hash(ip, email)).delete()
