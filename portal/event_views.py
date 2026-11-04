"""Stage A farming-event journal (actuals, not plans)."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .domain_forms import FarmingEventForm
from .domain_models import EVENT_TYPE_CHOICES, EVENT_TYPE_VALUES
from .models import AuditEvent, CropCycle, FarmingEvent, FarmProperty
from .permissions import accessible_farms, get_active_farm, role_for

WRITE_ROLES = ("owner", "manager")
EDIT_ROLES = ("owner", "manager", "worker")
PAGE_SIZE = 20

# Task3: server-side sort allowlist for the journal table. Keys are the only
# accepted `sort` GET values; anything else falls back to chronological
# default. Never order_by() raw user input.
SORT_FIELDS = {
    "date": "event_date",
    "type": "event_type",
    "property": "property__name",
    "title": "description",
}
SORT_DIRS = ("asc", "desc")


def _audit(farm, actor, action, obj):
    return AuditEvent.objects.create(
        farm=farm, actor=actor, action=action, object_type="event",
        object_id=str(obj.pk),
        meta={"event_id": obj.pk, "event_type": obj.event_type, "property_id": obj.property_id},
    )


def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm


def _ctx_farms(request, farm):
    try:
        farms = accessible_farms(request.user)
    except Exception:
        farms = [farm] if farm else []
    return {"farm": farm, "farms": farms, "active_farm": farm}


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@login_required
def event_list(request):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    qs = FarmingEvent.objects.filter(farm=farm).select_related("property", "crop_cycle").order_by("event_date", "id")
    f_year = (request.GET.get("year") or "").strip()
    f_from = (request.GET.get("date_from") or "").strip()
    f_to = (request.GET.get("date_to") or "").strip()
    f_prop = (request.GET.get("property") or "").strip()
    f_type = (request.GET.get("type") or "").strip()
    if f_year:
        try:
            y = int(f_year)
        except (TypeError, ValueError):
            qs = qs.none()
            y = None
        if y is None:
            pass
        elif not 1 <= y <= 9999:
            qs = qs.none()
        else:
            qs = qs.filter(event_date__year=y)
    for raw, lookup in ((f_from, "event_date__gte"), (f_to, "event_date__lte")):
        if raw:
            parsed = _parse_date(raw)
            if parsed is None:
                qs = qs.none()
            else:
                qs = qs.filter(**{lookup: parsed})
    if f_prop:
        try:
            pid = int(f_prop)
        except (TypeError, ValueError):
            qs = qs.none()
        else:
            if abs(pid) > 2147483647:
                qs = qs.none()
            else:
                try:
                    prop = FarmProperty.objects.get(pk=pid, farm=farm)
                except (FarmProperty.DoesNotExist, TypeError, ValueError, OverflowError):
                    qs = qs.none()
                else:
                    qs = qs.filter(property=prop)
    if f_type:
        if f_type in EVENT_TYPE_VALUES:
            qs = qs.filter(event_type=f_type)
        else:
            qs = qs.none()
    raw_sort = (request.GET.get("sort") or "").strip()
    raw_dir = (request.GET.get("dir") or "").strip()
    sort_key = raw_sort if raw_sort in SORT_FIELDS else "date"
    sort_dir = raw_dir if raw_dir in SORT_DIRS else "asc"
    sort_field = SORT_FIELDS[sort_key]
    if sort_dir == "desc":
        qs = qs.order_by("-%s" % sort_field, "id")
    else:
        qs = qs.order_by(sort_field, "id")
    paginator = Paginator(qs, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    ctx = _ctx_farms(request, farm)
    ctx.update({
        "events": page_obj, "page_obj": page_obj,
        "role": role,
        "can_create": role in EDIT_ROLES,
        "can_edit": role in EDIT_ROLES,
        "can_delete": role in WRITE_ROLES,
        "properties": FarmProperty.objects.filter(farm=farm).order_by("name"),
        "event_types": EVENT_TYPE_CHOICES,
        "f_year": f_year, "f_from": f_from, "f_to": f_to,
        "f_prop": f_prop, "f_type": f_type,
        "sort": sort_key, "dir": sort_dir,
    })
    return render(request, "portal/event_list.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def event_create(request):
    farm = _farm_or_404(request)
    if role_for(request.user, farm) not in EDIT_ROLES:
        raise PermissionDenied()
    if request.method == "POST":
        form = FarmingEventForm(request.POST, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False)
                    obj.farm = farm
                    obj.created_by = request.user
                    obj.updated_by = request.user
                    obj.save()
                    _audit(farm, request.user, "domain.event.create", obj)
                    # DB-only sowing enqueue; never breaks the journal save.
                    try:
                        from .crop_sowing import enqueue_sowing_reconciliation
                        enqueue_sowing_reconciliation(farm, obj, request.user)
                    except Exception:
                        import logging as _logging
                        _logging.getLogger(__name__).error("sowing enqueue failed")
                messages.success(request, _("Mentve."))
                return redirect("event_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = FarmingEventForm(farm=farm)
    ctx = _ctx_farms(request, farm)
    ctx.update({"form": form, "title": _("\u00daj bejegyz\u00e9s")})
    return render(request, "portal/event_form.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def event_edit(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(FarmingEvent, pk=pk, farm=farm)
    if role_for(request.user, farm) not in EDIT_ROLES:
        raise PermissionDenied()
    if request.method == "POST":
        form = FarmingEventForm(request.POST, instance=obj, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    o = form.save(commit=False)
                    o.updated_by = request.user
                    o.save()
                    _audit(farm, request.user, "domain.event.update", o)
                    # DB-only sowing enqueue; never breaks the journal save.
                    try:
                        from .crop_sowing import enqueue_sowing_reconciliation
                        enqueue_sowing_reconciliation(farm, o, request.user)
                    except Exception:
                        import logging as _logging
                        _logging.getLogger(__name__).error("sowing enqueue failed")
                messages.success(request, _("Mentve."))
                return redirect("event_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = FarmingEventForm(instance=obj, farm=farm)
    ctx = _ctx_farms(request, farm)
    ctx.update({"form": form, "obj": obj, "title": _("Bejegyz\u00e9s szerkeszt\u00e9se")})
    return render(request, "portal/event_form.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def event_delete(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(FarmingEvent, pk=pk, farm=farm)
    if role_for(request.user, farm) not in WRITE_ROLES:
        raise PermissionDenied()
    if request.method == "POST":
        with transaction.atomic():
            _dpk = obj.pk
            obj.delete()
            AuditEvent.objects.create(
                farm=farm, actor=request.user, action="domain.event.delete",
                object_type="event", object_id=str(_dpk),
                meta={"event_id": _dpk},
            )
        messages.success(request, _("T\u00f6r\u00f6lve."))
        return redirect("event_list")
    ctx = _ctx_farms(request, farm)
    ctx.update({"obj": obj, "title": _("Bejegyz\u00e9s t\u00f6rl\u00e9se")})
    return render(request, "portal/event_confirm_delete.html", ctx)
