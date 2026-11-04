import json
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST, require_http_methods
from .forms import RegisterForm, LoginForm, FarmForm, ActionForm, FarmExtraFormSet
from django.core.exceptions import PermissionDenied, ValidationError
from .permissions import get_active_farm, role_for
from .models import Farm, FarmExtraField, AdviceRun, AdviceStep, FarmAction, ConsentEvent, TermsAcceptance, FarmProfileSnapshot, current_consent_granted
from .services import AdviceRejected, new_idempotency_key, update_farm_profile, save_farm_profile_with_extras, create_farm_with_extras, _ordered_extras_list, _reserve_advice_run
from .login_throttle import is_login_throttled, record_login_failure, clear_login_failures

User = get_user_model()

def healthz(request):
    return JsonResponse({"status": "ok"})

def set_language_view(request):
    """Delegate to Django's standard set_language, but only for LANGUAGES.

    Django's check_for_language accepts any shipped catalog (e.g. fr);
    this portal only supports hu/en, so other codes get a safe same-site
    redirect with no language cookie set (no open redirect either).
    """
    from django.views.i18n import set_language as _dj_set_language
    from django.http import HttpResponseRedirect
    from django.utils.http import url_has_allowed_host_and_scheme
    if request.method == "POST":
        lang = (request.POST.get("language") or "").lower()
        if lang not in dict(settings.LANGUAGES):
            nxt = request.POST.get("next") or "/"
            if not url_has_allowed_host_and_scheme(url=nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                nxt = "/"
            return HttpResponseRedirect(nxt)
    return _dj_set_language(request)

def home(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return redirect("login")

def info_view(request):
    return render(request, "portal/info.html")

def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = LoginForm(request.POST or None)
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        ip = request.META.get("REMOTE_ADDR", "?")
        # Shared SQLite throttle (all workers), not the per-process cache.
        if is_login_throttled(ip, email):
            messages.error(request, _("Túl sok sikertelen próbálkozás. Kérjük, várjon 15 percet."))
            return render(request, "portal/login.html", {"form": form}, status=429)
        if form.is_valid():
            user = authenticate(request, username=email, password=form.cleaned_data["password"])
            if user is not None and user.is_active:
                clear_login_failures(ip, email)
                login(request, user)
                return redirect("dashboard")
            record_login_failure(ip, email)
            messages.error(request, _("Hibás e-mail cím vagy jelszó."))
        else:
            messages.error(request, _("Kérjük, ellenőrizze a megadott adatokat."))
    return render(request, "portal/login.html", {"form": form})

def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(email=form.cleaned_data["email"], password=form.cleaned_data["password"])
            TermsAcceptance.objects.get_or_create(user=user, terms_version=settings.TERMS_VERSION, privacy_notice_version=settings.PRIVACY_VERSION)
            farm = Farm.objects.create(owner=user, name=_("Saját gazdaságom"))
            if form.cleaned_data.get("cloud_consent"):
                ConsentEvent.objects.create(farm=farm, actor=user, event_type="granted", policy_version=settings.CLOUD_POLICY_VERSION)
        login(request, user)
        messages.success(request, _("Üdvözöljük! Először töltse ki gazdasága profilját."))
        return redirect("farm")
    return render(request, "portal/register.html", {"form": form})

@login_required
@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")

def _active_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm

@login_required
@require_POST
def select_active_farm(request):
    raw = (request.POST.get("farm_id") or "").strip()
    try:
        farm = Farm.objects.get(pk=int(raw))
    except (Farm.DoesNotExist, ValueError, TypeError):
        raise Http404()
    if role_for(request.user, farm) is None:
        raise Http404()
    request.session["active_farm_id"] = farm.id
    return redirect("dashboard")

def get_own_farm(user):
    try:
        return Farm.objects.get(owner=user)
    except Farm.DoesNotExist:
        raise Http404()

@login_required
def dashboard(request):
    farm = get_active_farm(request)
    last = None
    open_actions = done_actions = []
    missing = []
    granted = False
    if farm:
        last = AdviceRun.objects.filter(farm=farm, status="success").prefetch_related("steps").order_by("-attempt_started_at").first()
        open_actions = FarmAction.objects.filter(farm=farm, status="open")[:20]
        done_actions = FarmAction.objects.filter(farm=farm, status="done")[:10]
        missing = farm.completeness_missing()
        granted = current_consent_granted(farm)
    last_prov = None
    if last is not None:
        from .provenance import advice_provenance
        last_prov = advice_provenance(last)
    next_task = open_actions[0] if len(open_actions) else None
    weather_status = None
    if farm is not None:
        try:
            from .domain_models import FarmLocation as _FL
            from .weather import get_forecast_cached as _gfc
            _locs = list(_FL.objects.filter(farm=farm).order_by("name")[:5])
            if not _locs:
                weather_status = str(_("Időjárás: nincs helyszín, gyorsítótárazott adat nincs."))
            else:
                _parts = []
                for _lo in _locs:
                    _fc, _fs = _gfc(farm.id, _lo)
                    if _fc is not None:
                        _parts.append("%s: %s" % (_lo.name, {"fresh": _("friss"), "stale": _("elavult"), "missing": _("nincs adat")}.get(_fs, _("nincs adat"))))
                    else:
                        _parts.append("%s: %s" % (_lo.name, _("nincs gyorsítótárazott adat")))
                weather_status = str(_("Időjárás gyorsítótárból: ")) + "; ".join(_parts)
        except Exception:
            weather_status = None
    return render(request, "portal/dashboard.html", {"farm": farm, "last_advice": last, "last_prov": last_prov, "next_task": next_task, "weather_status": weather_status, "open_actions": open_actions, "done_actions": done_actions, "missing": missing, "missing_labels": farm.completeness_missing_labels() if farm else [], "consent_granted": granted})

@login_required
def farm_view(request):
    from django.db import IntegrityError as _IE

    def _posted_extra_ids(post, own_ids):
        bad = []
        for k, v in post.items():
            if k.startswith("extras-") and k.endswith("-id") and str(v or "").strip():
                try:
                    pid = int(str(v).strip())
                except (ValueError, TypeError):
                    bad.append(v)
                    continue
                if pid not in own_ids:
                    bad.append(pid)
        return bad

    farm = get_active_farm(request)
    if request.method == "POST":
        if farm is not None and role_for(request.user, farm) not in ("owner", "manager"):
            raise PermissionDenied()
        if farm is None:
            form = FarmForm(request.POST)
            has_extras = any(k.startswith("extras-") for k in request.POST)
            proto = Farm(owner=request.user, name=(request.POST.get("name") or "").strip() or "tmp")
            formset = FarmExtraFormSet(request.POST, prefix="extras", instance=proto, queryset=FarmExtraField.objects.none()) if has_extras else None
            form_ok = form.is_valid()
            fs_ok = formset.is_valid() if formset is not None else True
            if formset is not None:
                for f in formset.forms:
                    if getattr(getattr(f, "instance", None), "pk", None):
                        fs_ok = False
                        f.add_error(None, _("Érvénytelen mezőazonosító."))
                if not fs_ok:
                    messages.error(request, _("Érvénytelen mezőazonosító."))
            if form_ok and fs_ok:
                rows = []
                if formset is not None:
                    for f in formset.forms:
                        cd = getattr(f, "cleaned_data", None)
                        if not cd or cd.get("DELETE"):
                            continue
                        k = (cd.get("key") or "").strip()
                        v = (cd.get("value") or "").strip()
                        if k or v:
                            rows.append((k, v))
                try:
                    create_farm_with_extras(request.user, form.cleaned_data, rows)
                except (_IE, ValidationError):
                    if formset is None:
                        formset = FarmExtraFormSet(prefix="extras", instance=proto, queryset=FarmExtraField.objects.none())
                    messages.error(request, _("Érvénytelen mezőazonosító."))
                    return render(request, "portal/farm.html", {"form": form, "farm": None, "extra_formset": formset})
                messages.success(request, _("Gazdaság létrehozva. Most kitöltheti a részleteket."))
                return redirect("farm")
            return render(request, "portal/farm.html", {"form": form, "farm": None, "extra_formset": formset})
        has_extras = any(k.startswith("extras-") for k in request.POST)
        if not has_extras:
            # Legacy path: existing callers POST core fields only.
            form = FarmForm(request.POST, instance=farm)
            if form.is_valid():
                # Re-fetch inside the transaction (Blocking1).
                update_farm_profile(farm, form.cleaned_data)
                messages.success(request, _("Profil mentve."))
                return redirect("farm")
            return render(request, "portal/farm.html", {"form": form, "farm": farm, "extra_formset": FarmExtraFormSet(prefix="extras", instance=farm)})
        form = FarmForm(request.POST, instance=farm)
        formset = FarmExtraFormSet(request.POST, prefix="extras", instance=farm)
        form_ok = form.is_valid()
        fs_ok = formset.is_valid()
        own_ids = set(FarmExtraField.objects.filter(farm=farm).values_list("pk", flat=True))
        if _posted_extra_ids(request.POST, own_ids):
            fs_ok = False
            messages.error(request, _("Érvénytelen mezőazonosító."))
        if form_ok and fs_ok:
            try:
                save_farm_profile_with_extras(farm, form.cleaned_data, formset)
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
                return render(request, "portal/farm.html", {"form": form, "farm": farm, "extra_formset": formset})
            messages.success(request, _("Profil mentve."))
            return redirect("farm")
        return render(request, "portal/farm.html", {"form": form, "farm": farm, "extra_formset": formset})
    else:
        form = FarmForm(instance=farm) if farm else FarmForm()
        if farm:
            extra_formset = FarmExtraFormSet(prefix="extras", instance=farm)
        else:
            _proto = Farm(owner=request.user, name="tmp")
            extra_formset = FarmExtraFormSet(prefix="extras", instance=_proto, queryset=FarmExtraField.objects.none())
    return render(request, "portal/farm.html", {"form": form, "farm": farm, "extra_formset": extra_formset})

@login_required
def advice_request_view(request):
    farm = _active_or_404(request)
    granted = current_consent_granted(farm)
    from datetime import timedelta
    from .quota import usage_counts
    hour_count, day_count = usage_counts(farm)
    if request.method == "POST":
        if role_for(request.user, farm) not in ("owner", "manager"):
            raise PermissionDenied()
        idem = (request.POST.get("idempotency_key") or "").strip() or new_idempotency_key()
        # Reserve-only POST: the request path only reserves (or replays) a
        # queued run and never calls the provider, never spawns a thread.
        # A dedicated worker process claims and executes queued runs.
        try:
            run, created = _reserve_advice_run(request.user, farm, idem)
        except AdviceRejected as e:
            if "application/json" in request.META.get("HTTP_ACCEPT", ""):
                _code = {"consent_required": 403, "quota_exceeded": 429,
                         "global_quota_exceeded": 429}.get(e.code, 400)
                return JsonResponse({"status": "failed", "phase": "error", "error": e.message}, status=_code)
            messages.error(request, e.message)
            return redirect("advice_request")
        if "application/json" in request.META.get("HTTP_ACCEPT", ""):
            from .services import advice_phase as _phase
            _detail = reverse("advice_detail", args=[run.id])
            _status = reverse("advice_status", args=[run.id])
            if run.status != "pending":
                from django.template.loader import render_to_string as _rts
                from .services import SAFE_MESSAGES as _SM
                from portal.models import FarmAction as _FA
                _saved = set(_FA.objects.filter(farm=farm, source_step__isnull=False).values_list("source_step_id", flat=True))
                _err = None if run.status == "success" else (run.error_message_safe or str(_SM.get(run.error_code or "", "")) or str(_SM.get("provider_error", "")))
                _html = _rts("portal/_advice_status.html", {"run": run, "phase": _phase(run), "effective_status": run.status, "error": _err, "detail_url": _detail, "saved_step_ids": _saved, "render_steps": True}, request=request)
                return JsonResponse({"status": run.status, "phase": _phase(run), "detail_url": _detail, "status_url": _status, "error": _err, "html": _html})
            return JsonResponse(
                {"status": "pending", "phase": _phase(run), "status_url": _status, "detail_url": _detail},
                status=202)
        return redirect("advice_detail", run_id=run.id)
    from .provenance import prospective_advice_provenance
    prov = prospective_advice_provenance(farm, timezone.now())
    quota_checked_at = timezone.now()
    # Reload recovery: expose this active farm's existing pending run so
    # the page can resume polling its server-provided status_url on load.
    # Scoped strictly to the active farm; nothing is stored client-side.
    from django.urls import reverse as _reverse
    from .services import advice_phase as _req_phase
    pending_run = AdviceRun.objects.filter(farm=farm, status="pending").order_by("attempt_started_at").first()
    pending_status_url = _reverse("advice_status", args=[pending_run.id]) if pending_run else ""
    pending_phase = _req_phase(pending_run) if pending_run else ""
    return render(request, "portal/advice_request.html", {"farm": farm, "consent_granted": granted, "hour_count": hour_count, "day_count": day_count, "idem_key": new_idempotency_key(), "max_hour": settings.ADVICE_MAX_PER_HOUR_PER_FARM, "max_day": settings.ADVICE_MAX_PER_DAY_PER_FARM, "quota_checked_at": quota_checked_at, "prov": prov, "pending_run": pending_run, "pending_status_url": pending_status_url, "pending_phase": pending_phase})

@login_required
@require_http_methods(["GET", "HEAD"])
def advice_status_view(request, run_id):
    """Read-only pending status endpoint; never writes to the DB."""
    from django.template.loader import render_to_string
    from django.urls import reverse
    from .services import SAFE_MESSAGES, advice_phase, is_pending_overdue
    farm = _active_or_404(request)
    run = get_object_or_404(AdviceRun, pk=run_id, farm=farm)
    detail_url = reverse("advice_detail", args=[run.id])
    status_url = reverse("advice_status", args=[run.id])
    # Read-only lookup for the polled fragment so success renders usable
    # save-action forms; the endpoint itself never writes to the DB.
    saved_step_ids = set(FarmAction.objects.filter(farm=farm, source_step__isnull=False).values_list("source_step_id", flat=True))
    if run.status == "pending" and is_pending_overdue(run):
        # Derived-overdue fallback label only (authorized orphan, provider
        # call in flight or lost): the DB row stays pending/running.
        # The fallback reports failure, so the phase is "error" even
        # though the stored run still derives "running".
        error = str(SAFE_MESSAGES.get("attempt_interrupted", ""))
        html = render_to_string("portal/_advice_status.html", {"run": run, "phase": "error", "effective_status": "failed", "error": error, "detail_url": detail_url, "saved_step_ids": saved_step_ids, "render_steps": True}, request=request)
        return JsonResponse({"status": "failed", "phase": "error", "error": error or str(SAFE_MESSAGES.get("provider_error", "")), "detail_url": detail_url, "status_url": status_url, "html": html})
    if run.status == "failed":
        error = run.error_message_safe or str(SAFE_MESSAGES.get(run.error_code or "", "")) or str(SAFE_MESSAGES.get("provider_error", ""))
        html = render_to_string("portal/_advice_status.html", {"run": run, "phase": advice_phase(run), "effective_status": "failed", "error": error, "detail_url": detail_url, "saved_step_ids": saved_step_ids, "render_steps": True}, request=request)
        return JsonResponse({"status": "failed", "phase": advice_phase(run), "error": error, "detail_url": detail_url, "status_url": status_url, "html": html})
    if run.status == "success":
        html = render_to_string("portal/_advice_status.html", {"run": run, "phase": advice_phase(run), "effective_status": "success", "error": None, "detail_url": detail_url, "saved_step_ids": saved_step_ids, "render_steps": True}, request=request)
        return JsonResponse({"status": "success", "phase": advice_phase(run), "error": None, "detail_url": detail_url, "status_url": status_url, "html": html})
    html = render_to_string("portal/_advice_status.html", {"run": run, "phase": advice_phase(run), "effective_status": "pending", "error": None, "detail_url": detail_url, "saved_step_ids": saved_step_ids, "render_steps": True}, request=request)
    return JsonResponse({"status": "pending", "phase": advice_phase(run), "error": None, "detail_url": detail_url, "status_url": status_url, "html": html})


@login_required
def advice_history(request):
    farm = _active_or_404(request)
    runs = list(AdviceRun.objects.filter(farm=farm).order_by("-attempt_started_at")[:5])
    from .provenance import advice_provenance
    for r in runs:
        r.prov = advice_provenance(r)
    return render(request, "portal/advice_history.html", {"farm": farm, "runs": runs})

@login_required
def advice_detail(request, run_id):
    from django.urls import reverse
    from .services import SAFE_MESSAGES, advice_phase, is_pending_overdue
    farm = _active_or_404(request)
    run = get_object_or_404(AdviceRun, pk=run_id, farm=farm)
    saved_step_ids = set(FarmAction.objects.filter(farm=farm, source_step__isnull=False).values_list("source_step_id", flat=True))
    from .provenance import advice_provenance
    prov = advice_provenance(run)
    # An overdue authorized run is shown with the same stale fallback as
    # the status endpoint: phase=error / effective failed, DB untouched.
    if run.status == "pending" and is_pending_overdue(run):
        phase = "error"
        effective_status = "failed"
        detail_error = str(SAFE_MESSAGES.get("attempt_interrupted", ""))
    else:
        phase = advice_phase(run)
        effective_status = run.status
        detail_error = run.error_message_safe
    return render(request, "portal/advice_detail.html", {"run": run, "farm": farm, "saved_step_ids": saved_step_ids, "prov": prov, "phase": phase, "effective_status": effective_status, "detail_error": detail_error, "status_url": reverse("advice_status", args=[run.id]), "detail_url": reverse("advice_detail", args=[run.id])})

@login_required
def actions_list(request):
    farm = _active_or_404(request)
    open_a = FarmAction.objects.filter(farm=farm, status="open")
    done_a = FarmAction.objects.filter(farm=farm, status="done")
    return render(request, "portal/actions.html", {"farm": farm, "open_actions": open_a, "done_actions": done_a, "form": ActionForm()})

@login_required
@require_POST
def action_create(request):
    farm = _active_or_404(request)
    if request.method == "POST" and role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    form = ActionForm(request.POST)
    if form.is_valid():
        FarmAction.objects.create(farm=farm, text=form.cleaned_data["text"], status="open")
        messages.success(request, _("Teendő mentve."))
    else:
        messages.error(request, _("A teendő szövege érvénytelen (1–2000 karakter)."))
    return redirect("actions")

@login_required
def action_edit(request, action_id):
    farm = _active_or_404(request)
    if request.method == "POST" and role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    action = get_object_or_404(FarmAction, pk=action_id, farm=farm)
    if request.method == "POST":
        form = ActionForm(request.POST, instance=action)
        if form.is_valid():
            form.save()
            messages.success(request, _("Teendő frissítve."))
            return redirect("actions")
    else:
        form = ActionForm(instance=action)
    return render(request, "portal/action_edit.html", {"form": form, "action": action})

@login_required
@require_POST
def action_toggle(request, action_id):
    farm = _active_or_404(request)
    if request.method == "POST" and role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    action = get_object_or_404(FarmAction, pk=action_id, farm=farm)
    if action.status == "open":
        action.status = "done"; action.done_at = timezone.now()
    else:
        action.status = "open"; action.done_at = None
    action.save()
    return redirect("actions")

@login_required
@require_http_methods(["GET","POST"])
def action_delete(request, action_id):
    farm = _active_or_404(request)
    if request.method == "POST" and role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    action = get_object_or_404(FarmAction, pk=action_id, farm=farm)
    if request.method == "POST":
        action.delete()
        messages.success(request, _("Teendő törölve."))
        return redirect("actions")
    return render(request, "portal/action_delete.html", {"action": action})

@login_required
@require_POST
def action_from_step(request, run_id, step_id):
    farm = _active_or_404(request)
    if request.method == "POST" and role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    run = get_object_or_404(AdviceRun, pk=run_id, farm=farm)
    step = get_object_or_404(AdviceStep, pk=step_id, advice_run=run, farm=farm)
    FarmAction.objects.create(farm=farm, source_step=step, source_step_farm_id=farm.id, text=step.action_text, status="open")
    messages.success(request, _("Teendő mentve a tanácslépésből."))
    return redirect("actions")

@login_required
def account_view(request):
    farm = Farm.objects.filter(owner=request.user).first()
    granted = current_consent_granted(farm) if farm else False
    last_event = ConsentEvent.objects.filter(farm=farm).first() if farm else None
    terms = TermsAcceptance.objects.filter(user=request.user)
    return render(request, "portal/account.html", {"farm": farm, "consent_granted": granted, "last_event": last_event, "terms": terms})

@login_required
def export_info_view(request):
    return render(request, "portal/export_info.html")


@login_required
@require_POST
def consent_view(request):
    farm = get_own_farm(request.user)
    if farm.owner_id != request.user.id:
        raise PermissionDenied()
    action = request.POST.get("consent_action")
    if action == "grant":
        ConsentEvent.objects.create(farm=farm, actor=request.user, event_type="granted", policy_version=settings.CLOUD_POLICY_VERSION)
        messages.success(request, _("Felhőadat-kezelési hozzájárulás megadva."))
    elif action == "revoke":
        ConsentEvent.objects.create(farm=farm, actor=request.user, event_type="revoked", policy_version=settings.CLOUD_POLICY_VERSION)
        messages.success(request, _("Felhőadat-kezelési hozzájárulás visszavonva. Új tanács nem kérhető, korábbi adatai megmaradnak."))
    return redirect("account")

@login_required
def export_view(request):
    farm = Farm.objects.filter(owner=request.user).first()
    data = {"exported_at": timezone.now().isoformat(), "account_email": request.user.email, "farm": None, "snapshots": [], "advice_runs": [], "actions": [], "consent_events": [], "locations": [], "properties": [], "equipment": [], "equipment_assignments": [], "crop_cycles": [], "terms_acceptances": [{"terms_version": t.terms_version, "privacy_notice_version": t.privacy_notice_version, "accepted_at": t.accepted_at.isoformat()} for t in TermsAcceptance.objects.filter(user=request.user)]}
    if farm:
        data["farm"] = {**farm.profile_dict(), "profile_version": farm.profile_version}
        data["extras"] = _ordered_extras_list(farm)
        data["snapshots"] = [{"profile_version": s.profile_version, "profile": s.profile_json, "created_at": s.created_at.isoformat()} for s in FarmProfileSnapshot.objects.filter(farm=farm)]
        runs = []
        for r in AdviceRun.objects.filter(farm=farm).prefetch_related("steps"):
            runs.append({"id": r.id, "status": r.status, "source": r.source, "requested_model": r.requested_model, "actual_model": r.actual_model, "profile_version": r.profile_snapshot.profile_version, "language": r.language, "is_general": r.result_is_general, "assumptions": r.result_assumptions, "focused_questions": r.result_focused_questions, "steps": [{"position": s.position, "title": s.title, "action_text": s.action_text, "rationale": s.rationale, "risk_note": s.risk_note} for s in r.steps.all()], "attempt_started_at": r.attempt_started_at.isoformat()})
        data["advice_runs"] = runs
        data["actions"] = [{"text": a.text, "status": a.status, "created_at": a.created_at.isoformat()} for a in FarmAction.objects.filter(farm=farm)]
        data["consent_events"] = [{"event_type": e.event_type, "policy_version": e.policy_version, "occurred_at": e.occurred_at.isoformat()} for e in ConsentEvent.objects.filter(farm=farm)]
        from .models import FarmLocation as _FL, FarmProperty as _FP, Equipment as _EQ, EquipmentProperty as _EP, CropCycle as _CC
        data["locations"] = [{"name": x.name, "latitude": str(x.latitude), "longitude": str(x.longitude), "timezone": x.timezone} for x in _FL.objects.filter(farm=farm)]
        _loc = {x.id: x.name for x in _FL.objects.filter(farm=farm)}
        data["properties"] = [{"name": x.name, "kind": x.kind, "area_hectares": str(x.area_hectares), "location": _loc.get(x.location_id), "notes": x.notes, "boundary_geojson": x.boundary_geojson, "point_latitude": str(x.point_latitude) if x.point_latitude is not None else None, "point_longitude": str(x.point_longitude) if x.point_longitude is not None else None, "map_area_hectares_estimate": x.map_area_hectares} for x in _FP.objects.filter(farm=farm)]
        _prop = {x.id: x.name for x in _FP.objects.filter(farm=farm)}
        _eqs = list(_EQ.objects.filter(farm=farm).prefetch_related("properties"))
        data["equipment"] = [{"name": x.name, "capabilities": list(x.capabilities or []), "available": x.available, "hours_per_day": str(x.hours_per_day), "assigned_properties": sorted(p.name for p in x.properties.all())} for x in _eqs]
        data["equipment_assignments"] = [{"equipment": l.equipment.name, "property": l.property.name} for l in _EP.objects.filter(equipment__farm=farm, property__farm=farm).select_related("equipment", "property")]
        # Export keeps all cycles including hidden flags; default manager stays unfiltered.
        data["crop_cycles"] = [{"property": c.property.name, "crop_name": c.crop_name, "state": c.state, "growth_stage": c.growth_stage, "sow_date": c.sow_date.isoformat() if c.sow_date else None, "expected_end_date": c.expected_end_date.isoformat() if c.expected_end_date else None, "observations": c.observations, "is_hidden": c.is_hidden, "hidden_reason": c.hidden_reason} for c in _CC.objects.filter(farm=farm).select_related("property")]
        from .models import FarmingEvent as _FE
        data["farming_events"] = [{"property": e.property.name, "crop_cycle": (e.crop_cycle.crop_name if e.crop_cycle_id else None) or (e.crop_name_snapshot or None), "crop_name_snapshot": e.crop_name_snapshot, "event_date": e.event_date.isoformat(), "event_type": e.event_type, "description": e.description, "quantity": str(e.quantity) if e.quantity is not None else None, "unit": e.unit, "materials": e.materials, "treated_area_hectares": str(e.treated_area_hectares) if e.treated_area_hectares is not None else None, "notes": e.notes, "created_by": e.created_by_id, "updated_by": e.updated_by_id, "created_at": e.created_at.isoformat() if e.created_at else None, "updated_at": e.updated_at.isoformat() if e.updated_at else None} for e in _FE.objects.filter(farm=farm).select_related("property", "crop_cycle").order_by("event_date", "id")]
        from .models import WorkItem as _WI
        data["work_items"] = [{"property": w.property.name, "crop_cycle": w.crop_cycle.crop_name, "title": w.title, "capability": w.capability, "start_date": w.start_date.isoformat(), "end_date": w.end_date.isoformat(), "estimated_hours": str(w.estimated_hours), "equipment": w.equipment.name if w.equipment_id else None, "missing": w.missing, "recommendation": w.recommendation, "guidance": w.guidance} for w in _WI.objects.filter(farm=farm).select_related("property", "crop_cycle", "equipment")]
    resp = HttpResponse(json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="sajat-adataim.json"'
    return resp

@login_required
@require_http_methods(["GET","POST"])
def account_delete_view(request):
    if request.method == "POST":
        confirm = (request.POST.get("confirm_text") or "").strip()
        if confirm != "TORLES":
            messages.error(request, _('A törléshez írja be pontosan: TORLES'))
            return render(request, "portal/account_delete.html")
        user = request.user
        logout(request)
        # Explicit session invalidation: deleting the user makes existing sessions unauthenticated;
        # also remove session rows encoding this user id where decodable.
        try:
            from django.contrib.sessions.models import Session
            for s in Session.objects.filter(expire_date__gte=timezone.now()):
                try:
                    if str(s.get_decoded().get("_auth_user_id")) == str(user.pk):
                        s.delete()
                except Exception:
                    pass
        except Exception:
            pass
        user.delete()
        return redirect("login")
    return render(request, "portal/account_delete.html")
