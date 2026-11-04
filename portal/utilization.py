"""Utilization helpers: pure date/hour math for the GET-only monitor.

No DB writes, no network. Weekday counts use an O(1) full-weeks formula so
huge original task windows never iterate day by day.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

MIN_YEAR = 1990
MAX_YEAR = 2100
MAX_SPAN_DAYS = 90
DEFAULT_SPAN_DAYS = 30
MIN_DATE = date(MIN_YEAR, 1, 1)
MAX_DATE = date(MAX_YEAR, 12, 31)


def safe_add_days(d, n):
    """Add days, clamping to [0001-01-01, 9999-12-31] instead of OverflowError."""
    try:
        return d + timedelta(days=n)
    except OverflowError:
        return date.max if n > 0 else date.min


def weekdays_between(a, b):
    """Inclusive Mon-Fri count in [a, b]; 0 for reversed/empty. O(1)."""
    if a is None or b is None or b < a:
        return 0
    total = (b - a).days + 1
    full, rem = divmod(total, 7)
    n = full * 5
    base = a + timedelta(days=full * 7)
    for i in range(rem):
        if (base + timedelta(days=i)).weekday() < 5:
            n += 1
    return n


def parse_hours(value):
    """Valid estimated hours as Decimal, else None (unknown, never zero)."""
    try:
        h = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None
    try:
        if not h.is_finite():
            return None
    except AttributeError:
        return None
    if h <= 0:
        return None
    return h


def parse_machine_capacity(equipment):
    """(available_flag, hours_per_day Decimal|None). Invalid/missing -> unknown.

    A missing ``hours_per_day`` attribute is unknown (None), never an
    invented default: callers must render "?" rather than assume 8h.
    """
    avail = bool(getattr(equipment, "available", True))
    if not hasattr(equipment, "hours_per_day"):
        return avail, None
    raw = getattr(equipment, "hours_per_day")
    if raw is None:
        return avail, None
    try:
        h = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return avail, None
    try:
        if not h.is_finite():
            return avail, None
    except AttributeError:
        return avail, None
    if h <= 0 or h > 24:
        return avail, None
    return avail, h


def _parse_iso(s):
    try:
        d = date.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None
    if d.year < MIN_YEAR or d.year > MAX_YEAR:
        return None
    return d


def default_window(farm, today):
    from django.db.models import Q as _Q
    from .models import WorkItem
    # Window follows visible items only; hidden-linked rows stay in DB.
    qs = WorkItem.objects.filter(farm=farm).filter(
        _Q(crop_cycle__isnull=True) | _Q(crop_cycle__is_hidden=False)).order_by("start_date")
    first = qs.first()
    if first is None or first.start_date is None:
        return today, safe_add_days(today, DEFAULT_SPAN_DAYS - 1)
    last = qs.order_by("-end_date").first()
    end = last.end_date if last and last.end_date else first.start_date
    cap = safe_add_days(first.start_date, MAX_SPAN_DAYS - 1)
    if end > cap:
        end = cap
    return first.start_date, end


def resolve_window(farm, params, today):
    """Return inclusive (start, end) local calendar dates, capped at 90 days."""
    dflt = default_window(farm, today)
    s = _parse_iso(params.get("start")) if params.get("start") else None
    e = _parse_iso(params.get("end")) if params.get("end") else None
    if s is None and e is None:
        return dflt
    if s is None:
        s = dflt[0]
    if e is None:
        e = dflt[1]
    if s > e:
        s, e = e, s
    if (e - s).days + 1 > MAX_SPAN_DAYS:
        e = safe_add_days(s, MAX_SPAN_DAYS - 1)
    return s, e


def task_share(task_start, task_end, hours, win_start, win_end):
    """Uniform weekday allocation of H over the ENTIRE original window.

    Returns dict(total_weekdays, per_day, overlap_weekdays, allocated,
    weekend_only, unknown). unknown True when demand cannot be quantified.
    Dates stay inclusive; weekend-only bars still render.

    ``allocated`` and ``per_day`` are EXACT Decimals (no per-task rounding);
    callers quantize once at presentation so fractional shares aggregate
    without precision loss.
    """
    total_wd = weekdays_between(task_start, task_end)
    h = parse_hours(hours)
    if h is None or total_wd == 0:
        weekend_only = total_wd == 0 and h is not None and task_start and task_end
        return {"total_weekdays": total_wd, "per_day": None, "overlap_weekdays": 0,
                "allocated": None, "weekend_only": bool(weekend_only), "unknown": True}
    per_day = h / total_wd
    lo = max(task_start, win_start)
    hi = min(task_end, win_end)
    if hi < lo:
        return {"total_weekdays": total_wd, "per_day": per_day, "overlap_weekdays": 0,
                "allocated": Decimal("0"), "weekend_only": False, "unknown": False}
    ov = weekdays_between(lo, hi)
    return {"total_weekdays": total_wd, "per_day": per_day, "overlap_weekdays": ov,
            "allocated": per_day * ov,
            "weekend_only": False, "unknown": False}


def assignment_problems(task, equipment, linked_property_ids):
    """Eligibility warnings for an assigned machine; [] means valid."""
    from django.utils.translation import gettext as _
    problems = []
    if equipment is None:
        return problems
    if not getattr(equipment, "available", True):
        problems.append(str(_("unavailable machine")))
    caps = list(getattr(equipment, "capabilities", None) or [])
    if task.capability not in caps:
        problems.append(str(_("capability mismatch")))
    if task.property_id not in (linked_property_ids or []):
        problems.append(str(_("not assigned to this property")))
    return problems
