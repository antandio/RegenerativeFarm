"""Stage 3 pure planning: deterministic per-CropCycle work templates.

Pure function (no DB writes). Crop family is detected with a bounded
casefold keyword match (pasture/legelo/gyep, cover/takaro, cereal/buza/
wheat, vegetable/zoldseg, orchard/gyumolcs, fallback generic). State is
bounded: history -> no work; unknown state -> one cautious observation;
planned -> prep/care/harvest (no sowing); current+growing -> no past
soil/sowing (care/harvest only). Observations only add explicit risk
notes (never instructions); frost/drought/heat keywords may add one
small observation task. Date anchor is explicit via optional as_of
(injected by the view, defaults to today); harvest without a known
expected_end_date is labelled estimated, never actual. Equipment must be
available AND assigned to the exact property (unassigned capable machines
count as missing). Weekly keys are (iso_year, iso_week) tuples so the
same ISO week in different years never collapses. Farm capacity default
is 40 h/week (documented assumption, shown in the summary); equipment
weekly capacity is hours_per_day * 5 per unique equipment id (never
double-counted per property).
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext as _g
from django.utils.translation import gettext_lazy as _

from .regenerative_policy import POLICY_VERSION, summary_reference

GUIDANCE = _("Oktatási célú ütemezési segédlet – nem helyettesíti a szakértői véleményt, garancia nincs.")
NO_GUARANTEE = _("A terv teljességére és minden kockázat felismerésére garancia nincs.")
# Language-neutral fixed-guidance reference carried by every generated item.
# Short on purpose: item guidance persists into a 240-char field.
POLICY_ITEM_REF = "Regenerative-policy %s." % POLICY_VERSION

TEMPLATES = [
    {"key": "soil", "title": _("Talaj-előkészítés"), "capability": "tillage",
     "duration_days": 3, "offset_start": -14, "offset_end": -12, "hours": Decimal("6")},
    {"key": "sow", "title": _("Vetés"), "capability": "sowing",
     "duration_days": 2, "offset_start": 0, "offset_end": 1, "hours": Decimal("4")},
    {"key": "care", "title": _("Ápolás/öntözés"), "capability": "irrigation",
     "duration_days": 5, "offset_start": 14, "offset_end": 18, "hours": Decimal("5")},
    {"key": "harvest", "title": _("Betakarítás"), "capability": "harvest",
     "duration_days": 3, "offset_start": None, "offset_end": None, "hours": Decimal("8")},
]

FAMILY_TEMPLATES = {
    "cereal": TEMPLATES,
    "generic": TEMPLATES,
    "vegetable": [
        {"key": "soil", "title": _("Talaj-előkészítés"), "capability": "tillage",
         "duration_days": 3, "offset_start": -14, "offset_end": -12, "hours": Decimal("6")},
        {"key": "sow", "title": _("Palántázás/vetés"), "capability": "sowing",
         "duration_days": 2, "offset_start": 0, "offset_end": 1, "hours": Decimal("5")},
        {"key": "care", "title": _("Ápolás/öntözés"), "capability": "irrigation",
         "duration_days": 5, "offset_start": 14, "offset_end": 18, "hours": Decimal("6")},
        {"key": "harvest", "title": _("Szedés/betakarítás"), "capability": "harvest",
         "duration_days": 4, "offset_start": None, "offset_end": None, "hours": Decimal("7")},
    ],
    "cover": [
        {"key": "soil", "title": _("Talaj-előkészítés"), "capability": "tillage",
         "duration_days": 2, "offset_start": -7, "offset_end": -6, "hours": Decimal("4")},
        {"key": "sow", "title": _("Takarónövény vetése"), "capability": "sowing",
         "duration_days": 2, "offset_start": 0, "offset_end": 1, "hours": Decimal("3")},
        {"key": "care", "title": _("Takarónövény lezárása"), "capability": "other",
         "duration_days": 2, "offset_start": 30, "offset_end": 31, "hours": Decimal("3")},
    ],
    "pasture": [
        {"key": "graze", "title": _("Legeltetési rotáció"), "capability": "livestock",
         "duration_days": 3, "offset_start": 0, "offset_end": 2, "hours": Decimal("4")},
        {"key": "pasture_care", "title": _("Gyepápolás/kaszálás"), "capability": "harvest",
         "duration_days": 2, "offset_start": 14, "offset_end": 15, "hours": Decimal("5")},
        {"key": "fence", "title": _("Kerítés/állatjóléti ellenőrzés"), "capability": "other",
         "duration_days": 1, "offset_start": 7, "offset_end": 7, "hours": Decimal("2")},
    ],
    "orchard": [
        {"key": "prune", "title": _("Metszés/koronaápolás"), "capability": "other",
         "duration_days": 3, "offset_start": -21, "offset_end": -19, "hours": Decimal("6")},
        {"key": "care", "title": _("Öntözés/tápanyag-utánpótlás"), "capability": "irrigation",
         "duration_days": 5, "offset_start": 14, "offset_end": 18, "hours": Decimal("5")},
        {"key": "harvest", "title": _("Szüret"), "capability": "harvest",
         "duration_days": 4, "offset_start": None, "offset_end": None, "hours": Decimal("8")},
    ],
}

WEEK_CAPACITY_HOURS = Decimal("40")
FARM_WEEK_CAPACITY_HOURS = WEEK_CAPACITY_HOURS
CAPACITY_NOTE = "Feltételezés: 40 óra/hét gazdaság-szintű kapacitás (dokumentált alapértelmezés)."
EQUIPMENT_CAPACITY_NOTE = "Gépkapacitás: napi óraszám × 5 munkanap/hét egyedi gépenként, területenként nem duplikálva."
WORKDAYS_PER_WEEK = Decimal("5")

GROWING_KEYWORDS = ("grow", "veget", "flower", "fruit", "növek", "novek", "fejlőd",
                    "fejlod", "bokros", "szárba", "szarba", "csíra", "csira",
                    "kelés", "keles", "hajtás", "hajtas", "termés", "termes",
                    "érés", "eres", "virág", "virag")
PREP_KEYS = frozenset({"soil", "sow", "prune"})
SOW_KEYS = frozenset({"sow"})
HARVEST_KEYS = frozenset({"harvest"})

OBS_RISK_KEYWORDS = (
    ("fagy", ("fagy", "frost")),
    ("szárazság/aszály", ("aszály", "aszaly", "száraz", "szaraz", "szárazság", "drought")),
    ("hőség", ("hőség", "hoseg", "forró", "forro", "kánikula", "kanikula", "hot", "heat", "hőstressz")),
)


def _crop_family(crop_name):
    n = (crop_name or "").casefold()
    if any(k in n for k in ("pasture", "legel", "gyep")):
        return "pasture"
    if any(k in n for k in ("cover", "takaró", "takaro")):
        return "cover"
    if any(k in n for k in ("cereal", "buza", "búza", "wheat", "gabona")):
        return "cereal"
    if any(k in n for k in ("vegetable", "zöldség", "zoldseg")):
        return "vegetable"
    if any(k in n for k in ("orchard", "gyümölcs", "gyumolcs")):
        return "orchard"
    return "generic"


def _is_growing(growth_stage):
    n = (growth_stage or "").casefold()
    return any(k in n for k in GROWING_KEYWORDS)


def _area_factor(cycle):
    try:
        a = Decimal(str(cycle.get("area_hectares", "1") if cycle.get("area_hectares") is not None else "1"))
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return Decimal("1")
    if a <= 0:
        return Decimal("1")
    return a


def _scaled_hours(base, factor):
    try:
        return (Decimal(str(base)) * factor).quantize(Decimal("0.1"))
    except Exception:
        return Decimal(str(base))


def _obs_risks(observations):
    n = (observations or "").casefold()
    return [label for label, kws in OBS_RISK_KEYWORDS if any(k in n for k in kws)]


def _anchor(cycle, as_of=None):
    today = as_of or date.today()
    base = cycle.get("sow_date") or today
    end = cycle.get("expected_end_date")
    return base, end


def _pick_equipment(tmpl, cycle, equipment):
    """Only available equipment assigned to the exact property counts."""
    cands = [e for e in (equipment or [])
             if tmpl["capability"] in (e.get("capabilities") or []) and e.get("available")]
    if not cands:
        return None, True
    prop_id = cycle.get("property_id")
    for e in cands:
        assigned = e.get("property_ids") or []
        if prop_id is not None and prop_id in assigned:
            return e, False
    return None, True


def annotate_weather(entry, start_month=None):
    """Pure weather annotation for one property.

    `entry` (built cache-only by the caller, never fetched here):
    {"forecast": dict|None, "fstatus": str, "climate": dict|None,
     "cstatus": str}. Returns a list of display strings: forecast notes with
    timestamp/units, stale/missing risk caveat, and climate evidence summary
    (historical baseline, NOT forecast) with monthly context. No DB, no
    network, no cache access — purity preserved.
    """
    from .weather import plan_notes
    entry = entry or {}
    fc = entry.get("forecast")
    fs = entry.get("fstatus") or "missing"
    cl = entry.get("climate")
    notes = []
    if not fc:
        notes.append(_g("Időjárási adat hiányzik (%s) – terv óvatosan értelmezendő.") % fs)
    else:
        notes.extend(plan_notes(fc))
        units = fc.get("units") or {}
        notes.append(_g("Előrejelzés lekérve: %s (%s, %s, %s).") % (
            fc.get("fetched_at"),
            units.get("temperature", "°C"),
            units.get("precipitation", "mm"),
            units.get("wind", "km/h")))
        if fs != "fresh":
            notes.append(_g("Gyorsítótárazott/elavult adat (%s) – változhat, kockázat: "
                            "döntés előtt frissítsen.") % fs)
    if not cl:
        notes.append(_g("Éghajlati alap hiányzik (%s) – történeti összevetés nélkül.") %
                     (entry.get("cstatus") or "missing"))
    else:
        notes.append(_g("Éghajlati bizonyíték (NOT forecast): %s. %s.") % (
            cl.get("period"), cl.get("limitation")))
        if start_month is not None:
            row = None
            for m in (cl.get("monthly") or []):
                if m.get("month") == start_month:
                    row = m
                    break
            if row:
                notes.append(_g("%d. havi átlag: max %s °C, min %s °C, csapadék %s mm/nap.") % (
                    start_month, row.get("tmax_mean"), row.get("tmin_mean"),
                    row.get("precip_mean")))
        notes.append(_g("Éghajlat lekérve: %s.") % cl.get("fetched_at"))
    return notes


def _weather_evidence(weather):
    ev = {}
    for pid, entry in (weather or {}).items():
        entry = entry or {}
        fc = entry.get("forecast") or {}
        cl = entry.get("climate") or {}
        ev[pid] = {
            "fstatus": entry.get("fstatus") or "missing",
            "cstatus": entry.get("cstatus") or "missing",
            "fetched_at": fc.get("fetched_at"),
            "forecast_units": fc.get("units"),
            "climate_period": cl.get("period"),
            "climate_years": cl.get("years"),
            "limitation": cl.get("limitation"),
            "climate_fetched_at": cl.get("fetched_at"),
        }
    return ev


def _week_key(d):
    iso = d.isocalendar()
    return (iso[0], iso[1])


def generate_plan(cycles, equipment, weather=None, as_of=None):
    """Return (items, summary). items: list of dicts per CropCycle template.

    Each item dict keys: crop_cycle_id, property_id, crop_name, title,
    capability, start_date, end_date, estimated_hours, equipment_id,
    missing, recommendation, guidance, weather_annotations. summary:
    week_peaks {(year, week): hours}, over_capacity_weeks, week_capacity_hours,
    capacity_note, equipment_peaks, over_equipment_weeks,
    equipment_capacity_note, guidance, no_guarantee, weather_evidence.
    Deterministic: same input -> same output; sorted by (crop_cycle_id, start).
    `weather` is an optional caller-built map {property_id: entry} from cached
    location data only (see annotate_weather); None preserves the old pure
    behaviour (annotations then carry a missing-data note). `as_of` is the
    explicit date anchor (injected by the view); defaults to today.
    """
    anchor_day = as_of or date.today()
    eq_hours = {}
    for e in (equipment or []):
        try:
            h = Decimal(str(e.get("hours_per_day", "8") or "8"))
        except (InvalidOperation, ValueError, TypeError):
            h = Decimal("8")
        if h <= 0:
            h = Decimal("8")
        if e.get("id") is not None:
            eq_hours[e.get("id")] = h
    items = []
    for c in cycles or []:
        state = (c.get("state") or "current") if isinstance(c, dict) else "current"
        if state == "history":
            continue
        if state not in ("current", "planned"):
            factor = _area_factor(c)
            s = anchor_day
            risks = _obs_risks(c.get("observations"))
            risk_txt = (" Kockázati megjegyzés (megfigyelés alapján, nem utasítás): %s."
                        % ", ".join(risks)) if risks else ""
            items.append({
                "crop_cycle_id": c.get("id"),
                "property_id": c.get("property_id"),
                "crop_name": c.get("crop_name", ""),
                "title": "%s – %s" % (c.get("crop_name", ""), _g("Óvatos megfigyelés – ismeretlen állapot")),
                "capability": "other",
                "start_date": s,
                "end_date": s,
                "estimated_hours": _scaled_hours(Decimal("1"), factor),
                "equipment_id": None,
                "missing": False,
                "recommendation": str(_g("Ismeretlen állapot: szakértői egyeztetés javasolt.")),
                "guidance": str(GUIDANCE) + " " + str(NO_GUARANTEE) + risk_txt,
            })
            continue
        family = _crop_family(c.get("crop_name"))
        tmpls = FAMILY_TEMPLATES.get(family, TEMPLATES)
        if state == "planned":
            tmpls = [t for t in tmpls if t["key"] not in SOW_KEYS]
        elif _is_growing(c.get("growth_stage")):
            tmpls = [t for t in tmpls if t["key"] not in PREP_KEYS]
        base, end = _anchor(c, anchor_day)
        factor = _area_factor(c)
        risks = _obs_risks(c.get("observations"))
        risk_txt = (" Kockázati megjegyzés (megfigyelés alapján, nem utasítás): %s."
                    % ", ".join(risks)) if risks else ""
        for t in tmpls:
            estimated = False
            if t["key"] in HARVEST_KEYS and end:
                s = end - timedelta(days=t["duration_days"] - 1)
                e = end
            elif t["key"] in HARVEST_KEYS:
                s = base + timedelta(days=90)
                e = s + timedelta(days=t["duration_days"] - 1)
                estimated = True
            else:
                s = base + timedelta(days=t["offset_start"] or 0)
                e = s + timedelta(days=t["duration_days"] - 1)
            eq, missing = _pick_equipment(t, c, equipment)
            rec = ""
            if missing:
                rec = _g("Bérlés javasolt (%s): nincs alkalmas saját gép.") % t["capability"]
            title = "%s – %s" % (c.get("crop_name", ""), t["title"])
            guide = str(GUIDANCE) + " " + str(NO_GUARANTEE) + risk_txt
            if estimated:
                title += str(_g(" (becsült, nem tényleges dátum)"))
                guide += " " + str(_g("Becsült dátum: várható befejezés ismeretlen, nem tényleges határidő."))
            items.append({
                "crop_cycle_id": c.get("id"),
                "property_id": c.get("property_id"),
                "crop_name": c.get("crop_name", ""),
                "title": title,
                "capability": t["capability"],
                "start_date": s,
                "end_date": e,
                "estimated_hours": _scaled_hours(t["hours"], factor),
                "equipment_id": (eq or {}).get("id"),
                "missing": missing,
                "recommendation": rec,
                "guidance": guide,
            })
        if risks:
            s = anchor_day
            items.append({
                "crop_cycle_id": c.get("id"),
                "property_id": c.get("property_id"),
                "crop_name": c.get("crop_name", ""),
                "title": "%s – %s (%s)" % (c.get("crop_name", ""),
                                           _g("Megfigyelés: óvatos ellenőrzés"), ", ".join(risks)),
                "capability": "other",
                "start_date": s,
                "end_date": s,
                "estimated_hours": _scaled_hours(Decimal("1"), factor),
                "equipment_id": None,
                "missing": False,
                "recommendation": "",
                "guidance": str(GUIDANCE) + " " + str(NO_GUARANTEE) + risk_txt,
            })
    # Every generated item carries the fixed-guidance reference first, so it
    # survives the 240-char persistence truncation in views.
    for it in items:
        it["guidance"] = POLICY_ITEM_REF + " " + it.get("guidance", "")
    items.sort(key=lambda d: (d["crop_cycle_id"] or 0, d["start_date"], d["title"]))
    for it in items:
        entry = (weather or {}).get(it["property_id"])
        it["weather_annotations"] = annotate_weather(entry, it["start_date"].month)
    weeks = {}
    for it in items:
        wk = _week_key(it["start_date"])
        weeks[wk] = weeks.get(wk, Decimal("0")) + it["estimated_hours"]
    peaks = dict(sorted(weeks.items()))
    over = sorted(w for w, h in peaks.items() if h > WEEK_CAPACITY_HOURS)
    eq_peaks = {}
    for it in items:
        if it["equipment_id"] is None:
            continue
        wk = _week_key(it["start_date"])
        key = (wk[0], wk[1], it["equipment_id"])
        eq_peaks[key] = eq_peaks.get(key, Decimal("0")) + it["estimated_hours"]
    eq_peaks = dict(sorted(eq_peaks.items()))
    over_eq = sorted(k for k, h in eq_peaks.items()
                    if h > eq_hours.get(k[2], Decimal("8")) * WORKDAYS_PER_WEEK)
    summary = {"week_peaks": peaks, "over_capacity_weeks": over,
               "week_capacity_hours": WEEK_CAPACITY_HOURS,
               "capacity_note": CAPACITY_NOTE,
               "equipment_peaks": eq_peaks, "over_equipment_weeks": over_eq,
               "equipment_capacity_note": EQUIPMENT_CAPACITY_NOTE,
               "guidance": str(GUIDANCE), "no_guarantee": str(NO_GUARANTEE),
               "weather_evidence": _weather_evidence(weather)}
    # Binding metadata: every generated plan references the fixed guidance.
    # Deterministic (version + checksum of the bundled asset).
    summary.update(summary_reference())
    return items, summary
