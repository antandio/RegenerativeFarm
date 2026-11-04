"""Plan summary helper for the plans GET view (persisted WorkItems only).

No DB writes, no network. Weekly shares come from
``utilization.task_share`` with exact Decimals (no per-task rounding).

- Weekend-only / unknown demand never contributes numeric hours to
  ``week_peaks`` / ``equipment_peaks``, but still renders once (on the
  start week) with ``allocated_hours == 0`` so it can never disappear
  when another item shares the week.
- Equipment is keyed by ID internally; names are display-only
  (``equipment_names`` / ``equipment_rows``), so duplicate names never
  merge capacity. Capacity comes from
  ``utilization.parse_machine_capacity``: unknown capacity (None) is
  shown as "?" and never flags overload — no invented 8h fallback.
- Week iteration steps 7 days at a time via ``utilization.safe_add_days``
  (``date.max`` safe), never day by day.
"""
from copy import copy
from datetime import date
from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from .planning import (CAPACITY_NOTE, EQUIPMENT_CAPACITY_NOTE,
                       WEEK_CAPACITY_HOURS, WORKDAYS_PER_WEEK)
from .utilization import (parse_machine_capacity, safe_add_days, task_share)

OVERLOAD_OPTIONS = (
    _("Túlterhelés esetén a heti feladatok egy része másik hétre is áthelyezhető."),
    _("Túlterhelés esetén a heti kapacitás további erőforrás bevonásával is növelhető."),
)


def monday_of_week(iso_year, iso_week):
    return date.fromisocalendar(iso_year, iso_week, 1)


def iter_week_keys(start, end):
    """(iso_year, iso_week) keys overlapped by [start, end], weekly steps."""
    if start is None:
        return []
    if end is None:
        end = start
    if end < start:
        return []
    s_iso = start.isocalendar()
    e_iso = end.isocalendar()
    cur = date.fromisocalendar(s_iso[0], s_iso[1], 1)
    last = date.fromisocalendar(e_iso[0], e_iso[1], 1)
    out = []
    guard = 0
    while cur <= last:
        iso = cur.isocalendar()
        out.append((iso[0], iso[1]))
        nxt = safe_add_days(cur, 7)
        if nxt <= cur:
            break
        cur = nxt
        guard += 1
        if guard > 5000:
            break
    return out


def _anchor(copy_id, seen_ids, wk):
    if copy_id in seen_ids:
        return "work-%s-%d-%d" % (copy_id, wk[0], wk[1])
    seen_ids.add(copy_id)
    return "work-%s" % copy_id


def summarize(items, equipments):
    """Return (weeks, summary) for persisted work items.

    ``weeks`` maps (year, week) -> [copies with ``allocated_hours``,
    ``unknown`` flag and ``anchor_id``]. ``summary`` keeps the
    ``week_peaks`` / ``equipment_peaks`` keys (equipment keyed by ID)
    plus human-display ``equipment_names`` / ``equipment_rows``,
    ``overload_options`` and ``weekend_unknown_count``.
    """
    caps = {}
    names = {}
    for e in equipments or []:
        names[e.id] = e.name
        try:
            _avail, h = parse_machine_capacity(e)
        except Exception:
            h = None
        caps[e.id] = h
    weeks = {}
    peaks = {}
    eq_peaks = {}
    unknown_count = 0
    seen_ids = set()
    for it in items or []:
        start = getattr(it, "start_date", None)
        if not start:
            continue
        end = getattr(it, "end_date", None) or start
        placed = False
        for wk in iter_week_keys(start, end):
            ws = monday_of_week(wk[0], wk[1])
            we = safe_add_days(ws, 6)
            sh = task_share(start, end, it.estimated_hours, ws, we)
            if sh.get("unknown") or sh.get("allocated") is None:
                continue
            al = sh["allocated"]
            if al == 0:
                continue
            c = copy(it)
            c.allocated_hours = al
            c.unknown = False
            c.anchor_id = _anchor(c.id, seen_ids, wk)
            weeks.setdefault(wk, []).append(c)
            peaks[wk] = peaks.get(wk, Decimal("0")) + al
            if getattr(it, "equipment_id", None):
                key = (wk[0], wk[1], it.equipment_id)
                eq_peaks[key] = eq_peaks.get(key, Decimal("0")) + al
            placed = True
        if not placed:
            wk = (start.isocalendar()[0], start.isocalendar()[1])
            c = copy(it)
            c.allocated_hours = Decimal("0")
            c.unknown = True
            c.anchor_id = _anchor(c.id, seen_ids, wk)
            weeks.setdefault(wk, []).append(c)
            unknown_count += 1
    peaks = dict(sorted(peaks.items()))
    eq_peaks = dict(sorted(eq_peaks.items()))
    over = sorted([w for w, h in peaks.items() if h > WEEK_CAPACITY_HOURS])
    over_eq = sorted([k for k, h in eq_peaks.items()
                      if caps.get(k[2]) is not None
                      and h > caps[k[2]] * WORKDAYS_PER_WEEK])
    over_eq_set = set(over_eq)
    rows = []
    for k, h in eq_peaks.items():
        cap = caps.get(k[2])
        rows.append({
            "week": (k[0], k[1]),
            "equipment_id": k[2],
            "equipment_name": names.get(k[2], "?"),
            "hours": h,
            "weekly_capacity": (cap * WORKDAYS_PER_WEEK
                                if cap is not None else None),
            "overloaded": k in over_eq_set,
        })
    summary = {
        "week_peaks": peaks,
        "over_capacity_weeks": over,
        "week_capacity_hours": WEEK_CAPACITY_HOURS,
        "capacity_note": CAPACITY_NOTE,
        "equipment_peaks": eq_peaks,
        "over_equipment_weeks": over_eq,
        "equipment_capacity_note": EQUIPMENT_CAPACITY_NOTE,
        "equipment_names": names,
        "equipment_rows": rows,
        "overload_options": (list(OVERLOAD_OPTIONS)
                             if (over or over_eq) else []),
        "weekend_unknown_count": unknown_count,
    }
    return weeks, summary
