"""GET-only utilization monitor over persisted WorkItems.

No writes, no ephemeral generation, no network: weather comes only from the
existing cache-only _weather_map + annotate_weather.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from . import utilization as U
from .domain_models import PROPERTY_KIND_CHOICES
from .models import Equipment, EquipmentProperty, FarmProperty, WorkItem
from .permissions import get_active_farm, role_for
from .planning import annotate_weather
from .plans_views import _weather_map

READ_ROLES = ("owner", "manager", "worker", "viewer")
FARM_HOURS_PER_WEEKDAY = Decimal("8")
VALID_KINDS = frozenset(k for k, _label in PROPERTY_KIND_CHOICES)
VALID_RESOURCE_TYPES = ("all", "property", "equipment")


def _int_or_404(raw, farm, model):
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        raise Http404()
    if not model.objects.filter(farm=farm, pk=pk).exists():
        raise Http404()
    return pk


def _week_key(d):
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _week_label(key):
    return "%d-W%02d" % (key[0], key[1])


def _pct_str(left_days, width_days, span):
    # Preformatted dot-decimal strings: template must not render raw floats
    # under HU locale (comma decimals would be invalid CSS).
    return ("%.4f" % (left_days / span * 100.0), "%.4f" % (width_days / span * 100.0))


def _bar_pos(wstart, wend, start, span):
    lo = max(wstart, start)
    hi = min(wend, start + timedelta(days=span - 1))
    left, width = _pct_str((lo - start).days, (hi - lo).days + 1, span)
    return lo, hi, left, width


def _quant1(v):
    return v.quantize(Decimal("0.1"))


def _week_state(assigned_exact, unknown_count, avail, hpd, weekdays):
    """(available_or_None, assigned_q, pct_or_None, state).

    Unknown demand never reports misleading ok: any unknown count forces
    state "unknown" (or "conflict" on an unavailable machine). Percentages
    are computed from exact sums, quantized once at presentation.
    """
    assigned_q = _quant1(assigned_exact)
    if not avail:
        if assigned_exact > 0 or unknown_count > 0:
            return Decimal("0"), assigned_q, None, "conflict"
        return Decimal("0"), assigned_q, None, "unavailable"
    if hpd is None:
        return None, assigned_q, None, "unknown"
    if weekdays == 0:
        if assigned_exact > 0 or unknown_count > 0:
            return Decimal("0"), assigned_q, None, "conflict" if not avail else "unknown"
        return Decimal("0"), assigned_q, None, "free"
    available = _quant1(hpd * weekdays)
    if unknown_count > 0:
        # Unknown demand must never show a numeric percent (consistent
        # with the farm summary, which forces pct None on unknown).
        return available, assigned_q, None, "unknown"
    if available == 0:
        return available, assigned_q, None, "free"
    pct = _quant1(assigned_exact / available * 100)
    return available, assigned_q, pct, ("overload" if pct > 100 else "ok")


@login_required
@require_http_methods(["GET"])
def utilization_view(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    if role_for(request.user, farm) not in READ_ROLES:
        raise Http404()
    today = date.today()
    params = request.GET
    start, end = U.resolve_window(farm, params, today)
    span = (end - start).days + 1

    prop_id = equip_id = None
    if params.get("property"):
        prop_id = _int_or_404(params.get("property"), farm, FarmProperty)
    if params.get("equipment"):
        equip_id = _int_or_404(params.get("equipment"), farm, Equipment)
    raw_kind = (params.get("kind") or "").strip()
    kind = raw_kind if raw_kind in VALID_KINDS else None
    raw_rt = (params.get("resource_type") or "").strip()
    resource_type = raw_rt if raw_rt in VALID_RESOURCE_TYPES else "all"
    filters_active = bool(prop_id or equip_id or kind or resource_type != "all")

    from django.db.models import Q as _Q
    # Hidden-linked rows are preserved in DB, just not shown.
    items = list(WorkItem.objects.filter(farm=farm).filter(
        _Q(crop_cycle__isnull=True) | _Q(crop_cycle__is_hidden=False)).select_related(
        "equipment", "property", "crop_cycle").order_by("start_date", "id"))

    def overlaps(w):
        return w.start_date <= end and w.end_date >= start

    win_items = [w for w in items if overlaps(w)]
    # Farm totals: every overlapping task counted exactly once (unfiltered).
    farm_total_hours = Decimal("0")
    farm_allocated_exact = Decimal("0")
    farm_unknown = 0
    for w in win_items:
        h = U.parse_hours(w.estimated_hours)
        if h is not None:
            farm_total_hours += h
        share = U.task_share(w.start_date, w.end_date, w.estimated_hours, start, end)
        if share["unknown"]:
            farm_unknown += 1
        else:
            farm_allocated_exact += share["allocated"]
    farm_allocated = _quant1(farm_allocated_exact)

    # Equipment link map for eligibility checks.
    links = {}
    for l in EquipmentProperty.objects.filter(equipment__farm=farm).values(
            "equipment_id", "property_id"):
        links.setdefault(l["equipment_id"], []).append(l["property_id"])

    prop_kind = {p.id: p.kind for p in FarmProperty.objects.filter(farm=farm)}

    def task_visible(w):
        if prop_id is not None and w.property_id != prop_id:
            return False
        if equip_id is not None and w.equipment_id != equip_id:
            return False
        if kind is not None and prop_kind.get(w.property_id) != kind:
            return False
        return True

    vis_items = [w for w in win_items if task_visible(w)]

    wmap = _weather_map(farm)

    def make_bar(w):
        share = U.task_share(w.start_date, w.end_date, w.estimated_hours, start, end)
        probs = U.assignment_problems(w, w.equipment, links.get(w.equipment_id, [])) \
            if w.equipment_id else []
        lo, hi, left, width = _bar_pos(w.start_date, w.end_date, start, span)
        total_h = U.parse_hours(w.estimated_hours)
        if share["unknown"]:
            alloc_q = None
        else:
            alloc_q = _quant1(share["allocated"])
        # Data-table status: problems + missing/no-equipment/recommendation
        # even when no assignment errors.
        table_bits = list(probs)
        if w.missing:
            table_bits.append(str(_("missing machine")))
            if w.recommendation:
                table_bits.append(str(w.recommendation))
        elif w.equipment_id is None:
            table_bits.append(str(_("no equipment assigned")))
        if share["unknown"]:
            table_bits.append(str(_("unknown")))
        if not table_bits:
            table_bits.append(str(_("ok")))
        return {"task": w, "share": share, "problems": probs,
                "left": left, "width": width,
                "alloc": alloc_q,
                "total_h": total_h,
                "table_state": "; ".join(table_bits)}

    show_props = resource_type in ("all", "property")
    show_equips = resource_type in ("all", "equipment")

    properties = list(FarmProperty.objects.filter(farm=farm).order_by("name"))
    if kind:
        properties = [p for p in properties if p.kind == kind]
    if prop_id:
        properties = [p for p in properties if p.id == prop_id]

    prop_rows = []
    if show_props:
        for p in properties:
            tasks = [w for w in vis_items if w.property_id == p.id]
            if filters_active and not tasks:
                continue
            bars = [make_bar(w) for w in tasks]
            covered = set()
            for w in tasks:
                lo = max(w.start_date, start)
                hi = min(w.end_date, end)
                d = lo
                while d <= hi:
                    covered.add(d)
                    d += timedelta(days=1)
            notes = annotate_weather(wmap.get(p.id), start.month)
            prop_rows.append({"prop": p, "bars": bars, "covered_days": len(covered),
                              "span": span, "notes": notes,
                              "has_tasks": bool(tasks),
                              "task_ids": sorted(w.id for w in tasks)})

    equipments = list(Equipment.objects.filter(farm=farm).order_by("name"))
    if equip_id:
        equipments = [e for e in equipments if e.id == equip_id]

    # Day grid: one cell per day with exact offset percentages on the same
    # timeline surface width as the bars.
    days = []
    for i in range(span):
        d = start + timedelta(days=i)
        left, width = _pct_str(i, 1, span)
        days.append({"date": d, "left": left, "width": width,
                     "weekday": d.weekday(), "is_weekend": d.weekday() >= 5,
                     "is_monday": d.weekday() == 0, "is_first": i == 0})
    # Week-boundary ticks positioned on the same surface.
    ticks = []
    for i in range(span):
        d = start + timedelta(days=i)
        if d.weekday() == 0 or i == 0:
            left, _w = _pct_str(i, 1, span)
            ticks.append({"date": d, "left": left})

    def build_weeks(task_list, hpd, avail):
        weekly = {}
        d = start
        while d <= end:
            weekly.setdefault(_week_key(d), {"assigned": Decimal("0"),
                                             "weekdays": 0, "unknown": 0})
            if d.weekday() < 5:
                weekly[_week_key(d)]["weekdays"] += 1
            d += timedelta(days=1)
        for w in task_list:
            share = U.task_share(w.start_date, w.end_date, w.estimated_hours, start, end)
            lo = max(w.start_date, start)
            hi = min(w.end_date, end)
            if share["unknown"] or share["per_day"] is None:
                seen = set()
                dd = lo
                while dd <= hi:
                    seen.add(_week_key(dd))
                    dd += timedelta(days=1)
                for k in seen:
                    weekly.setdefault(k, {"assigned": Decimal("0"),
                                          "weekdays": 0, "unknown": 0})
                    weekly[k]["unknown"] += 1
                continue
            dd = lo
            while dd <= hi:
                if dd.weekday() < 5:
                    weekly[_week_key(dd)]["assigned"] += share["per_day"]
                dd += timedelta(days=1)
        rows = []
        for key in sorted(weekly):
            wd = weekly[key]["weekdays"]
            if hpd is None and avail:
                available, assigned, pct, state = None, _quant1(weekly[key]["assigned"]), None, "unknown"
                if weekly[key]["unknown"]:
                    state = "unknown"
                elif weekly[key]["assigned"] == 0:
                    state = "unknown"
            else:
                available, assigned, pct, state = _week_state(
                    weekly[key]["assigned"], weekly[key]["unknown"], avail,
                    hpd if not (hpd is None and avail) else (FARM_HOURS_PER_WEEKDAY if hpd is None else hpd),
                    wd)
                if hpd is None and avail:
                    available, pct = None, None
                    state = "unknown"
            rows.append({"key": key, "label": _week_label(key), "assigned": assigned,
                         "available": available, "pct": pct, "state": state,
                         "unknown": weekly[key]["unknown"], "weekdays": wd})
        return rows

    equip_rows = []
    if show_equips:
        for e in equipments:
            avail, hpd = U.parse_machine_capacity(e)
            tasks = [w for w in vis_items if w.equipment_id == e.id]
            if filters_active and not tasks and (equip_id != e.id):
                # Hide machines with no visible demand when filtering,
                # but keep the explicitly selected machine visible.
                if equip_id is None and (prop_id is not None or kind is not None):
                    continue
            bars = [make_bar(w) for w in tasks]
            weeks = build_weeks(tasks, hpd, avail)
            unknown_tasks = sum(1 for w in tasks
                                if U.task_share(w.start_date, w.end_date,
                                                 w.estimated_hours, start, end)["unknown"])
            equip_rows.append({"equip": e, "available_flag": avail, "hpd": hpd,
                               "tasks": tasks, "bars": bars, "weeks": weeks,
                               "unknown_tasks": unknown_tasks,
                               "task_ids": sorted(w.id for w in tasks)})

    unassigned_all = [w for w in vis_items if w.equipment_id is None]
    unassigned = [{"bar": make_bar(w), "task": w} for w in unassigned_all]
    if filters_active and not unassigned_all and resource_type == "equipment":
        unassigned = []

    # Screenreader table rows: every filtered visible task, independent of
    # resource_type grouping (equipment-only filters must not yield an
    # empty accessible table while bars are shown).
    _prop_names = {p.id: p.name for p in FarmProperty.objects.filter(farm=farm)}
    _equip_names = {e.id: e.name for e in Equipment.objects.filter(farm=farm)}
    table_bars = []
    for w in vis_items:
        _b = make_bar(w)
        table_bars.append({"bar": _b, "task": w,
                           "prop_name": _prop_names.get(w.property_id, ""),
                           "equip_name": _equip_names.get(w.equipment_id, "") if w.equipment_id else ""})

    # Farm weekly summary (unfiltered scope) with unknown tracking.
    farm_weeks = {}
    d = start
    while d <= end:
        farm_weeks.setdefault(_week_key(d), {"assigned": Decimal("0"),
                                             "weekdays": 0, "unknown": 0})
        if d.weekday() < 5:
            farm_weeks[_week_key(d)]["weekdays"] += 1
        d += timedelta(days=1)
    for w in win_items:
        share = U.task_share(w.start_date, w.end_date, w.estimated_hours, start, end)
        lo = max(w.start_date, start)
        hi = min(w.end_date, end)
        if share["unknown"] or share["per_day"] is None:
            seen = set()
            dd = lo
            while dd <= hi:
                seen.add(_week_key(dd))
                dd += timedelta(days=1)
            for k in seen:
                farm_weeks.setdefault(k, {"assigned": Decimal("0"),
                                          "weekdays": 0, "unknown": 0})
                farm_weeks[k]["unknown"] += 1
            continue
        dd = lo
        while dd <= hi:
            if dd.weekday() < 5:
                farm_weeks[_week_key(dd)]["assigned"] += share["per_day"]
            dd += timedelta(days=1)
    farm_week_rows = []
    for key in sorted(farm_weeks):
        wd = farm_weeks[key]["weekdays"]
        unk = farm_weeks[key]["unknown"]
        if wd == 0:
            assigned = _quant1(farm_weeks[key]["assigned"])
            state = "unknown" if (unk or assigned != 0) else "unknown"
            farm_week_rows.append({"label": _week_label(key), "assigned": assigned,
                                   "available": Decimal("0"), "pct": None,
                                   "state": state, "unknown": unk})
            continue
        available = _quant1(FARM_HOURS_PER_WEEKDAY * wd)
        assigned = _quant1(farm_weeks[key]["assigned"])
        if unk:
            farm_week_rows.append({"label": _week_label(key), "assigned": assigned,
                                   "available": available, "pct": None,
                                   "state": "unknown", "unknown": unk})
        elif available == 0:
            farm_week_rows.append({"label": _week_label(key), "assigned": assigned,
                                   "available": available, "pct": None,
                                   "state": "unknown", "unknown": unk})
        else:
            pct = _quant1(farm_weeks[key]["assigned"] / available * 100)
            farm_week_rows.append({"label": _week_label(key), "assigned": assigned,
                                   "available": available, "pct": pct,
                                   "state": "overload" if pct > 100 else "ok",
                                   "unknown": unk})

    prev_start = U.safe_add_days(start, -span)
    next_start = U.safe_add_days(start, span)
    # Clamp nav targets into the valid param range so year-9999 windows
    # never produce overflowing links.
    if prev_start < U.MIN_DATE:
        prev_start = U.MIN_DATE
    if next_start > U.MIN_DATE and (U.safe_add_days(next_start, span - 1) > U.MAX_DATE):
        next_start = U.safe_add_days(U.MAX_DATE, -(span - 1))

    def nav_url(base):
        q = dict(params.items())
        q["start"] = base.isoformat()
        q["end"] = U.safe_add_days(base, span - 1).isoformat()
        return "?" + "&".join("%s=%s" % kv for kv in sorted(q.items()))

    kind_choices = [(k, str(lbl)) for k, lbl in PROPERTY_KIND_CHOICES]
    try:
        from .plan_summary import summarize as _summarize
        _weeks, _ps = _summarize(list(items), list(Equipment.objects.filter(farm=farm)))
        _overload_options = _ps.get("overload_options", [])
    except Exception:
        _overload_options = []

    ctx = {"farm": farm, "start": start, "end": end, "span": span,
           "prop_rows": prop_rows, "equip_rows": equip_rows, "unassigned": unassigned,
           "table_bars": table_bars,
           "farm_total_hours": farm_total_hours, "farm_allocated": farm_allocated,
           "farm_unknown": farm_unknown, "farm_week_rows": farm_week_rows,
           "farm_task_count": len(win_items),
           "prev_url": nav_url(prev_start), "next_url": nav_url(next_start),
           "days": days, "ticks": ticks,
           "prop_id": prop_id, "equip_id": equip_id, "kind": kind,
           "resource_type": resource_type,
           "kind_choices": kind_choices,
           "all_properties": list(FarmProperty.objects.filter(farm=farm).order_by("name")),
           "all_equipment": list(Equipment.objects.filter(farm=farm).order_by("name")),
           "has_any_tasks": bool(items),
           "overload_options": _overload_options}
    try:
        from .permissions import accessible_farms
        ctx.update({"farms": accessible_farms(request.user), "active_farm": farm})
    except Exception:
        pass
    return render(request, "portal/utilization.html", ctx)
