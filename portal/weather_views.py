"""Tenant weather views: GET cache-only (no DB write, no network); POST refresh."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from .models import FarmLocation
from .permissions import accessible_farms, get_active_farm, role_for
from . import weather as W
from . import weather_icons as WI

WRITE_ROLES = ("owner", "manager")


def _farm(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm


@login_required
@require_http_methods(["GET"])
def weather_view(request):
    farm = _farm(request)
    locs = list(FarmLocation.objects.filter(farm=farm).order_by("name"))
    entries = []
    for loc in locs:
        fc, fs = W.get_forecast_cached(farm.id, loc)
        cl, cs = W.get_climate_cached(farm.id, loc)
        entry = {"loc": loc, "forecast": fc, "fstatus": fs,
                 "climate": cl, "cstatus": cs,
                 "notes": W.plan_notes(fc)}
        if fc is not None:
            cur = fc.get("current") or {}
            cid, clabel = WI.icon_for(cur.get("weather_code"),
                                      cur.get("is_day"))
            entry["current_icon"] = {"id": cid, "label": clabel}
            days = []
            for d in (fc.get("daily") or []):
                d = dict(d or {})
                did, dlabel = WI.icon_for_day(d.get("weather_code"))
                d["icon_id"] = did
                d["icon_label"] = dlabel
                days.append(d)
            entry["days"] = days
        else:
            unk_id, unk_label = WI.unknown()
            entry["current_icon"] = {"id": unk_id, "label": unk_label}
            entry["days"] = []
        entries.append(entry)
    try:
        farms = accessible_farms(request.user)
    except Exception:
        farms = [farm]
    return render(request, "portal/weather.html",
                  {"farm": farm, "farms": farms, "active_farm": farm,
                   "entries": entries, "source": W.SOURCE, "license": W.LICENSE})


@login_required
@require_http_methods(["POST"])
def weather_refresh(request):
    farm = _farm(request)
    if role_for(request.user, farm) not in WRITE_ROLES:
        raise PermissionDenied()
    scope = (request.POST.get("scope") or "both").strip()
    if scope not in ("current", "climate", "both"):
        scope = "both"
    n = 0
    for loc in FarmLocation.objects.filter(farm=farm):
        if scope in ("current", "both"):
            W.refresh_forecast(farm.id, loc)
            n += 1
        if scope in ("climate", "both"):
            W.refresh_climate(farm.id, loc)
            n += 1
    messages.success(request, _("Időjárás frissítve (%(n)s lekérés).") % {"n": n})
    return redirect("weather")
