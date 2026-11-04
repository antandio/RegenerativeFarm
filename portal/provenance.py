"""StageC historical provenance: persisted-only snapshots, no inference.

Every value rendered by the provenance panel comes from the persisted
attempt row (AdviceRun / ChatAttempt) and its linked snapshot row.
Current profile, current consent and live quota are NEVER shown as
history. Anything not persisted renders as "not recorded".
No cost figure is ever invented: token counts come from the persisted
usage_* fields; money cost is always reported as unknown.
"""

from django.utils.translation import gettext as _


def advice_provenance(run):
    """Build a persisted-only provenance dict for an AdviceRun."""
    snap = getattr(run, "profile_snapshot", None)
    assumptions = run.result_assumptions or None
    if assumptions is not None and not list(assumptions):
        assumptions = None
    return {
        "kind": "advice",
        "prospective": False,
        "source": run.source or None,
        "requested_model": run.requested_model,
        "actual_model": run.actual_model,
        "attempt_time": run.attempt_started_at,
        "snapshot_version": getattr(snap, "profile_version", None),
        "snapshot_time": getattr(snap, "created_at", None),
        "uncertainty": assumptions,
        "usage_in": run.usage_input_tokens,
        "usage_out": run.usage_output_tokens,
        "dispatch_at": run.dispatch_authorized_at,
        "dispatch_consent_id": run.dispatch_consent_event_id,
        "human_confirmed": bool(run.dispatch_authorized_at is not None
                                and run.dispatch_consent_event_id is not None),
    }


def chat_provenance(attempt):
    """Build a persisted-only provenance dict for a ChatAttempt.

    Chat attempts persist no profile snapshot and no assumptions list,
    so those points honestly render as not recorded.
    """
    return {
        "kind": "chat",
        "prospective": False,
        "source": attempt.source or None,
        "requested_model": attempt.requested_model,
        "actual_model": attempt.actual_model,
        "attempt_time": attempt.attempt_started_at,
        "snapshot_version": None,
        "snapshot_time": None,
        "uncertainty": None,
        "usage_in": attempt.usage_input_tokens,
        "usage_out": attempt.usage_output_tokens,
        "dispatch_at": attempt.dispatch_authorized_at,
        "dispatch_consent_id": attempt.dispatch_consent_event_id,
        "human_confirmed": bool(attempt.dispatch_authorized_at is not None
                                and attempt.dispatch_consent_event_id is not None),
    }


def prospective_advice_provenance(farm, now):
    """What a request sent NOW would record. Explicitly not history."""
    return {
        "kind": "advice",
        "prospective": True,
        "source": "live",
        "snapshot_version": farm.profile_version,
        "snapshot_time": now,
        "uncertainty": None,
        "usage_in": None,
        "usage_out": None,
        "dispatch_at": None,
        "dispatch_consent_id": None,
        "human_confirmed": False,
    }


def not_recorded_label():
    return _("nincs rögzítve")
