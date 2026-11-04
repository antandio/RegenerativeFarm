"""Durable agent worker command: reap stale, claim oldest, execute.

English comments per project convention. Singleton via an flock on a
private, configurable lock file.
"""
import signal
import threading
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


def _default_lock_path():
    configured = getattr(settings, "AGENT_WORKER_LOCK_FILE", "") or ""
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "agent-worker.lock"


class Command(BaseCommand):
    help = "Run the durable agent worker (DB claim queue, one run at a time)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Reap stale, execute at most one run, then exit.",
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
            help="Singleton lock file (default: AGENT_WORKER_LOCK_FILE "
            "setting or BASE_DIR/agent-worker.lock). Private (0600).",
        )

    def handle(self, *args, **options):
        from portal.agent.worker import run_worker_once

        lock_path = options.get("lock_file") or _default_lock_path()
        stop_event = threading.Event()

        def _handle_stop(signum, frame):
            stop_event.set()

        previous = {}
        import threading as _th
        in_main_thread = _th.current_thread() is _th.main_thread()
        if in_main_thread:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, _handle_stop)
        try:
            lock_file = options.get("lock_file") or _default_lock_path()
            if options.get("once"):
                self.stdout.write(run_worker_once(lock_file=lock_file))
                return
            poll = max(float(options.get("poll_seconds") or 5.0), 0.5)
            while not stop_event.is_set():
                run_worker_once(lock_file=lock_file)
                if stop_event.is_set():
                    break
                stop_event.wait(poll)
        finally:
            if in_main_thread:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
