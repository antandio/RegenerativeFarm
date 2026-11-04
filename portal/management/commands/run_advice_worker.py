"""Durable advice worker command: finalize stale, claim oldest, execute.

English comments per project convention. Singleton via an flock on a
private, configurable lock file; stop state is per-invocation and the
previous signal handlers are always restored (no module-global STOP).
"""
import fcntl
import os
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _default_lock_path():
    configured = getattr(settings, "ADVICE_WORKER_LOCK_FILE", "") or ""
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "advice-worker.lock"


@contextmanager
def worker_singleton_lock(lock_path):
    """Hold an exclusive non-blocking flock; raise CommandError if held."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError(
                "run_advice_worker: another worker instance is already "
                f"running (lock file: {path})"
            )
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


class Command(BaseCommand):
    help = "Run the durable advice worker (DB claim queue, one run at a time)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Finalize stale, execute at most one run, then exit.",
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=5.0,
            help="Idle sleep between loop iterations.",
        )
        parser.add_argument(
            "--lock-file",
            default="",
            help="Singleton lock file (default: ADVICE_WORKER_LOCK_FILE "
            "setting or BASE_DIR/advice-worker.lock). Private (0600).",
        )

    def handle(self, *args, **options):
        from portal.advice_worker import run_worker_once

        lock_path = options.get("lock_file") or _default_lock_path()
        # Per-invocation stop state: a fresh event for every handle() call,
        # so a stop (or test) in one invocation never poisons the next.
        stop_event = threading.Event()

        def _handle_stop(signum, frame):
            stop_event.set()

        previous = {}
        in_main_thread = threading.current_thread() is threading.main_thread()
        if in_main_thread:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, _handle_stop)
        try:
            with worker_singleton_lock(lock_path):
                if options.get("once"):
                    self.stdout.write(run_worker_once())
                    return
                poll = max(float(options.get("poll_seconds") or 5.0), 0.5)
                while not stop_event.is_set():
                    run_worker_once()
                    if stop_event.is_set():
                        break
                    # Interruptible idle wait; wakes immediately on stop.
                    stop_event.wait(poll)
        finally:
            if in_main_thread:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
