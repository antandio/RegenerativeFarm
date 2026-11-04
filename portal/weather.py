"""Stage 4 weather: global fixed Open-Meteo forecast + archive, cache-only GET.

No nested models, no cloud LLM, no secrets. Public requests only to the two
fixed Open-Meteo hosts (no SSRF: URLs are constants, only query params vary).
"""
from __future__ import annotations

import time
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SOURCE = "Open-Meteo"
LICENSE = "CC BY 4.0"
FORECAST_TTL = 30 * 60
STALE_TTL = 24 * 3600
TIMEOUT = 10.0
ARCHIVE_TIMEOUT = 30.0
CLIMATE_YEARS = 10


class WeatherError(ValueError):
    pass


def validate_point(lat, lon, tz):
    try:
        la = float(lat)
    except (TypeError, ValueError):
        raise WeatherError("invalid latitude")
    try:
        lo = float(lon)
    except (TypeError, ValueError):
        raise WeatherError("invalid longitude")
    if not (-90 <= la <= 90):
        raise WeatherError("latitude out of range")
    if not (-180 <= lo <= 180):
        raise WeatherError("longitude out of range")
    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError):
        raise WeatherError("invalid timezone")
    return la, lo, str(tz)


def _num(x, default=None):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def _wmo(x):
    """Strict WMO code: real int only (`bool` rejected); else None."""
    return x if type(x) is int else None


def _isday(x):
    """Strict day flag: exactly int 0 (night) or int 1 (day); else None."""
    return x if type(x) is int and x in (0, 1) else None


def _get(url, params, timeout, transport=None):
    if transport is not None:
        with httpx.Client(transport=transport, timeout=timeout) as c:
            return c.get(url, params=params)
    return httpx.get(url, params=params, timeout=timeout)


def fetch_forecast(lat, lon, tz, transport=None):
    la, lo, zone = validate_point(lat, lon, tz)
    params = {
        "latitude": la, "longitude": lo, "timezone": zone,
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code",
        "forecast_days": 7,
    }
    try:
        r = _get(FORECAST_URL, params, TIMEOUT, transport)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        raise WeatherError(f"network: {e}")
    if not isinstance(body, dict):
        raise WeatherError("invalid payload")
    cur = body.get("current") or {}
    daily = body.get("daily") or {}
    units = body.get("current_units") or {}
    days = daily.get("time") or []
    if not isinstance(days, list) or not days:
        raise WeatherError("invalid daily")
    n = len(days)
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    psum = daily.get("precipitation_sum") or []
    wmax = daily.get("wind_speed_10m_max") or []
    wcode = daily.get("weather_code") or []
    out_days = []
    for i in range(min(n, 7)):
        out_days.append({
            "date": str(days[i]) if isinstance(days[i], str) else None,
            "tmax": _num(tmax[i] if i < len(tmax) else None),
            "tmin": _num(tmin[i] if i < len(tmin) else None),
            "precip_sum": _num(psum[i] if i < len(psum) else None),
            "wind_max": _num(wmax[i] if i < len(wmax) else None),
            "weather_code": _wmo(wcode[i] if i < len(wcode) else None),
        })
    return {
        "current": {
            "temperature": _num(cur.get("temperature_2m")),
            "precipitation": _num(cur.get("precipitation")),
            "wind_speed": _num(cur.get("wind_speed_10m")),
            "time": cur.get("time") if isinstance(cur.get("time"), str) else None,
            "weather_code": _wmo(cur.get("weather_code")),
            "is_day": _isday(cur.get("is_day")),
        },
        "daily": out_days,
        "units": {
            "temperature": units.get("temperature_2m") if isinstance(units.get("temperature_2m"), str) else "°C",
            "precipitation": units.get("precipitation") if isinstance(units.get("precipitation"), str) else "mm",
            "wind": units.get("wind_speed_10m") if isinstance(units.get("wind_speed_10m"), str) else "km/h",
        },
        "source": SOURCE, "license": LICENSE,
        "fetched_at": timezone.now().isoformat(),
    }


def fetch_climate(lat, lon, tz, years=CLIMATE_YEARS, transport=None):
    la, lo, zone = validate_point(lat, lon, tz)
    years = max(1, min(int(years), 10))
    end_y = date.today().year - 1
    start_y = end_y - years + 1
    if end_y < start_y:
        raise WeatherError("no full calendar year available")
    params = {
        "latitude": la, "longitude": lo, "timezone": zone,
        "start_date": f"{start_y}-01-01", "end_date": f"{end_y}-12-31",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
    }
    try:
        r = _get(ARCHIVE_URL, params, ARCHIVE_TIMEOUT, transport)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        raise WeatherError(f"network: {e}")
    daily = body.get("daily") or {}
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    pr = daily.get("precipitation_sum") or []
    acc = {}
    for i, d in enumerate(times):
        if not isinstance(d, str) or len(d) < 7:
            continue
        m = int(d[5:7])
        a = acc.setdefault(m, {"tx": [], "tn": [], "p": []})
        v = _num(tmax[i] if i < len(tmax) else None)
        w = _num(tmin[i] if i < len(tmin) else None)
        q = _num(pr[i] if i < len(pr) else None)
        if v is not None:
            a["tx"].append(v)
        if w is not None:
            a["tn"].append(w)
        if q is not None:
            a["p"].append(q)
    monthly = []
    for m in range(1, 13):
        a = acc.get(m, {"tx": [], "tn": [], "p": []})
        monthly.append({
            "month": m,
            "tmax_mean": round(sum(a["tx"]) / len(a["tx"]), 1) if a["tx"] else None,
            "tmin_mean": round(sum(a["tn"]) / len(a["tn"]), 1) if a["tn"] else None,
            "precip_mean": round(sum(a["p"]) / len(a["p"]), 2) if a["p"] else None,
        })
    limitation = (_("single-year baseline, not a climate normal") if years == 1
                  else _("multi-year monthly means; not a 30-year normal") if years < 10
                  else _("10-year monthly baseline; not a 30-year normal"))
    return {
        "period": f"{start_y}-{end_y}", "years": years, "monthly": monthly,
        "label": _("historical climate baseline (NOT forecast)"),
        "limitation": limitation,
        "source": SOURCE, "license": LICENSE,
        "fetched_at": timezone.now().isoformat(),
    }


def _ck(kind, farm_id, loc):
    """Cache key binding farm id + location id + coordinates + timezone.

    Coordinates/tz are part of the key so an edited location never serves
    the previous point's payload under the same key.
    """
    loc_id = getattr(loc, "id", loc)
    try:
        lat = float(loc.latitude)
        lon = float(loc.longitude)
        tz = str(loc.timezone)
    except (TypeError, ValueError, AttributeError):
        return f"weather:{kind}:{farm_id}:{loc_id}"
    return f"weather:{kind}:{farm_id}:{loc_id}:{lat:.6f}:{lon:.6f}:{tz}"


def cached(kind, farm_id, loc):
    d = cache.get(_ck(kind, farm_id, loc))
    if not isinstance(d, dict) or "payload" not in d:
        return None, "missing"
    age = time.time() - d.get("stored_at", 0)
    if age <= FORECAST_TTL:
        return d["payload"], "fresh"
    if age <= STALE_TTL:
        p = dict(d["payload"])
        p["stale"] = True
        return p, "stale"
    return None, "outdated"


def get_forecast_cached(farm_id, loc):
    return cached("forecast", farm_id, loc)


def get_climate_cached(farm_id, loc):
    return cached("climate", farm_id, loc)


def refresh_forecast(farm_id, loc, transport=None):
    try:
        payload = fetch_forecast(loc.latitude, loc.longitude, loc.timezone, transport)
    except WeatherError:
        old, status = cached("forecast", farm_id, loc)
        if old is not None:
            old = dict(old)
            old["stale"] = True
            return old, "stale" if status == "stale" else "outdated-fallback"
        return None, "missing"
    cache.set(_ck("forecast", farm_id, loc),
              {"payload": payload, "stored_at": time.time()}, STALE_TTL)
    return payload, "fresh"


def refresh_climate(farm_id, loc, years=CLIMATE_YEARS, transport=None):
    try:
        payload = fetch_climate(loc.latitude, loc.longitude, loc.timezone, years, transport)
    except WeatherError:
        old, status = cached("climate", farm_id, loc)
        if old is not None:
            old = dict(old)
            old["stale"] = True
            return old, "stale" if status == "stale" else "outdated-fallback"
        return None, "missing"
    cache.set(_ck("climate", farm_id, loc),
              {"payload": payload, "stored_at": time.time()}, STALE_TTL)
    return payload, "fresh"


def plan_notes(forecast):
    """Cached-weather annotations for plans: precip/wind/frost constraints.

    Advisory only; never a soil-workability guarantee.
    """
    from django.utils.translation import gettext as _
    notes = []
    if not forecast:
        return [_("Időjárási adat hiányzik – terv óvatosan értelmezendő.")]
    days = forecast.get("daily") or []
    for d in days[:3]:
        ps = d.get("precip_sum")
        if isinstance(ps, (int, float)) and ps >= 10:
            notes.append(_("Csapadékos nap %(date)s (%(mm)s mm) – talajmunka korlát lehet.") % {"date": d.get("date"), "mm": ps})
        wm = d.get("wind_max")
        if isinstance(wm, (int, float)) and wm >= 40:
            notes.append(_("Erős szél %(date)s (%(v)s km/h) – permetezés/szórás korlát.") % {"date": d.get("date"), "v": wm})
        tn = d.get("tmin")
        if isinstance(tn, (int, float)) and tn < 0:
            notes.append(_("Fagyveszély %(date)s (%(t)s °C) – érzékeny munkák halasztandók.") % {"date": d.get("date"), "t": tn})
    notes.append(_("Tájékoztató jellegű – talajmunkára garancia nincs."))
    return notes
