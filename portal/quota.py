"""Shared quota: AdviceRun + ChatAttempt + dispatched sowing count TOGETHER.

Every counted row spends from the same per-farm hour/day and global day
budgets. Sowing usage counts immutable attempt rows by dispatch time: every
dispatched SowingComparisonAttempt spends current budget, so a months-old
pending row retried now spends now and old failed retries are never erased.
Count failures propagate to the caller, which must block dispatch (fail
closed) instead of bypassing the budget.
"""
from datetime import timedelta
from django.conf import settings
from django.utils import timezone


class QuotaExceeded(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _sowing_counts(farm_id, hour_cut, day_cut):
    # No error swallowing here: a count failure must block dispatch, never
    # silently undercount and bypass the shared budget. Callers treat any
    # unexpected quota error as a dispatch refusal.
    from .models import SowingComparisonAttempt
    sh = SowingComparisonAttempt.objects.filter(
        farm_id=farm_id, dispatched_at__gte=hour_cut).count()
    sd = SowingComparisonAttempt.objects.filter(
        farm_id=farm_id, dispatched_at__gte=day_cut).count()
    return sh, sd


def _counts(farm_id, now):
    # Lazy imports: quota is imported by services/chat at call time.
    from .models import AdviceRun
    from .chat_models import ChatAttempt
    hour_cut = now - timedelta(hours=1)
    day_cut = now - timedelta(hours=24)
    ah = AdviceRun.objects.filter(farm_id=farm_id, attempt_started_at__gte=hour_cut).count()
    ch = ChatAttempt.objects.filter(farm_id=farm_id, attempt_started_at__gte=hour_cut).count()
    ad = AdviceRun.objects.filter(farm_id=farm_id, attempt_started_at__gte=day_cut).count()
    cd = ChatAttempt.objects.filter(farm_id=farm_id, attempt_started_at__gte=day_cut).count()
    sh, sd = _sowing_counts(farm_id, hour_cut, day_cut)
    return ah + ch + sh, ad + cd + sd


def check_combined_quotas(farm):
    """Raise QuotaExceeded('quota_exceeded'|'global_quota_exceeded') when over budget."""
    from .models import AdviceRun
    from .chat_models import ChatAttempt
    now = timezone.now()
    hour_count, day_count = _counts(farm.pk, now)
    if hour_count >= settings.ADVICE_MAX_PER_HOUR_PER_FARM or day_count >= settings.ADVICE_MAX_PER_DAY_PER_FARM:
        raise QuotaExceeded("quota_exceeded")
    day_cut = now - timedelta(hours=24)
    from .models import SowingComparisonAttempt
    sowing_glob = SowingComparisonAttempt.objects.filter(
        dispatched_at__gte=day_cut).count()
    glob = (AdviceRun.objects.filter(attempt_started_at__gte=day_cut).count()
            + ChatAttempt.objects.filter(attempt_started_at__gte=day_cut).count()
            + sowing_glob)
    if glob >= settings.ADVICE_GLOBAL_MAX_PER_DAY:
        raise QuotaExceeded("global_quota_exceeded")
    return hour_count, day_count


def usage_counts(farm):
    now = timezone.now()
    return _counts(farm.pk, now)
