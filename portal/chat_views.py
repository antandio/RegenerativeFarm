"""Stage5 chat views: GET history/context (no provider call), POST send, POST confirm."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from .chat import (NAV_LINKS, ChatRejected, build_chat_context, confirm_proposal,
                   enqueue_chat, new_chat_idempotency_key, request_chat,
                   validate_selected_property_ids, MAX_SELECTED_PROPERTY_IDS)
from .chat_models import ChatAttempt, ChatProposal
from .models import Farm
from .permissions import get_active_farm, role_for
from .quota import usage_counts


def get_chat_client():
    from .chat import MetaChatClient
    return MetaChatClient()


def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm


@login_required
@require_http_methods(["GET", "POST"])
def chat_view(request):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise PermissionDenied()
    if request.method == "POST":
        # Plans enrichment prefill: explicit POST, stored server-side, NO GET API.
        if "prefill" in request.POST and "cost_consent" not in request.POST:
            if role not in ("owner", "manager", "worker"):
                raise PermissionDenied()
            prefill = str(request.POST.get("prefill") or "")[:2000]
            request.session["chat_prefill"] = prefill
            return redirect("chat")
        # Send: explicit cost consent required; viewer read-only.
        if role not in ("owner", "manager", "worker"):
            raise PermissionDenied()
        if (request.POST.get("cost_consent") or "") != "on":
            messages.error(request, _("A küldéshez kifejezett költség- és hozzájárulási megerősítés szükséges."))
            return redirect("chat")
        from .models import current_consent_granted
        if not current_consent_granted(Farm.objects.get(pk=farm.pk)):
            messages.error(request, _("Csevegéshez felhőadat-kezelési hozzájárulás szükséges."))
            return redirect("chat")
        raw_selected = request.POST.getlist("selected_property_ids")
        try:
            selected = validate_selected_property_ids(Farm.objects.get(pk=farm.pk), raw_selected)
        except ChatRejected:
            own_ids = []
            try:
                from .domain_models import FarmProperty as _FP
                _parsed = []
                for _v in raw_selected[:MAX_SELECTED_PROPERTY_IDS]:
                    try:
                        _pid = int(str(_v).strip())
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if _pid > 0:
                        _parsed.append(_pid)
                if _parsed:
                    own_ids = sorted(set(_FP.objects.filter(
                        farm_id=farm.pk, id__in=_parsed).values_list("id", flat=True)))
            except Exception:
                own_ids = []
            return _render_chat_get(request, farm, role,
                                    prefill=str(request.POST.get("message") or "")[:2000],
                                    selected_ids=own_ids, status=400)
        message = str(request.POST.get("message") or "")
        idem = (request.POST.get("idempotency_key") or "").strip() or new_chat_idempotency_key()
        language = request.POST.get("language") or "hu"
        if language not in ("hu", "en"):
            language = "hu"
        try:
            run = request_chat(request.user, farm, message, idem, language=language,
                               client=get_chat_client(), context_str=None,
                               selected_property_ids=selected)
        except ChatRejected as e:
            if e.code == "forbidden":
                raise PermissionDenied()
            messages.error(request, e.message)
            if e.code == "invalid_input":
                return _render_chat_get(request, farm, role,
                                        prefill=message[:2000],
                                        selected_ids=selected, status=200)
            return redirect("chat")
        if run.status == "success":
            messages.success(request, _("Válasz megérkezett. Az írási javaslatok jóváhagyásra várnak."))
        else:
            messages.error(request, run.error_message_safe or _("A válasz nem készült el."))
        return redirect("chat")
    # GET: no provider call, no writes. Context + fixed links + history.
    prefill = request.session.pop("chat_prefill", "")
    return _render_chat_get(request, farm, role, prefill=prefill)


def _render_chat_get(request, farm, role, prefill="", selected_ids=None, status=200):
    from portal.chat_models import ChatAttempt as _A, ChatProposal as _P
    attempts = list(_A.objects.filter(
        farm=farm, initiated_by=request.user,
        deleted_at__isnull=True).order_by("-attempt_started_at")[:5])
    attempt_ids = [a.id for a in attempts]
    proposals = list(_P.objects.filter(attempt_id__in=attempt_ids).order_by("attempt_id", "position")) if attempt_ids else []
    by_attempt = {}
    for p in proposals:
        by_attempt.setdefault(p.attempt_id, []).append(p)
    sel = list(selected_ids) if selected_ids else []
    try:
        _ctx, ctx_str = build_chat_context(Farm.objects.get(pk=farm.pk), selected_property_ids=sel or None)
    except Exception:
        _ctx, ctx_str = build_chat_context(Farm.objects.get(pk=farm.pk))
        sel = []
    from .domain_models import FarmProperty as _FP
    parcel_props = list(_FP.objects.filter(farm_id=farm.pk).order_by("name"))
    selected_set = set(sel)
    for _a in attempts:
        _snap = getattr(_a, "selection_snapshot", None) or {}
        if isinstance(_snap, dict) and _snap.get("mode") == "subset":
            _names = [str(_p.get("name")) for _p in (_snap.get("properties") or []) if isinstance(_p, dict) and _p.get("name")]
            _a.scope_names = _names
            _a.scope_mode = "subset"
        else:
            _a.scope_names = []
            _a.scope_mode = "all"
    hour_count, day_count = usage_counts(farm)
    from django.conf import settings as _s
    role_labels = {"owner": _("Tulajdonos"), "manager": _("Menedzser"),
                   "worker": _("Munkatárs"), "viewer": _("Megfigyelő")}
    from django.utils import timezone as _tz
    from .provenance import chat_provenance as _chat_prov
    from .chat import CHAT_NAV_LABELS as _NL, CHAT_SAFE_DESTS as _SD
    for _a in attempts:
        _a.prov = _chat_prov(_a)
    for _pr in proposals:
        if _pr.kind == "navigate" and _pr.payload_link:
            try:
                _raw = reverse(_pr.payload_link)
            except Exception:
                from .chat import NAV_LINKS as _NLL
                _raw = _NLL.get(_pr.payload_link, "#")
            _pr.nav_human = str(_NL.get(_pr.payload_link, _pr.payload_link))
            _pr.nav_dest = _SD.get(_pr.payload_link, _raw)
        elif _pr.kind == "plans_generate":
            _pr.nav_human = str(_("Tervkészítés hatása: munkaterv-tervezet"))
            _pr.nav_dest = reverse("plans")
    ctx = {"farm": farm, "role": role, "role_label": role_labels.get(role, role),
           "prefill": prefill, "attempts": attempts,
           "proposals": proposals, "by_attempt": by_attempt,
           "parcel_props": parcel_props, "selected_ids": sel, "selected_set": selected_set,
           "context_chars": len(ctx_str), "idem_key": new_chat_idempotency_key(),
           "hour_count": hour_count, "day_count": day_count,
           "max_hour": _s.ADVICE_MAX_PER_HOUR_PER_FARM, "max_day": _s.ADVICE_MAX_PER_DAY_PER_FARM,
           "quota_checked_at": _tz.now()}
    return render(request, "portal/chat.html", ctx, status=status)


@login_required
@require_http_methods(["POST"])
def proposal_confirm_view(request, proposal_id):
    farm = _farm_or_404(request)
    if role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    try:
        prop = ChatProposal.objects.select_related("attempt").get(
            pk=proposal_id, farm=farm)
    except ChatProposal.DoesNotExist:
        raise Http404()
    if prop.attempt.initiated_by_id != request.user.pk:
        raise Http404()
    try:
        confirm_proposal(request.user, farm, proposal_id)
    except ChatRejected as e:
        if e.code == "forbidden":
            raise PermissionDenied()
        if e.code == "invalid_input":
            # Same-farm cross-user attempts must not be disclosed.
            try:
                att = ChatAttempt.objects.filter(pk=prop.attempt_id).first()
                if att is not None and att.initiated_by_id != request.user.pk:
                    raise Http404()
            except Http404:
                raise
            except Exception:
                pass
        messages.error(request, e.message)
        return redirect("chat")
    messages.success(request, _("Javaslat végrehajtva."))
    return redirect("chat")


def _api_status_of(run):
    if run.status == "success":
        return "done"
    if run.status == "pending":
        return "queued"
    return run.status


@login_required
@require_http_methods(["POST"])
def chat_enqueue_view(request):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    if role not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    if (request.POST.get("cost_consent") or "") != "on":
        return JsonResponse({"error": "cost_consent_required"}, status=400)
    from .models import current_consent_granted
    if not current_consent_granted(Farm.objects.get(pk=farm.pk)):
        return JsonResponse({"error": "consent_required"}, status=403)
    message = str(request.POST.get("message") or "")
    idem = (request.POST.get("idempotency_key") or "").strip()
    if not idem:
        return JsonResponse({"error": "invalid_input"}, status=400)
    language = request.POST.get("language") or "hu"
    if language not in ("hu", "en"):
        language = "hu"
    try:
        run = enqueue_chat(request.user, Farm.objects.get(pk=farm.pk),
                           message, idem, language=language)
    except ChatRejected as e:
        if e.code == "forbidden":
            raise PermissionDenied()
        if e.code in ("consent_required", "consent_withdrawn_before_call",
                      "quota_exceeded", "global_quota_exceeded"):
            return JsonResponse({"error": e.code}, status=403)
        return JsonResponse({"error": e.code}, status=400)
    return JsonResponse({"id": run.pk, "status": _api_status_of(run)}, status=202)


@login_required
@require_http_methods(["GET"])
def chat_status_view(request, attempt_id):
    farm = _farm_or_404(request)
    if role_for(request.user, farm) is None:
        raise Http404()
    try:
        run = ChatAttempt.objects.get(pk=attempt_id, farm_id=farm.pk,
                                      initiated_by=request.user,
                                      deleted_at__isnull=True)
    except ChatAttempt.DoesNotExist:
        raise Http404()
    return JsonResponse({"id": run.pk, "status": _api_status_of(run),
                         "answer": run.answer_text,
                         "error_code": run.error_code})


@login_required
@require_http_methods(["GET"])
def chat_history_view(request):
    farm = _farm_or_404(request)
    if role_for(request.user, farm) is None:
        raise Http404()
    try:
        page = int(request.GET.get("page") or 1)
    except (ValueError, TypeError):
        page = 1
    try:
        page_size = int(request.GET.get("page_size") or 10)
    except (ValueError, TypeError):
        page_size = 10
    page = max(page, 1)
    page_size = max(min(page_size, 20), 1)
    qs = ChatAttempt.objects.filter(
        farm_id=farm.pk, initiated_by=request.user,
        deleted_at__isnull=True).order_by("-attempt_started_at")
    total = qs.count()
    offset = (page - 1) * page_size
    rows = list(qs[offset:offset + page_size])
    results = [{"id": r.pk, "message": r.user_message or "",
                "answer": r.answer_text,
                "status": _api_status_of(r),
                "language": r.language,
                "attempt_started_at": (r.attempt_started_at.isoformat()
                                       if r.attempt_started_at else "")}
               for r in rows]
    return JsonResponse({"total": total, "page": page,
                         "page_size": page_size, "results": results})


@login_required
@require_http_methods(["POST"])
def chat_attempt_delete_view(request, attempt_id):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    if role not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    try:
        run = ChatAttempt.objects.get(pk=attempt_id, farm_id=farm.pk,
                                      initiated_by=request.user)
    except ChatAttempt.DoesNotExist:
        raise Http404()
    with transaction.atomic():
        locked = ChatAttempt.objects.select_for_update().get(pk=run.pk)
        if locked.initiated_by_id != request.user.pk or locked.farm_id != farm.pk:
            raise Http404()
        ChatProposal.objects.filter(attempt_id=locked.pk).delete()
        ChatAttempt.objects.filter(pk=locked.pk).update(
            selection_snapshot={}, user_message="", answer_text=None,
            deleted_at=timezone.now())
    return redirect("chat")
