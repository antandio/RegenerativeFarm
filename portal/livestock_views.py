"""Task4 livestock views: tenant-scoped group CRUD + archive (history preserved).

Write: owner/manager only (worker has no group write, mirroring domain
CRUD; worker journal capabilities live on events). Read: any active role.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .domain_forms import LivestockGroupForm, LivestockGroupEditForm, LivestockEventForm, LivestockWorkDoneForm, LivestockWorkItemForm
from .domain_models import LIVESTOCK_WORK_EVENT_MAP
from .models import AuditEvent, FarmingEvent, LivestockGroup, LivestockEvent, WorkItem
from .permissions import accessible_farms, get_active_farm, role_for

WRITE_ROLES = ("owner", "manager")
EDIT_ROLES = ("owner", "manager", "worker")


def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm


def _ctx(request, farm):
    try:
        farms = accessible_farms(request.user)
    except Exception:
        farms = [farm] if farm else []
    return {"farm": farm, "farms": farms, "active_farm": farm}


def _check_write(request, farm):
    if role_for(request.user, farm) not in WRITE_ROLES:
        raise PermissionDenied()


def _audit(farm, actor, action, oid="", meta=None):
    return AuditEvent.objects.create(
        farm=farm, actor=actor, action=action,
        object_type="livestock_group", object_id=str(oid or ""), meta=meta or {})


@login_required
def livestock_list(request):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    items = LivestockGroup.objects.filter(farm=farm).select_related("housing_property")
    show_archived = (request.GET.get("archived") or "").strip() == "1"
    if not show_archived:
        items = items.filter(is_active=True)
    ctx = _ctx(request, farm)
    ctx.update({"items": items, "role": role,
                "can_manage": role in WRITE_ROLES,
                "show_archived": show_archived})
    return render(request, "portal/livestock_list.html", ctx)


@login_required
def livestock_detail(request, pk):
    farm = _farm_or_404(request)
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    obj = get_object_or_404(
        LivestockGroup.objects.select_related("housing_property"), pk=pk, farm=farm)
    events = list(LivestockEvent.objects.filter(farm=farm, group=obj).select_related(
        "property", "target_group", "farming_event", "farming_event__property"))
    journal_entries = list(FarmingEvent.objects.filter(
        farm=farm, livestock_event__group=obj).select_related("property"))
    planned_items = list(_planned_for_group(farm, obj))
    ctx = _ctx(request, farm)
    ctx.update({"obj": obj, "role": role,
                "can_manage": role in WRITE_ROLES,
                "can_record": role in EDIT_ROLES,
                "events": events,
                "journal_entries": journal_entries, "planned_items": planned_items})
    return render(request, "portal/livestock_detail.html", ctx)


def _planned_for_group(farm, obj):
    """Read-only planned WorkItems synced to this group (slice C fills)."""
    try:
        from .models import WorkItem
    except Exception:
        return []
    prop_ids = {obj.housing_property_id} if obj.housing_property_id else set()
    prop_ids |= {e.property_id for e in
                 LivestockEvent.objects.filter(group=obj).exclude(property__isnull=True)}
    prop_ids.discard(None)
    items = list(WorkItem.objects.filter(farm=farm, livestock_group=obj).order_by(
        "start_date", "id")[:20]) if hasattr(WorkItem, "livestock_group") else []
    if prop_ids:
        try:
            from .models import WorkItem as _WI
            items += list(_WI.objects.filter(
                farm=farm, property_id__in=prop_ids).exclude(
                pk__in=[i.pk for i in items]).order_by("start_date", "id")[:20])
        except Exception:
            pass
    return items


@login_required
@require_http_methods(["GET", "POST"])
def livestock_create(request):
    farm = _farm_or_404(request)
    if request.method == "POST":
        _check_write(request, farm)
        form = LivestockGroupForm(request.POST, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False)
                    obj.farm = farm
                    obj.save()
                    _audit(farm, request.user, "livestock.group.create", obj.pk)
            except IntegrityError:
                form.add_error("name", _("Ilyen nevű csoport már létezik ezen a farmon."))
            else:
                messages.success(request, _("Mentve."))
                return redirect("livestock_detail", pk=obj.pk)
    else:
        form = LivestockGroupForm(farm=farm)
    ctx = _ctx(request, farm)
    ctx.update({"form": form, "title": _("Új állatcsoport")})
    return render(request, "portal/livestock_form.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_edit(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(LivestockGroup, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        if not obj.is_active:
            messages.error(request, _("Archivált csoport nem szerkeszthető. Előbb állítsa vissza."))
            return redirect("livestock_detail", pk=obj.pk)
        old_housing = obj.housing_property_id
        form = LivestockGroupEditForm(request.POST, instance=obj, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    updated = form.save()
                    new_housing = updated.housing_property_id
                    meta = None
                    if old_housing != new_housing:
                        meta = {"housing_old": old_housing, "housing_new": new_housing}
                    _audit(farm, request.user, "livestock.group.update", obj.pk, meta=meta)
            except IntegrityError:
                form.add_error("name", _("Ilyen nevű csoport már létezik ezen a farmon."))
            else:
                messages.success(request, _("Mentve."))
                return redirect("livestock_detail", pk=obj.pk)
    else:
        form = LivestockGroupEditForm(instance=obj, farm=farm)
    ctx = _ctx(request, farm)
    ctx.update({"form": form, "obj": obj, "title": _("Állatcsoport szerkesztése")})
    return render(request, "portal/livestock_form.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_archive(request, pk):
    """Archive an active group, or restore (unarchive) an archived one.

    Archived groups are frozen: no edit, event, work or reversal may
    mutate them — this toggle is the only owner/manager write allowed.
    """
    farm = _farm_or_404(request)
    obj = get_object_or_404(LivestockGroup, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        with transaction.atomic():
            if obj.is_active:
                obj.is_active = False
                obj.save()
                _audit(farm, request.user, "livestock.group.archive", obj.pk)
                messages.success(request, _("Archiválva. Az előzmények megmaradtak."))
            else:
                obj.is_active = True
                obj.save()
                _audit(farm, request.user, "livestock.group.unarchive", obj.pk)
                messages.success(request, _("Visszaállítva."))
        return redirect("livestock_detail", pk=obj.pk)
    ctx = _ctx(request, farm)
    if obj.is_active:
        ctx.update({"obj": obj, "title": _("Állatcsoport archiválása")})
    else:
        ctx.update({"obj": obj, "title": _("Állatcsoport visszaállítása")})
    return render(request, "portal/livestock_confirm_archive.html", ctx)


def _delete_block_reasons(obj):
    """Why a group must be archived instead of hard-deleted."""
    reasons = []
    if LivestockEvent.objects.filter(group=obj).exists():
        reasons.append(_("esemény-előzmény tartozik hozzá"))
    if LivestockEvent.objects.filter(target_group=obj).exists():
        reasons.append(_("más csoportból ide mozgatott állatok nyoma van"))
    if WorkItem.objects.filter(livestock_group=obj).exists():
        reasons.append(_("tervezett vagy elvégzett állatmunka kapcsolódik hozzá"))
    if (obj.headcount or 0) > 0:
        reasons.append(_("élő állományt tart nyilván"))
    return reasons


@login_required
@require_http_methods(["GET", "POST"])
def livestock_delete(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(LivestockGroup, pk=pk, farm=farm)
    block_reasons = _delete_block_reasons(obj)
    if request.method == "POST":
        _check_write(request, farm)
        block_reasons = _delete_block_reasons(obj)
        if block_reasons:
            messages.error(request, _("Nem törölhető (%s). Archiválja a csoportot.") % (
                ", ".join(str(r) for r in block_reasons),))
            return redirect("livestock_detail", pk=obj.pk)
        with transaction.atomic():
            _dpk = obj.pk
            obj.delete()
            _audit(farm, request.user, "livestock.group.delete", _dpk)
        messages.success(request, _("Törölve."))
        return redirect("livestock_list")
    ctx = _ctx(request, farm)
    ctx.update({"obj": obj, "title": _("Állatcsoport törlése"),
                "has_history": bool(block_reasons),
                "block_reasons": block_reasons})
    return render(request, "portal/livestock_confirm_delete.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_event_create(request, pk):
    from django.core.exceptions import ValidationError as _VE
    farm = _farm_or_404(request)
    obj = get_object_or_404(LivestockGroup, pk=pk, farm=farm)
    if role_for(request.user, farm) not in EDIT_ROLES:
        raise PermissionDenied()
    if request.method == "POST":
        if role_for(request.user, farm) not in EDIT_ROLES:
            raise PermissionDenied()
        form = LivestockEventForm(request.POST, farm=farm, group=obj)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                with transaction.atomic():
                    ev = LivestockEvent.record(
                        farm=farm, group=obj, event_date=cd["event_date"],
                        kind=cd["kind"], headcount_delta=cd["headcount_delta"],
                        target_group=cd.get("target_group"),
                        property=cd.get("property"),
                        notes=cd.get("notes") or "",
                        create_journal=bool(cd.get("create_journal")),
                        actor=request.user)
                    _audit(farm, request.user, "livestock.event.create", ev.pk)
            except _VE as e:
                form.add_error(None, e)
            else:
                messages.success(request, _("Esemény rögzítve."))
                return redirect("livestock_detail", pk=obj.pk)
    else:
        form = LivestockEventForm(farm=farm, group=obj)
    ctx = _ctx(request, farm)
    ctx.update({"form": form, "obj": obj, "title": _("Új állatesemény")})
    return render(request, "portal/livestock_event_form.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_work_create(request, pk):
    """Manual planned grazing/feeding/health work for one group."""
    farm = _farm_or_404(request)
    obj = get_object_or_404(LivestockGroup, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        if not obj.is_active:
            messages.error(request, _("Archivált csoportra nem tervezhető munka."))
            return redirect("livestock_detail", pk=obj.pk)
        form = LivestockWorkItemForm(request.POST, farm=farm, group=obj)
        if form.is_valid():
            from django.core.exceptions import ValidationError as _VE
            try:
                with transaction.atomic():
                    w = form.save(commit=False, group=obj)
                    w.farm = farm
                    w.save()
                    _audit(farm, request.user, "livestock.work.create", w.pk)
            except _VE as e:
                form.add_error(None, e)
            else:
                messages.success(request, _("Mentve."))
                return redirect("livestock_detail", pk=obj.pk)
    else:
        if role_for(request.user, farm) not in WRITE_ROLES:
            raise PermissionDenied()
        form = LivestockWorkItemForm(farm=farm, group=obj)
    ctx = _ctx(request, farm)
    ctx.update({"form": form, "obj": obj, "title": _("Új tervezett állatmunka")})
    return render(request, "portal/livestock_work_form.html", ctx)


def _livestock_work_or_404(farm, pk):
    try:
        return WorkItem.objects.select_related(
            "livestock_group", "completed_event").get(
            pk=pk, farm=farm, livestock_group__isnull=False)
    except WorkItem.DoesNotExist:
        raise Http404()


@login_required
@require_http_methods(["GET", "POST"])
def livestock_work_edit(request, pk):
    from django.core.exceptions import ValidationError as _VE
    farm = _farm_or_404(request)
    w = _livestock_work_or_404(farm, pk)
    if request.method == "POST":
        _check_write(request, farm)
        if not w.livestock_group.is_active:
            messages.error(request, _("Archivált csoport munkája nem módosítható. Előbb állítsa vissza a csoportot."))
            return redirect("livestock_detail", pk=w.livestock_group_id)
        form = LivestockWorkItemForm(request.POST, instance=w, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save(commit=False, group=w.livestock_group)
                    w.farm = farm
                    w.save()
                    _audit(farm, request.user, "livestock.work.update", w.pk)
            except _VE as e:
                form.add_error(None, e)
            else:
                messages.success(request, _("Mentve."))
                return redirect("livestock_detail", pk=w.livestock_group_id)
    else:
        if role_for(request.user, farm) not in WRITE_ROLES:
            raise PermissionDenied()
        form = LivestockWorkItemForm(instance=w, farm=farm)
    ctx = _ctx(request, farm)
    ctx.update({"form": form, "obj": w.livestock_group, "work": w,
                "title": _("Tervezett állatmunka szerkesztése")})
    return render(request, "portal/livestock_work_form.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_work_done(request, pk):
    """Mark planned work done; sync one actual event (explicit form if none)."""
    from django.core.exceptions import ValidationError as _VE
    farm = _farm_or_404(request)
    w = _livestock_work_or_404(farm, pk)
    if role_for(request.user, farm) not in EDIT_ROLES:
        raise PermissionDenied()
    if request.method == "POST" and not w.livestock_group.is_active:
        messages.error(request, _("Archivált csoport munkája nem zárható le. Előbb állítsa vissza a csoportot."))
        return redirect("livestock_detail", pk=w.livestock_group_id)
    if w.completed_event_id:
        if request.method == "POST":
            if w.status != "done":
                with transaction.atomic():
                    w.status = "done"
                    w.save()
            messages.success(request, _("Kész. Az esemény már rögzítve volt."))
            return redirect("livestock_detail", pk=w.livestock_group_id)
        ctx = _ctx(request, farm)
        ctx.update({"form": None, "work": w, "obj": w.livestock_group,
                    "title": _("Tervezett állatmunka elvégzése")})
        return render(request, "portal/livestock_work_done.html", ctx)
    if request.method == "POST":
        form = LivestockWorkDoneForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    ev = LivestockEvent.record(
                        farm=farm, group=w.livestock_group,
                        event_date=form.cleaned_data["event_date"],
                        kind=LIVESTOCK_WORK_EVENT_MAP.get(w.livestock_kind, "other"),
                        headcount_delta=0,
                        property=w.property,
                        notes=(form.cleaned_data.get("notes") or w.title or ""),
                        actor=request.user)
                    w.status = "done"
                    w.completed_at = form.cleaned_data["event_date"]
                    w.completed_event = ev
                    w.save()
                    _audit(farm, request.user, "livestock.work.done", w.pk)
            except _VE as e:
                form.add_error(None, e)
            else:
                messages.success(request, _("Kész. Az elvégzés eseményként rögzítve."))
                return redirect("livestock_detail", pk=w.livestock_group_id)
    else:
        form = LivestockWorkDoneForm(initial={"event_date": w.end_date})
    ctx = _ctx(request, farm)
    ctx.update({"form": form, "work": w, "obj": w.livestock_group,
                "title": _("Tervezett állatmunka elvégzése")})
    return render(request, "portal/livestock_work_done.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_work_delete(request, pk):
    farm = _farm_or_404(request)
    w = _livestock_work_or_404(farm, pk)
    gid = w.livestock_group_id
    if request.method == "POST":
        _check_write(request, farm)
        if not w.livestock_group.is_active:
            messages.error(request, _("Archivált csoport munkája nem törölhető. Előbb állítsa vissza a csoportot."))
            return redirect("livestock_detail", pk=gid)
        with transaction.atomic():
            _dpk = w.pk
            w.delete()
            _audit(farm, request.user, "livestock.work.delete", _dpk)
        messages.success(request, _("Törölve."))
        return redirect("livestock_detail", pk=gid)
    if role_for(request.user, farm) not in WRITE_ROLES:
        raise PermissionDenied()
    ctx = _ctx(request, farm)
    ctx.update({"work": w, "obj": w.livestock_group,
                "title": _("Tervezett állatmunka törlése")})
    return render(request, "portal/livestock_work_confirm_delete.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def livestock_event_delete(request, ek):
    from django.core.exceptions import ValidationError as _VE
    farm = _farm_or_404(request)
    ev = get_object_or_404(
        LivestockEvent.objects.select_related("group"), pk=ek, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        try:
            with transaction.atomic():
                _dpk = ev.pk
                gid = ev.group_id
                ev.reverse_delete()
                _audit(farm, request.user, "livestock.event.delete", _dpk)
        except _VE as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("livestock_detail", pk=ev.group_id)
        messages.success(request, _("Esemény visszavonva (a naplóbejegyzés megmaradt)."))
        return redirect("livestock_detail", pk=gid)
    ctx = _ctx(request, farm)
    ctx.update({"obj": ev, "title": _("Állatesemény visszavonása")})
    return render(request, "portal/livestock_event_confirm_delete.html", ctx)
