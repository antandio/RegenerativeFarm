"""Stage 3 plans views: authenticated active-farm GET list/timeline, POST generate."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from .models import CropCycle, Equipment, EquipmentProperty, Farm, FarmProperty, Membership, WorkItem
from .permissions import accessible_farms, get_active_farm, role_for
from .planning import annotate_weather, generate_plan
from . import weather as W

WRITE_ROLES = ("owner", "manager")


def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm


def _ctx(request, farm):
    try:
        farms = accessible_farms(request.user)
    except Exception:
        farms = [farm]
    return {"farm": farm, "farms": farms, "active_farm": farm}


def _cycle_dicts(farm):
    out = []
    # Exclude soft-hidden plans; rows stay in DB for history/export.
    for c in CropCycle.objects.filter(farm=farm, is_hidden=False).order_by("id"):
        out.append({"id": c.id, "property_id": c.property_id, "crop_name": c.crop_name,
                    "state": c.state, "growth_stage": c.growth_stage or "",
                    "sow_date": c.sow_date, "expected_end_date": c.expected_end_date,
                    "observations": c.observations or "",
                    "area_hectares": str(c.property.area_hectares) if c.property_id else "1"})
    return out


def _weather_map(farm):
    """Cache-only weather entries keyed by property id (no network, no writes)."""
    wmap = {}
    props = FarmProperty.objects.filter(farm=farm).select_related("location")
    for p in props:
        loc = p.location
        if loc is None:
            continue
        fc, fs = W.get_forecast_cached(farm.id, loc)
        cl, cs = W.get_climate_cached(farm.id, loc)
        wmap[p.id] = {"forecast": fc, "fstatus": fs,
                      "climate": cl, "cstatus": cs}
    return wmap


def _equipment_dicts(farm):
    links = {}
    for l in EquipmentProperty.objects.filter(equipment__farm=farm).values("equipment_id", "property_id"):
        links.setdefault(l["equipment_id"], []).append(l["property_id"])
    out = []
    for e in Equipment.objects.filter(farm=farm).order_by("id"):
        out.append({"id": e.id, "capabilities": list(e.capabilities or []),
                    "available": bool(e.available), "property_ids": links.get(e.id, []),
                    "hours_per_day": str(e.hours_per_day)})
    return out


@login_required
@require_http_methods(["GET", "POST"])
def plans_view(request):
    farm = _farm_or_404(request)
    if request.method == "POST":
        # Fresh permission check inside atomic block.
        with transaction.atomic():
            fresh = Farm.objects.select_for_update().get(pk=farm.pk)
            role = role_for(request.user, fresh)
            if role not in WRITE_ROLES:
                raise PermissionDenied()
            from datetime import date as _date
            items, _summary = generate_plan(
                _cycle_dicts(fresh), _equipment_dicts(fresh),
                weather=_weather_map(fresh), as_of=_date.today())
            # Manual livestock items are user data: regenerate only crop items.
            # Hidden-cycle historical rows are preserved, not deleted.
            from django.db.models import Q as _Qdel
            WorkItem.objects.filter(
                farm=fresh, livestock_group__isnull=True).filter(
                _Qdel(crop_cycle__isnull=True)
                | _Qdel(crop_cycle__is_hidden=False)).delete()
            eq_map = {e.id: e for e in Equipment.objects.filter(farm=fresh)}
            cyc_map = {c.id: c for c in CropCycle.objects.filter(farm=fresh, is_hidden=False)}
            for d in items:
                cyc = cyc_map.get(d["crop_cycle_id"])
                if cyc is None:
                    continue
                WorkItem.objects.create(
                    farm=fresh, crop_cycle=cyc, property=cyc.property, title=d["title"][:120],
                    capability=d["capability"], start_date=d["start_date"], end_date=d["end_date"],
                    estimated_hours=d["estimated_hours"], equipment=eq_map.get(d["equipment_id"]),
                    missing=d["missing"], recommendation=d["recommendation"][:240],
                    guidance=d["guidance"][:240])
        from .models import AuditEvent
        try:
            AuditEvent.objects.create(farm=fresh, actor=request.user, action="plan.generate",
                                      object_type="plan", object_id="",
                                      meta={"items": len(items),
                                            "over_capacity_weeks": [str(w) for w in _summary.get("over_capacity_weeks", [])],
                                            "over_equipment_weeks": [str(w) for w in _summary.get("over_equipment_weeks", [])]})
        except Exception:
            pass
        messages.success(request, _("Terv elkészült."))
        return redirect("plans")
    # GET: no writes. Persisted summary from saved WorkItems (no fresh generate_plan).
    # Hidden-linked rows are preserved in DB, just not shown.
    from django.db.models import Q as _Q
    items = WorkItem.objects.filter(farm=farm).filter(
        _Q(crop_cycle__isnull=True) | _Q(crop_cycle__is_hidden=False)).select_related(
        "equipment", "crop_cycle").order_by("start_date", "id")
    wmap = _weather_map(farm)
    from .planning import _weather_evidence as _w_ev
    try:
        from .planning import GUIDANCE as _GUID, NO_GUARANTEE as _NOG
        _guid, _nog = str(_GUID), str(_NOG)
    except Exception:
        _guid, _nog = "", ""
    for it in items:
        start_month = it.start_date.month if it.start_date else None
        it.weather_annotations = annotate_weather(wmap.get(it.property_id), start_month)
    from .plan_summary import summarize as _summarize
    weeks, summary = _summarize(list(items), list(Equipment.objects.filter(farm=farm)))
    summary.update({"guidance": _guid, "no_guarantee": _nog,
                    "weather_evidence": _w_ev(wmap)})
    # Binding metadata on the read path too (GET never regenerates plans).
    try:
        from .regenerative_policy import summary_reference as _policy_ref
        summary.update(_policy_ref())
    except Exception:
        pass
    ctx = _ctx(request, farm)
    ctx.update({"items": items, "timeline": sorted(weeks.items()),
                "weather_summary": summary, "weather_source": W.SOURCE,
                "weather_license": W.LICENSE})
    return render(request, "portal/plans.html", ctx)
