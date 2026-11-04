"""Sourced context manifest builder: goal-bound, hash-pinned, omission-listed.

Every element carries source_id, content hash, locator, trust class,
relevance, uncertainty, revision and timestamp. Anything excluded or
truncated lands in `omissions`: a subset is never presented as the
whole farm. Weather comes only from the existing shared cache (no
network here); an expired or missing forecast is marked unknown and
its measurements are never presented as current, climate history is
labeled as history. All source text is untrusted data: it is delimited
at the model boundary, never fetched from URLs and never executed.
Model classification calls are not made here; only the local keyword
screen runs per source, and ambiguous sources are marked uncertain.
"""
import hashlib
import re

from django.utils import timezone

from . import agri_gate

TRUST_SERVER = "server-record"
TRUST_CACHE = "system-cache"
TRUST_DOC = "granted-document"
TRUST_PRIOR = "prior-output"

TEXT_LIMIT = 2000
TOTAL_TEXT_BUDGET = 24000
MAX_SOURCES = 100
DOC_SECTION_LIMIT = 2000
ROW_CAP_JOURNAL = 20
ROW_CAP_PRIOR_AGENT = 5
ROW_CAP_PRIOR_CHAT = 5
ROW_CAP_PRIOR_ADVICE = 3
ROW_CAP_WEATHER_LOCS = 3

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _goal_tokens(goal):
    return {t.lower() for t in _WORD_RE.findall(str(goal or ""))
            if len(t) >= 3}


def _item(source_id, text, locator, trust, relevance, uncertainty,
          revision=""):
    blob = str(text or "")
    return {
        "source_id": source_id,
        "hash": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "text": blob[:TEXT_LIMIT],
        "truncated": len(blob) > TEXT_LIMIT,
        "locator": locator,
        "trust": trust,
        "relevance": relevance,
        "uncertainty": uncertainty,
        "revision": revision or "",
        "timestamp": timezone.now().isoformat(),
    }


def _attrs(obj, names):
    parts = []
    for name in names:
        try:
            value = getattr(obj, name, None)
        except Exception:
            value = None
        if value is None or value == "":
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _fresh_role(farm, actor):
    """Re-query the actor's current role from live rows, never a cache."""
    try:
        from portal.models import Farm
        live_owner_id = Farm.objects.filter(pk=farm.pk).values_list("owner_id", flat=True).first()
        actor_id = getattr(actor, "pk", None)
        if actor_id is not None and actor_id == live_owner_id:
            return "owner"
    except Exception:
        pass
    try:
        from portal.models import Membership
        row = Membership.objects.filter(
            farm=farm, user=actor, is_active=True).values_list(
                "role", flat=True).first()
        return str(row) if row else "none"
    except Exception:
        return "unknown"


def build_manifest(*, farm, actor, goal, selected_doc_ids=(),
                   granted_doc_ids=(), language="hu",
                   selected_property_ids=(), actor_role=None):
    """Assemble the sourced manifest for one farm and goal."""
    items = []
    omissions = []
    total_chars = 0
    goal_tok = _goal_tokens(goal)
    base_relevance = f"goal:{str(goal or '')[:80]}"
    fresh_role = _fresh_role(farm, actor)
    if actor_role is not None and actor_role != fresh_role:
        omissions.append(
            f"actor-role: passed {actor_role!r} differs from fresh "
            f"{fresh_role!r}; fresh role used")

    def add(source_id, text, locator, trust, uncertainty, revision=""):
        nonlocal total_chars
        blob = str(text or "")
        screen = agri_gate.local_screen(blob)
        if screen == "nonagri":
            omissions.append(f"{source_id}: excluded non-agricultural text")
            return
        if screen == "uncertain":
            uncertainty = (str(uncertainty or "")
                           + "; uncertain: ambiguous agricultural relevance"
                           ).strip("; ")
        if len(items) >= MAX_SOURCES:
            omissions.append(
                f"{source_id}: source cap {MAX_SOURCES} reached")
            return
        take = min(len(blob), TEXT_LIMIT)
        if total_chars + take > TOTAL_TEXT_BUDGET:
            omissions.append(
                f"{source_id}: total text budget {TOTAL_TEXT_BUDGET} exceeded")
            return
        overlap = len(goal_tok & {t.lower() for t in _WORD_RE.findall(blob)
                                  if len(t) >= 3}) if goal_tok else 0
        relevance = f"{base_relevance}; overlap {overlap}/{len(goal_tok)}"
        item = _item(source_id, blob, locator, trust, relevance,
                     uncertainty, revision)
        items.append(item)
        total_chars += len(item["text"])
        if item["truncated"]:
            omissions.append(f"{source_id}: truncated to {TEXT_LIMIT} chars")

    def section(label, func):
        try:
            func()
        except Exception as exc:
            omissions.append(f"{label}: unreadable, treated as missing "
                             f"({type(exc).__name__})")

    selected_props = set()
    for raw in selected_property_ids or []:
        try:
            selected_props.add(int(raw))
        except (TypeError, ValueError):
            omissions.append(f"property:{raw}: invalid id")
    section("farm:profile", lambda: _profile(farm, add, omissions))
    section("parcels", lambda: _parcels(farm, add, omissions,
                                        selected_props))
    section("equipment", lambda: _equipment(farm, add, omissions))
    section("cycles", lambda: _cycles(farm, add, omissions))
    section("journal", lambda: _journal(farm, add, omissions))
    section("livestock", lambda: _livestock(farm, add, omissions))
    section("open-actions", lambda: _open_actions(farm, add, omissions))
    section("documents", lambda: _documents(
        farm, actor, add, omissions, selected_doc_ids, granted_doc_ids))
    section("rules", lambda: _rules(farm, add, omissions))
    section("contacts", lambda: _contacts(farm, actor, add, omissions))
    section("staff", lambda: _staff(farm, add, omissions))
    section("prior-decisions", lambda: _prior(farm, actor, add, omissions))
    section("weather", lambda: _weather(farm, add, omissions))
    if [i for i in items if i["source_id"].startswith("document:")]:
        pass
    elif selected_doc_ids:
        omissions.append("partial farm view: selected documents omitted "
                         "(no grant, revoked, or unreadable)")
    return {"items": items, "omissions": omissions,
            "budget": {"total_chars": total_chars,
                       "limit": TOTAL_TEXT_BUDGET,
                       "sources": len(items), "source_cap": MAX_SOURCES}}


def _profile(farm, add, omissions):
    try:
        data = farm.profile_dict()
    except Exception as exc:
        omissions.append(f"farm:profile: profile_dict failed "
                         f"({type(exc).__name__}), name-only fallback")
        data = {"name": getattr(farm, "name", "")}
    if not isinstance(data, dict):
        omissions.append("farm:profile: profile_dict not a dict, "
                         "name-only fallback")
        data = {"name": getattr(farm, "name", "")}
    lines = [f"{k}={v}" for k, v in data.items() if v]
    try:
        for extra in farm.extra_fields.all().order_by("position", "id"):
            lines.append(f"extra:{extra.key}={extra.value}")
    except Exception:
        omissions.append("farm:extra-fields: unreadable")
    missing = []
    try:
        missing = farm.completeness_missing()
    except Exception:
        pass
    uncertainty = "low"
    if missing:
        uncertainty = f"medium: missing fields {','.join(missing)}"
    add("farm:profile", "\n".join(lines), f"portal.Farm:{farm.pk}",
        TRUST_SERVER, uncertainty)


def _parcels(farm, add, omissions, selected_props):
    try:
        locations = list(farm.locations.all())
    except Exception:
        locations = []
        omissions.append("farm:locations: unreadable")
    for loc in locations:
        text = _attrs(loc, ("name", "latitude", "longitude", "timezone"))
        add(f"location:{loc.pk}", text or f"name={loc}",
            f"portal.FarmLocation:{loc.pk}", TRUST_SERVER, "low")
    try:
        props = list(farm.domain_properties.all().order_by("id"))
    except Exception:
        props = []
        omissions.append("farm:properties: unreadable")
    for prop in props:
        if selected_props and prop.pk not in selected_props:
            omissions.append(f"property:{prop.pk}: not in selected parcels")
            continue
        text = _attrs(prop, ("name", "kind", "area_hectares", "notes"))
        add(f"property:{prop.pk}", text or f"name={prop}",
            f"portal.FarmProperty:{prop.pk}", TRUST_SERVER, "low")


def _equipment(farm, add, omissions):
    try:
        machines = list(farm.equipment.all())
    except Exception:
        machines = []
        omissions.append("farm:equipment: unreadable")
    for mach in machines:
        text = _attrs(mach, ("name", "capabilities", "available",
                             "hours_per_day"))
        add(f"equipment:{mach.pk}", text or f"name={mach}",
            f"portal.Equipment:{mach.pk}", TRUST_SERVER,
            "medium: availability is a flag, not a booked schedule")


def _cycles(farm, add, omissions):
    try:
        cycles = list(farm.crop_cycles.filter(is_hidden=False,
                                              state__in=("current",
                                                         "planned")))
        hidden = farm.crop_cycles.filter(is_hidden=True).count()
    except Exception:
        cycles, hidden = [], 0
        omissions.append("farm:cycles: unreadable")
    if hidden:
        omissions.append(f"farm:cycles: {hidden} hidden rows excluded")
    for cyc in cycles:
        text = _attrs(cyc, ("crop_name", "state", "growth_stage",
                            "sow_date", "expected_end_date",
                            "observations", "property_id"))
        add(f"cycle:{cyc.pk}", text, f"portal.CropCycle:{cyc.pk}",
            TRUST_SERVER, "low")


def _journal(farm, add, omissions):
    try:
        events = list(farm.farming_events.all()
                      .order_by("-event_date", "-id")[:ROW_CAP_JOURNAL + 1])
    except Exception:
        events = []
        omissions.append("farm:farming-events: unreadable")
    if len(events) > ROW_CAP_JOURNAL:
        omissions.append(f"farm:farming-events: row cap {ROW_CAP_JOURNAL}")
        events = events[:ROW_CAP_JOURNAL]
    for ev in events:
        text = _attrs(ev, ("event_date", "event_type",
                           "crop_name_snapshot", "description",
                           "quantity", "unit", "materials",
                           "property_id"))
        add(f"farming-event:{ev.pk}", text,
            f"portal.FarmingEvent:{ev.pk}", TRUST_SERVER, "low")
    try:
        levents = list(farm.livestock_events.all()
                       .order_by("-event_date", "-id")[:ROW_CAP_JOURNAL + 1])
    except Exception:
        levents = []
        omissions.append("farm:livestock-events: unreadable")
    if len(levents) > ROW_CAP_JOURNAL:
        omissions.append(f"farm:livestock-events: row cap {ROW_CAP_JOURNAL}")
        levents = levents[:ROW_CAP_JOURNAL]
    for ev in levents:
        text = _attrs(ev, ("event_date", "kind", "headcount_delta",
                           "notes", "group_id"))
        add(f"livestock-event:{ev.pk}", text,
            f"portal.LivestockEvent:{ev.pk}", TRUST_SERVER, "low")
    try:
        works = list(farm.work_items.all()
                     .order_by("-id")[:ROW_CAP_JOURNAL + 1])
    except Exception:
        works = []
        omissions.append("farm:work-items: unreadable")
    if len(works) > ROW_CAP_JOURNAL:
        omissions.append(f"farm:work-items: row cap {ROW_CAP_JOURNAL}")
        works = works[:ROW_CAP_JOURNAL]
    for work in works:
        text = _attrs(work, ("status", "title", "capability",
                             "start_date", "end_date",
                             "estimated_hours", "equipment_id",
                             "property_id", "crop_cycle_id"))
        add(f"work-item:{work.pk}", text or f"work-item #{work.pk}",
            f"portal.WorkItem:{work.pk}", TRUST_SERVER,
            "medium: saved work, not a fresh observation")


def _livestock(farm, add, omissions):
    try:
        groups = list(farm.livestock_groups.filter(is_active=True))
        inactive = farm.livestock_groups.filter(is_active=False).count()
    except Exception:
        groups, inactive = [], 0
        omissions.append("farm:livestock: unreadable")
    if inactive:
        omissions.append(f"farm:livestock: {inactive} archived groups "
                         "excluded (history preserved, not reused)")
    for group in groups:
        housing = getattr(getattr(group, "housing_property", None),
                          "name", "")
        text = _attrs(group, ("name", "species", "breed_label",
                              "headcount"))
        if housing:
            text += f"; housing={housing}"
        add(f"livestock-group:{group.pk}", text,
            f"portal.LivestockGroup:{group.pk}", TRUST_SERVER,
            "low: live headcount, dated events carry movements")


def _open_actions(farm, add, omissions):
    try:
        actions = list(farm.actions.filter(status="open")
                       .order_by("-created_at", "-id")[:ROW_CAP_JOURNAL + 1])
    except Exception:
        actions = []
        omissions.append("farm:actions: unreadable")
    if len(actions) > ROW_CAP_JOURNAL:
        omissions.append(f"farm:actions: row cap {ROW_CAP_JOURNAL}")
        actions = actions[:ROW_CAP_JOURNAL]
    for action in actions:
        text = _attrs(action, ("text", "status"))
        add(f"action:{action.pk}", text or f"action #{action.pk}",
            f"portal.FarmAction:{action.pk}", TRUST_SERVER,
            "medium: open action, outcome not yet observed")


def _documents(farm, actor, add, omissions, selected_doc_ids,
               granted_doc_ids):
    from portal.domain_models import Document, FarmDocument
    from portal.models import current_consent_granted
    from .storage_sections import read_blob_for_manifest
    granted = set()
    for raw in granted_doc_ids or []:
        try:
            granted.add(int(raw))
        except (TypeError, ValueError):
            continue
    consent = False
    try:
        consent = bool(current_consent_granted(farm))
    except Exception:
        consent = False
    actor_id = getattr(actor, "pk", None)
    for raw_id in selected_doc_ids or []:
        try:
            doc_id = int(raw_id)
        except (TypeError, ValueError):
            omissions.append(f"document:{raw_id}: invalid id")
            continue
        sid = f"document:{doc_id}"
        try:
            doc = Document.objects.get(pk=doc_id)
        except Document.DoesNotExist:
            omissions.append(f"{sid}: not found or withdrawn")
            continue
        eligible = (doc.owner_id == actor_id) or doc.is_public
        assigned = FarmDocument.objects.filter(
            farm=farm, document=doc, is_active=True).exists()
        if not (eligible and assigned):
            omissions.append(f"{sid}: not eligible (owner/public "
                             "or farm assignment changed)")
            continue
        if not consent:
            omissions.append(f"{sid}: farm cloud consent not granted")
            continue
        if doc_id not in granted:
            omissions.append(f"{sid}: no separate per-run document "
                             "permission")
            continue
        blob = read_blob_for_manifest(doc.storage_name, DOC_SECTION_LIMIT)
        if not blob["available"]:
            omissions.append(f"{sid}: unreadable section "
                             f"({blob.get('reason')})")
            continue
        expected = str(doc.sha256 or "").lower()
        got = str(blob.get("sha256") or "").lower()
        if len(expected) != 64 or expected != got:
            omissions.append(f"{sid}: hash mismatch, unsupported")
            continue
        try:
            updated = doc.updated_at.isoformat()
        except Exception:
            updated = ""
        uncertainty = "medium: upload text, OCR/parse limits may apply"
        if blob.get("replaced"):
            uncertainty += "; decoded with replacement characters"
        if blob["truncated"]:
            uncertainty += "; truncated to section budget"
            omissions.append(f"{sid}: truncated to section budget")
        nlines = blob.get("lines") or 1
        add(sid, blob["text"],
            f"portal.Document:{doc.pk}#L1-L{nlines} "
            f"rev-sha:{got[:16]}",
            TRUST_DOC, uncertainty, updated)
    try:
        installed = set(FarmDocument.objects.filter(
            farm=farm, is_active=True).values_list("document_id",
                                                  flat=True))
        for doc_id in sorted(installed):
            if int(doc_id) not in {int(d) for d in selected_doc_ids or []}:
                omissions.append(
                    f"document:{doc_id}: installed but not selected "
                    "for this run")
    except Exception:
        pass


def _rules(farm, add, omissions):
    from portal.domain_models import RuleActivation
    try:
        activations = list(RuleActivation.objects.filter(
            farm=farm, is_active=True).select_related("rule"))
    except Exception:
        activations = []
        omissions.append("farm:rules: unreadable")
    for act in activations:
        rule = getattr(act, "rule", None)
        if rule is None:
            omissions.append(f"rule-activation:{act.pk}: rule missing")
            continue
        eligible = (rule.owner_id == farm.owner_id) or rule.is_public
        if not eligible:
            omissions.append(f"rule:{rule.pk}: visibility changed, "
                             "not reused from index")
            continue
        add(f"rule:{rule.pk}", str(rule.body or "")[:TEXT_LIMIT],
            f"portal.RuleDefinition:{rule.pk}", TRUST_SERVER,
            "subordinate: central policy wins on conflict; "
            "expert review on override claims")


def _contacts(farm, actor, add, omissions):
    try:
        is_owner = (getattr(actor, "pk", None) == farm.owner_id)
    except Exception:
        is_owner = False
    if not is_owner:
        omissions.append("farm:contacts: owner only, not fetched")
        return
    try:
        contacts = list(farm.external_contacts.all())
    except Exception:
        contacts = []
        omissions.append("farm:contacts: unreadable")
    for contact in contacts:
        text = _attrs(contact, ("name", "services", "availability"))
        add(f"contact:{contact.pk}", text,
            f"portal.ExternalContact:{contact.pk}", TRUST_SERVER,
            "capacity unknown: availability text only; no hire, order, "
            "payment or message without a separate explicit approval")


def _staff(farm, add, omissions):
    try:
        memberships = list(farm.memberships.filter(is_active=True))
    except Exception:
        memberships = []
        omissions.append("farm:staff: unreadable")
    for member in memberships:
        add(f"membership:{member.pk}", f"role={member.role}",
            f"portal.Membership:{member.pk}", TRUST_SERVER,
            "capacity unknown: membership is not booked working time; "
            "minimal display only")


def _prior(farm, actor, add, omissions):
    from .models import AgentRun
    try:
        runs = list(AgentRun.objects.filter(
            farm=farm, initiated_by=actor)
            .exclude(status__in=("deleted", "queued", "running"))
            .order_by("-completed_at", "-id")[:ROW_CAP_PRIOR_AGENT + 1])
    except Exception:
        runs = []
        omissions.append("farm:prior-agent-runs: unreadable")
    if len(runs) > ROW_CAP_PRIOR_AGENT:
        omissions.append("farm:prior-agent-runs: row cap "
                         f"{ROW_CAP_PRIOR_AGENT}")
        runs = runs[:ROW_CAP_PRIOR_AGENT]
    for prior in runs:
        summary = ""
        try:
            summary = (prior.candidate or {}).get("summary", "")
        except Exception:
            summary = ""
        add(f"agent-run:{prior.pk}",
            f"status={prior.status}; summary={summary}",
            f"portal.AgentRun:{prior.pk}", TRUST_PRIOR,
            "earlier decision output, revalidated only if reused")
    try:
        from portal.chat_models import ChatAttempt
        chats = list(ChatAttempt.objects.filter(
            farm=farm, initiated_by=actor, status="success")
            .order_by("-attempt_started_at",
                      "-id")[:ROW_CAP_PRIOR_CHAT + 1])
    except Exception:
        chats = []
    if len(chats) > ROW_CAP_PRIOR_CHAT:
        omissions.append("farm:prior-chat: row cap "
                         f"{ROW_CAP_PRIOR_CHAT}")
        chats = chats[:ROW_CAP_PRIOR_CHAT]
    for chat in chats:
        text = _attrs(chat, ("user_message", "answer_text"))
        add(f"chat-attempt:{chat.pk}", text or "earlier chat",
            f"portal.ChatAttempt:{chat.pk}", TRUST_PRIOR,
            "earlier own question and answer, not a fact source")
    try:
        from portal.models import AdviceRun
        advices = list(AdviceRun.objects.filter(
            farm=farm, initiated_by=actor, status="success")
            .order_by("-completed_at",
                      "-id")[:ROW_CAP_PRIOR_ADVICE + 1])
    except Exception:
        advices = []
    if len(advices) > ROW_CAP_PRIOR_ADVICE:
        omissions.append("farm:prior-advice: row cap "
                         f"{ROW_CAP_PRIOR_ADVICE}")
        advices = advices[:ROW_CAP_PRIOR_ADVICE]
    for advice in advices:
        text = _attrs(advice, ("result_assumptions",
                               "result_focused_questions"))
        add(f"advice-run:{advice.pk}", text or "earlier advice",
            f"portal.AdviceRun:{advice.pk}", TRUST_PRIOR,
            "earlier educational output, not a fact source")


def _loc_today(tzname):
    from datetime import date
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return date.today(), None
    except Exception:
        return None, "unknown"


def _forecast_valid(daily, tzname):
    from datetime import date
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo(str(tzname))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None
    try:
        from datetime import datetime
        today = datetime.now(zone).date()
    except Exception:
        today = date.today()
    dates = [d.get("date") for d in daily or [] if d.get("date")]
    if not dates:
        return False
    return max(dates) >= today.isoformat()


def _weather(farm, add, omissions):
    from portal import weather as wx
    try:
        locations = list(farm.locations.all())[:ROW_CAP_WEATHER_LOCS + 1]
    except Exception:
        locations = []
        omissions.append("farm:weather: locations unreadable")
    if len(locations) > ROW_CAP_WEATHER_LOCS:
        omissions.append("farm:weather: location cap "
                         f"{ROW_CAP_WEATHER_LOCS}")
        locations = locations[:ROW_CAP_WEATHER_LOCS]
    if not locations:
        add("weather:none", "no farm location stored",
            "portal.FarmLocation:missing", TRUST_CACHE,
            "unknown: no location, no weather guessed")
        return
    for loc in locations:
        tz = getattr(loc, "timezone", "") or "unknown-timezone"
        try:
            lat = float(loc.latitude)
            lon = float(loc.longitude)
        except (TypeError, ValueError):
            lat, lon = None, None
        for kind in ("forecast", "climate"):
            try:
                payload, status = wx.cached(kind, farm.pk, loc)
            except Exception:
                payload, status = None, "missing"
            sid = f"weather:{kind}:{loc.pk}"
            locator = (f"weather-cache:{kind} farm={farm.pk} "
                       f"loc={loc.pk} tz={tz}")
            if kind == "climate":
                if status == "fresh" and isinstance(payload, dict):
                    monthly = payload.get("monthly") or []
                    mtext = "; ".join(
                        f"m{m.get('month')}: tmax={m.get('tmax_mean')}, "
                        f"tmin={m.get('tmin_mean')}, "
                        f"precip={m.get('precip_mean')}"
                        for m in monthly[:12])
                    text = (f"historical climate baseline (NOT a forecast); "
                            f"period={payload.get('period')}; "
                            f"years={payload.get('years')}; {mtext}; "
                            f"source={payload.get('source')}; "
                            f"license={payload.get('license')}; "
                            f"fetched_at={payload.get('fetched_at')}; "
                            f"lat={lat}; lon={lon}; tz={tz}")
                    add(sid, text, locator, TRUST_CACHE,
                        "historical baseline only; not a forecast",
                        str(payload.get("fetched_at") or ""))
                else:
                    add(sid,
                        f"historical climate baseline (NOT a forecast); "
                        f"tz={tz}; cache status={status}; no download here "
                        f"(shared cache read only)",
                        locator, TRUST_CACHE,
                        "unknown: missing/expired, must not be treated "
                        "as conditions")
                continue
            if status == "fresh" and isinstance(payload, dict):
                daily = payload.get("daily") or []
                valid = _forecast_valid(daily, tz)
                current = payload.get("current") or {}
                units = payload.get("units") or {}
                horizon = ",".join(
                    str(d.get("date")) for d in daily[:7] if d.get("date"))
                if valid is False:
                    add(sid,
                        f"forecast snapshot EXPIRED; tz={tz}; horizon={horizon}; "
                        f"fetched_at={payload.get('fetched_at')}; "
                        f"no download here (shared cache read only)",
                        locator, TRUST_CACHE,
                        "unknown: expired forecast horizon, measurements "
                        "must not be treated as current conditions",
                        str(payload.get("fetched_at") or ""))
                    continue
                if valid is None:
                    qual = ("unknown-timezone: horizon validity unknown; "
                            "measurements provisional")
                else:
                    qual = "low while fresh; re-check validity before apply"
                text = (f"forecast snapshot; tz={tz}; horizon={horizon}; "
                        f"current temp={current.get('temperature')} "
                        f"{units.get('temperature')}; "
                        f"precip={current.get('precipitation')} "
                        f"{units.get('precipitation')}; "
                        f"wind={current.get('wind_speed')} "
                        f"{units.get('wind')}; "
                        f"time={current.get('time')}; "
                        f"source={payload.get('source')}; "
                        f"license={payload.get('license')}; "
                        f"fetched_at={payload.get('fetched_at')}; "
                        f"lat={lat}; lon={lon}")
                add(sid, text, locator, TRUST_CACHE, qual,
                    str(payload.get("fetched_at") or ""))
            else:
                add(sid, f"forecast snapshot; tz={tz}; cache status={status}; "
                    "no download here (shared cache read only)",
                    locator, TRUST_CACHE,
                    "unknown: expired/missing, must not be treated "
                    "as current conditions")