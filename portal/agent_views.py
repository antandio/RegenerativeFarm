"""Async agent HTTP UI: enqueue (reserve-only), status poll, cancel/delete.

English comments per project convention. GET routes never construct a
model client, never call external HTTP and never write to the DB: the
status poll only reads the run, its call ledger and its immutable
changeset previews.
"""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from portal.agent import budgets
from portal.agent.models import AgentCall, AgentChangeset, AgentDocGrant, AgentRun
from portal.agent.run import enqueue_run
from portal.models import current_consent_granted
from portal.models import Farm
from portal.permissions import get_active_farm, role_for


def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return Farm.objects.get(pk=farm.pk)


def _run_or_404(farm, user, run_id):
    """Own-server history with fresh role: cross user/farm reads 404."""
    try:
        run = AgentRun.objects.filter(
            pk=int(run_id), farm=farm,
            initiated_by=user).select_related(
            "farm", "initiated_by").first()
    except (ValueError, TypeError):
        raise Http404()
    if run is None or run.status == "deleted":
        raise Http404()
    if role_for(user, farm) is None:
        raise Http404()
    if run.farm_id != farm.pk:
        raise Http404()
    return run


def _permitted_property_ids(farm):
    from portal.domain_models import FarmProperty
    return set(FarmProperty.objects.filter(
        farm_id=farm.pk).values_list("id", flat=True))


def _permitted_doc_ids(farm, actor):
    from django.db.models import Q
    from portal.domain_models import FarmDocument
    return set(FarmDocument.objects.filter(
        farm_id=farm.pk, is_active=True).filter(
        Q(document__owner=actor) | Q(
            document__is_public=True)).values_list(
        "document_id", flat=True))


def _parse_ids(raw_values, limit=50):
    out = []
    for value in (raw_values or [])[:limit]:
        try:
            number = int(str(value).strip())
        except (ValueError, TypeError, AttributeError):
            continue
        if number > 0:
            out.append(number)
    return sorted(set(out))


@login_required
@require_http_methods(["GET", "POST"])
def agent_home(request):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    limits = budgets.get_limits()
    if request.method == "POST":
        if role not in ("owner", "manager", "worker"):
            raise PermissionDenied()
        if (request.POST.get("cost_consent") or "") != "on":
            messages.error(request, _(
                "Indításhoz kifejezett költség-hozzájárulás szükséges."))
            return redirect("agent_home")
        if not current_consent_granted(Farm.objects.get(pk=farm.pk)):
            messages.error(request, _(
                "Indításhoz felhőadat-kezelési hozzájárulás szükséges."))
            return redirect("agent_home")
        goal = str(request.POST.get("goal") or "").strip()[:2000]
        if not goal:
            messages.error(request, _("A cél megadása kötelező."))
            return redirect("agent_home")
        language = str(request.POST.get("language") or "hu")[:10] or "hu"
        permitted_props = _permitted_property_ids(farm)
        permitted_docs = _permitted_doc_ids(farm, request.user)
        selected_props = _parse_ids(
            request.POST.getlist("selected_property_ids"))
        selected_docs = _parse_ids(request.POST.getlist("selected_doc_ids"))
        granted_docs = _parse_ids(request.POST.getlist("granted_doc_ids"))
        if any(pid not in permitted_props for pid in selected_props):
            messages.error(request, _(
                "Ismeretlen területi azonosító a kérésben."))
            return redirect("agent_home")
        if any(did not in permitted_docs for did in selected_docs):
            messages.error(request, _(
                "Ismeretlen dokumentum-azonosító a kérésben."))
            return redirect("agent_home")
        if any(did not in permitted_docs for did in granted_docs):
            messages.error(request, _(
                "Ismeretlen dokumentum-azonosító a felhő-hozzájárulásban."))
            return redirect("agent_home")
        if any(did not in selected_docs for did in granted_docs):
            messages.error(request, _(
                "Felhőbe csak a kiválasztott dokumentumok engedhetők."))
            return redirect("agent_home")
        key = str(request.POST.get("idempotency_key") or "").strip()[:80]
        if not key:
            key = uuid.uuid4().hex
        try:
            run = enqueue_run(
                farm=farm, actor=request.user, goal=goal,
                idempotency_key=f"agent-{request.user.pk}-{key}",
                language=language, selected_doc_ids=selected_docs,
                granted_doc_ids=granted_docs,
                selected_property_ids=selected_props,
                limits_overrides=None)
        except ValueError:
            messages.error(request, _(
                "Ez az idempotencia-kulcs már más tartalommal szerepel."))
            return redirect("agent_home")
        return redirect("agent_detail", run_id=run.pk)
    runs = list(AgentRun.objects.filter(
        farm=farm, initiated_by=request.user,
        deleted_at__isnull=True).exclude(
        status="deleted").order_by("-created_at", "-id")[:20])
    own_active = AgentRun.objects.filter(
        farm=farm, initiated_by=request.user,
        deleted_at__isnull=True).exclude(
        status__in=("deleted", "done", "blocked", "error",
                    "outcome_unknown", "cancelled")).order_by(
        "-created_at", "-id").first()
    from django.db.models import Q
    from portal.domain_models import FarmDocument, FarmProperty
    properties = list(FarmProperty.objects.filter(
        farm_id=farm.pk).order_by("name", "id")[:100])
    farm_docs = list(FarmDocument.objects.filter(
        farm_id=farm.pk, is_active=True).filter(
        Q(document__owner=request.user) | Q(
            document__is_public=True)).select_related(
        "document").order_by("-updated_at", "-id")[:100])
    return render(request, "portal/agent_list.html", {
        "runs": runs,
        "own_active_run": own_active,
        "properties": properties,
        "farm_docs": farm_docs,
        "limits": limits,
        "page_title": _("Ügynök"),
    })


@login_required
@require_http_methods(["GET"])
def agent_detail(request, run_id):
    farm = _farm_or_404(request)
    run = _run_or_404(farm, request.user, run_id)
    calls = list(AgentCall.objects.filter(run=run).order_by(
        "started_at", "id"))
    previews = list(AgentChangeset.objects.filter(
        run=run).order_by("-created_at", "-id"))
    try:
        from portal.agent import changeset as cs_mod
        for cs in previews:
            # Only preview changesets get an approval token.
            if cs.state != "preview":
                cs.approve_token = ""
                continue
            try:
                cs.approve_token = cs_mod.make_token(cs)
            except Exception:
                cs.approve_token = ""
    except Exception:
        for cs in previews:
            cs.approve_token = ""
    return render(request, "portal/agent_detail.html", {
        "run": run,
        "calls": calls,
        "previews": previews,
        "page_title": _("Ügynökfutás"),
    })


@login_required
@require_http_methods(["GET"])
def agent_status(request, run_id):
    """Read-only poll: status/stage, safe errors, sources, policy hash,
    summary, criticism, call usage, budgets and immutable previews."""
    farm = _farm_or_404(request)
    run = _run_or_404(farm, request.user, run_id)
    calls = list(AgentCall.objects.filter(run=run).order_by(
        "started_at", "id"))
    previews = list(AgentChangeset.objects.filter(
        run=run, state="preview").order_by("-created_at", "-id"))
    manifest = run.context_manifest or {}
    sources = []
    for item in (manifest.get("items") or [])[:100]:
        if not isinstance(item, dict):
            continue
        sources.append({
            "source_id": str(item.get("source_id") or ""),
            "hash": str(item.get("hash") or ""),
            "revision": str(item.get("revision") or ""),
            "locator": str(item.get("locator") or ""),
            "trust": str(item.get("trust") or ""),
        })
    preview_data = []
    for cs in previews:
        preview_data.append({
            "id": cs.pk,
            "state": cs.state,
            "payload_hash": cs.payload_hash,
            "commands": cs.commands or [],
            "before_hash": cs.before_hash,
            "expires_at": cs.expires_at.isoformat() if cs.expires_at
            else None,
        })
    summary = ""
    try:
        summary = str((run.candidate or {}).get("summary") or "")
    except Exception:
        summary = ""
    return JsonResponse({
        "id": run.pk,
        "status": run.status,
        "stage": run.stage,
        "stage_events": run.stage_events or [],
        "error_code": run.error_code,
        "error_message_safe": run.error_message_safe,
        "sources": sources,
        "omissions": (manifest.get("omissions") or [])[:20],
        "policy_version": run.policy_version,
        "policy_sha256": run.policy_sha256,
        "summary": summary[:2000],
        "critic_findings": (run.critic_findings or [])[:50],
        "calls": [{"kind": c.kind, "status": c.status,
                   "model": c.model} for c in calls],
        "usage_summary": run.usage_summary or {},
        "budgets": run.budgets or {},
        "previews": preview_data,
    })


@login_required
@require_http_methods(["POST"])
def agent_cancel(request, run_id):
    farm = _farm_or_404(request)
    run = _run_or_404(farm, request.user, run_id)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    if run.initiated_by_id != request.user.pk:
        raise Http404()
    at = timezone.now()
    AgentRun.objects.filter(
        pk=run.pk, farm=farm, status__in=("queued", "running"),
        deleted_at__isnull=True, cancelled_at__isnull=True,
    ).update(status="cancelled", cancelled_at=at, completed_at=at,
             error_code="cancelled",
             error_message_safe="A futást a kérő megszakította.",
             updated_at=at)
    return redirect("agent_detail", run_id=run.pk)


@login_required
@require_http_methods(["POST"])
def agent_delete(request, run_id):
    farm = _farm_or_404(request)
    run = _run_or_404(farm, request.user, run_id)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    if run.initiated_by_id != request.user.pk:
        raise Http404()
    at = timezone.now()
    # Technical scrub via queryset UPDATE (bypasses model immutability):
    # question/answer/sources/context/candidate and unapplied changesets
    # are removed; minimal audit/cost rows stay. Late provider callbacks
    # cannot resurrect: worker guards check deleted_at/status first.
    from django.db import transaction
    with transaction.atomic():
        AgentRun.objects.filter(pk=run.pk, farm=farm).update(
            status="deleted", deleted_at=at, completed_at=at, goal="",
            context_manifest={}, candidate=None, critic_findings=[],
            policy_cited_sections=[], input_snapshot={},
            error_message_safe=None, updated_at=at)
        AgentChangeset.objects.filter(
            run_id=run.pk, state="preview").update(
            state="rejected", updated_at=at)
        AgentChangeset.objects.filter(
            run_id=run.pk, state__in=("preview", "rejected")).update(
            commands=[], before_snapshot={}, after_snapshot={},
            source_vector=[], receipt={}, updated_at=at)
        AgentDocGrant.objects.filter(run_id=run.pk).delete()
    return redirect("agent_home")


@login_required
@require_http_methods(["POST"])
def agent_changeset_approve(request, run_id, csid):
    from portal.agent import changeset as cs_mod

    farm = _farm_or_404(request)
    run = _run_or_404(farm, request.user, run_id)
    if not AgentChangeset.objects.filter(
            pk=csid, run=run, actor=request.user,
            farm=farm).exists():
        raise Http404()
    token = str(request.POST.get("token") or "")
    payload_hash = str(request.POST.get("payload_hash") or "")
    if not token or not payload_hash:
        messages.error(request, _("Hiányzó jóváhagyási token."))
        return redirect("agent_detail", run_id=run.pk)
    try:
        receipt = cs_mod.approve_changeset(
            request.user, farm, int(csid), token, payload_hash)
    except PermissionDenied:
        messages.error(request, _("A jóváhagyás megtagadva."))
        return redirect("agent_detail", run_id=run.pk)
    except Exception:
        messages.error(request, _("A jóváhagyás sikertelen vagy lejárt."))
        return redirect("agent_detail", run_id=run.pk)
    if receipt.get("verified"):
        fresh = AgentRun.objects.filter(pk=run.pk).first()
        if fresh is not None:
            events = list(fresh.stage_events or [])
            now_iso = timezone.now().isoformat()
            events.append({"stage": 6, "code": "apply", "at": now_iso})
            events.append({"stage": 7, "code": "independent",
                           "at": now_iso})
            AgentRun.objects.filter(
                pk=run.pk, status="preview").exclude(
                status__in=("deleted", "cancelled")).update(
                status="done", stage=7, stage_events=events[-50:],
                completed_at=timezone.now(), updated_at=timezone.now())
    messages.success(request, _("A változáscsomag alkalmazva."))
    return redirect("agent_detail", run_id=run.pk)


@login_required
@require_http_methods(["POST"])
def agent_changeset_reject(request, run_id, csid):
    from portal.agent import changeset as cs_mod

    farm = _farm_or_404(request)
    run = _run_or_404(farm, request.user, run_id)
    if not AgentChangeset.objects.filter(
            pk=csid, run=run, actor=request.user,
            farm=farm).exists():
        raise Http404()
    try:
        cs_mod.reject_changeset(request.user, farm, int(csid))
    except PermissionDenied:
        messages.error(request, _("A visszautasítás megtagadva."))
        return redirect("agent_detail", run_id=run.pk)
    except Exception:
        messages.error(request, _("A visszautasítás sikertelen."))
        return redirect("agent_detail", run_id=run.pk)
    messages.success(request, _("A változáscsomag visszautasítva."))
    return redirect("agent_detail", run_id=run.pk)
