"""Stage 2 tenant-scoped CRUD. Fixed allowlist kinds only."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from .models import Farm, FarmLocation, FarmProperty, Equipment, EquipmentProperty, CropCycle, AuditEvent
from .permissions import get_active_farm, role_for, accessible_farms
from .domain_forms import LocationForm, PropertyForm, EquipmentForm, CropCycleForm, ObservationForm

KINDS = ("location", "property", "equipment", "crop")
WRITE_ROLES = ("owner", "manager")

def _audit(farm, actor, action, otype="", oid=""):
    return AuditEvent.objects.create(farm=farm, actor=actor, action=action, object_type=otype, object_id=str(oid or ""), meta={})



def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        from django.http import Http404
        raise Http404()
    return farm

def _check_write(request, farm):
    if role_for(request.user, farm) not in WRITE_ROLES:
        raise PermissionDenied()

def _ctx_farms(request, farm):
    try:
        farms = accessible_farms(request.user)
    except Exception:
        farms = [farm] if farm else []
    return {"farm": farm, "farms": farms, "active_farm": farm}

# ---- locations ----
@login_required
def location_list(request):
    farm = _farm_or_404(request)
    items = FarmLocation.objects.filter(farm=farm)
    role = role_for(request.user, farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "location", "items": items, "role": role, "can_manage": role in WRITE_ROLES, "can_observe": role in ("owner", "manager", "worker")})
    return render(request, "portal/domain_list.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def location_create(request):
    farm = _farm_or_404(request)
    if request.method == "POST":
        _check_write(request, farm)
        form = LocationForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                obj = form.save(commit=False); obj.farm = farm; obj.save()
                _audit(farm, request.user, "domain.location.create", "location", obj.pk)
            messages.success(request, _("Mentve."))
            return redirect("location_list")
    else:
        form = LocationForm()
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "location", "form": form, "title": _("Új helyszín")})
    return render(request, "portal/domain_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def location_edit(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(FarmLocation, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        form = LocationForm(request.POST, instance=obj)
        if form.is_valid():
            with transaction.atomic():
                form.save()
                _audit(farm, request.user, "domain.location.update", "location", obj.pk)
            messages.success(request, _("Mentve."))
            return redirect("location_list")
    else:
        form = LocationForm(instance=obj)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "location", "form": form, "title": _("Helyszín szerkesztése")})
    return render(request, "portal/domain_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def location_delete(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(FarmLocation, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        try:
            with transaction.atomic():
                _dpk = obj.pk
                obj.delete()
                _audit(farm, request.user, "domain.location.delete", "location", _dpk)
            messages.success(request, _("Törölve."))
            return redirect("location_list")
        except ProtectedError:
            messages.error(request, _("Nem törölhető: tartozik hozzá terület. Előbb törölje vagy helyezze át azokat."))
            ctx = _ctx_farms(request, farm); ctx.update({"kind": "location", "obj": obj, "protected": True, "title": _("Helyszín törlése")})
            return render(request, "portal/domain_confirm_delete.html", ctx)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "location", "obj": obj, "protected": False, "title": _("Helyszín törlése")})
    return render(request, "portal/domain_confirm_delete.html", ctx)

# ---- properties ----
def _property_overview_payload(items):
    payload = []
    for o in items:
        lat = o.point_latitude
        lon = o.point_longitude
        try:
            lat_f = float(lat) if lat is not None else None
        except (TypeError, ValueError):
            lat_f = None
        try:
            lon_f = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lon_f = None
        payload.append({
            "id": o.pk,
            "name": str(o.name or ""),
            "kind": str(o.get_kind_display()),
            "polygon": o.boundary_geojson,
            "lat": lat_f,
            "lon": lon_f,
        })
    return payload


def _payload_json(payload):
    import json as _json
    s = _json.dumps(payload, ensure_ascii=False)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _farm_default_center(farm):
    """READONLY default map center: farm's first location; never written back."""
    loc = FarmLocation.objects.filter(farm=farm).order_by("name").first()
    if loc is not None:
        try:
            return {"lat": str(loc.latitude), "lon": str(loc.longitude)}
        except Exception:
            pass
    return {"lat": "46.25", "lon": "20.14"}


def _property_map_initial(form, instance=None, farm=None):
    import json as _json
    boundary = None
    lat = lon = None
    center = None
    if form is not None and form.is_bound:
        raw = form.data.get("boundary_geojson", "")
        if isinstance(raw, str) and raw.strip():
            try:
                boundary = _json.loads(raw)
            except Exception:
                boundary = None
        lat_raw = form.data.get("point_latitude", "")
        lon_raw = form.data.get("point_longitude", "")
        lat = lat_raw if isinstance(lat_raw, str) and lat_raw.strip() else None
        lon = lon_raw if isinstance(lon_raw, str) and lon_raw.strip() else None
        # Center follows the selected FarmLocation (readonly); never updated.
        if farm is not None:
            try:
                sel = FarmLocation.objects.filter(
                    farm=farm, pk=int(form.data.get("location"))).first()
                if sel is not None:
                    center = {"lat": str(sel.latitude), "lon": str(sel.longitude)}
            except (TypeError, ValueError):
                pass
        if center is None and farm is not None:
            center = _farm_default_center(farm)
    elif instance is not None and getattr(instance, "pk", None):
        boundary = instance.boundary_geojson
        if instance.point_latitude is not None:
            lat = str(instance.point_latitude)
        if instance.point_longitude is not None:
            lon = str(instance.point_longitude)
        try:
            loc = instance.location
            if loc is not None:
                center = {"lat": str(loc.latitude), "lon": str(loc.longitude)}
        except Exception:
            center = None
        if center is None and farm is not None:
            center = _farm_default_center(farm)
    elif farm is not None:
        center = _farm_default_center(farm)
    if center is None:
        center = {"lat": "46.25", "lon": "20.14"}
    return {"boundary": boundary, "point": {"lat": lat, "lon": lon}, "center": center}

@login_required
def property_list(request):
    farm = _farm_or_404(request)
    items = FarmProperty.objects.filter(farm=farm).select_related("location")
    role = role_for(request.user, farm)
    payload = _property_overview_payload(items)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "property", "items": items, "role": role, "can_manage": role in WRITE_ROLES, "can_observe": role in ("owner", "manager", "worker"), "map_properties": payload, "map_properties_json": _payload_json(payload), "map_default_center": _farm_default_center(farm) if not payload else None})
    return render(request, "portal/domain_list.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def property_create(request):
    farm = _farm_or_404(request)
    if request.method == "POST":
        _check_write(request, farm)
        form = PropertyForm(request.POST, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False); obj.farm = farm; obj.save()
                    _audit(farm, request.user, "domain.property.create", "property", obj.pk)
                messages.success(request, _("Mentve."))
                return redirect("property_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = PropertyForm(farm=farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "property", "form": form, "title": _("Új terület"), "map_initial": _property_map_initial(form, farm=farm)})
    return render(request, "portal/property_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def property_edit(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(FarmProperty, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        form = PropertyForm(request.POST, instance=obj, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
                    _audit(farm, request.user, "domain.property.update", "property", obj.pk)
                messages.success(request, _("Mentve."))
                return redirect("property_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = PropertyForm(instance=obj, farm=farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "property", "form": form, "title": _("Terület szerkesztése"), "map_initial": _property_map_initial(form, instance=obj, farm=farm)})
    return render(request, "portal/property_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def property_delete(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(FarmProperty, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        try:
            with transaction.atomic():
                _dpk = obj.pk
                obj.delete()
                _audit(farm, request.user, "domain.property.delete", "property", _dpk)
            messages.success(request, _("Törölve."))
            return redirect("property_list")
        except ProtectedError:
            messages.error(request, _("Nem törölhető: tartozik hozzá vetésforgó vagy kapcsolódó adat."))
            ctx = _ctx_farms(request, farm); ctx.update({"kind": "property", "obj": obj, "protected": True, "title": _("Terület törlése")})
            return render(request, "portal/domain_confirm_delete.html", ctx)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "property", "obj": obj, "protected": False, "title": _("Terület törlése")})
    return render(request, "portal/domain_confirm_delete.html", ctx)

# ---- equipment ----
@login_required
def equipment_list(request):
    farm = _farm_or_404(request)
    items = Equipment.objects.filter(farm=farm).prefetch_related("properties")
    role = role_for(request.user, farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "equipment", "items": items, "role": role, "can_manage": role in WRITE_ROLES, "can_observe": role in ("owner", "manager", "worker")})
    return render(request, "portal/domain_list.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def equipment_create(request):
    farm = _farm_or_404(request)
    if request.method == "POST":
        _check_write(request, farm)
        form = EquipmentForm(request.POST, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False); obj.farm = farm; obj.save()
                    form._save_m2m_through(obj, list(form.cleaned_data.get("properties") or []))
                    _audit(farm, request.user, "domain.equipment.create", "equipment", obj.pk)
                messages.success(request, _("Mentve."))
                return redirect("equipment_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = EquipmentForm(farm=farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "equipment", "form": form, "title": _("Új gép")})
    return render(request, "portal/domain_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def equipment_edit(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(Equipment, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        form = EquipmentForm(request.POST, instance=obj, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    o = form.save(commit=False); o.farm = farm; o.save()
                    form._save_m2m_through(o, list(form.cleaned_data.get("properties") or []))
                    _audit(farm, request.user, "domain.equipment.update", "equipment", o.pk)
                messages.success(request, _("Mentve."))
                return redirect("equipment_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = EquipmentForm(instance=obj, farm=farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "equipment", "form": form, "title": _("Gép szerkesztése")})
    return render(request, "portal/domain_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def equipment_delete(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(Equipment, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        with transaction.atomic():
            _dpk = obj.pk
            obj.delete()
            _audit(farm, request.user, "domain.equipment.delete", "equipment", _dpk)
        messages.success(request, _("Törölve."))
        return redirect("equipment_list")
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "equipment", "obj": obj, "protected": False, "title": _("Gép törlése")})
    return render(request, "portal/domain_confirm_delete.html", ctx)

# ---- crop cycles ----
@login_required
def cycle_list(request):
    farm = _farm_or_404(request)
    from django.db.models import F as _F
    from .domain_models import FarmingEvent, SowingComparison
    props = FarmProperty.objects.filter(farm=farm).order_by("name")
    f_prop = (request.GET.get("property") or "").strip()
    if f_prop:
        # Tenant-safe filter: unknown or foreign property yields no groups.
        try:
            pid = int(f_prop)
            if abs(pid) > 2147483647:
                raise ValueError
            sel = FarmProperty.objects.get(pk=pid, farm=farm)
        except (FarmProperty.DoesNotExist, TypeError, ValueError, OverflowError):
            props = props.none()
        else:
            props = props.filter(pk=sel.pk)
    groups = []
    for p in props:
        # Journal-derived actual sowings, newest effective date first.
        actuals = list(FarmingEvent.objects.filter(
            farm=farm, property=p, event_type="sowing").select_related(
            "crop_cycle").order_by("-event_date", "-id"))
        for e in actuals:
            e.display_crop = e.crop_name_snapshot or (
                e.crop_cycle.crop_name if e.crop_cycle_id else "")
        planned = list(CropCycle.objects.filter(
            farm=farm, property=p, state="planned", is_hidden=False).order_by(
            _F("sow_date").desc(nulls_last=True), "-id"))
        legacy = list(CropCycle.objects.filter(
            farm=farm, property=p, state__in=["current", "history"]).order_by(
            _F("sow_date").desc(nulls_last=True), "-id"))
        groups.append({"property": p, "actuals": actuals,
                       "planned": planned, "legacy": legacy})
    # Review queues: GET only, no provider calls. Pending wording never claims
    # a confirmed contradiction; confirmed mismatches render yellow.
    # Reviews whose planned cycle is hidden never leak onto this page,
    # including duplicate pending snapshots of an already hidden plan.
    pending = list(SowingComparison.objects.filter(
        farm=farm, review_state="pending_review").exclude(
        planned_cycle__is_hidden=True).select_related(
        "property", "planned_cycle", "farming_event").order_by(
        "-attempt_started_at", "-id")[:50])
    confirmed = list(SowingComparison.objects.filter(
        farm=farm, review_state="confirmed_mismatch").exclude(
        planned_cycle__is_hidden=True).select_related(
        "property", "planned_cycle", "farming_event").order_by(
        "-attempt_started_at", "-id")[:50])
    if f_prop:
        try:
            _pid = int(f_prop)
            pending = [r for r in pending if r.property_id == _pid]
            confirmed = [r for r in confirmed if r.property_id == _pid]
        except (TypeError, ValueError):
            pending, confirmed = [], []
    retained_ids = set(SowingComparison.objects.filter(
        farm=farm, review_state="dismissed_retained",
        planned_cycle__isnull=False).values_list(
        "planned_cycle_id", flat=True))
    role = role_for(request.user, farm)
    ctx = _ctx_farms(request, farm); ctx.update({
        "kind": "crop", "groups": groups, "f_prop": f_prop,
        "properties": FarmProperty.objects.filter(farm=farm).order_by("name"),
        "pending_reviews": pending, "confirmed_reviews": confirmed,
        "retained_plan_ids": retained_ids,
        "role": role, "can_manage": role in WRITE_ROLES,
        "can_observe": role in ("owner", "manager", "worker")})
    return render(request, "portal/rotation_list.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def cycle_create(request):
    farm = _farm_or_404(request)
    if request.method == "POST":
        _check_write(request, farm)
        form = CropCycleForm(request.POST, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False); obj.farm = farm; obj.save()
                    _audit(farm, request.user, "domain.crop.create", "crop", obj.pk)
                messages.success(request, _("Mentve."))
                return redirect("cycle_list")
            except ValidationError as e:
                form.add_error(None, e)
            except IntegrityError:
                form.add_error(None, _("Ezen a területen már van ilyen állapotú ciklus."))
    else:
        form = CropCycleForm(farm=farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "crop", "form": form, "title": _("Új vetésforgó")})
    return render(request, "portal/domain_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def cycle_edit(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(CropCycle, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        form = CropCycleForm(request.POST, instance=obj, farm=farm)
        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
                    _audit(farm, request.user, "domain.crop.update", "crop", obj.pk)
                messages.success(request, _("Mentve."))
                return redirect("cycle_list")
            except ValidationError as e:
                form.add_error(None, e)
            except IntegrityError:
                form.add_error(None, _("Ezen a területen már van ilyen állapotú ciklus."))
    else:
        form = CropCycleForm(instance=obj, farm=farm)
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "crop", "form": form, "title": _("Vetésforgó szerkesztése")})
    return render(request, "portal/domain_form.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def cycle_delete(request, pk):
    farm = _farm_or_404(request)
    obj = get_object_or_404(CropCycle, pk=pk, farm=farm)
    if request.method == "POST":
        _check_write(request, farm)
        with transaction.atomic():
            _dpk = obj.pk
            # Planned rows are never physically deleted: soft-hide preserves
            # WorkItems and journal links. Other states keep existing path.
            if obj.state == "planned":
                from django.utils import timezone as _tz
                obj.is_hidden = True
                obj.hidden_at = _tz.now()
                obj.hidden_reason = "deleted"
                obj.save()
            else:
                obj.delete()
            _audit(farm, request.user, "domain.crop.delete", "crop", _dpk)
        messages.success(request, _("Törölve."))
        return redirect("cycle_list")
    ctx = _ctx_farms(request, farm); ctx.update({"kind": "crop", "obj": obj, "protected": False, "title": _("Vetésforgó törlése")})
    return render(request, "portal/domain_confirm_delete.html", ctx)

@login_required
@require_http_methods(["GET", "POST"])
def cycle_observe(request, pk):
    farm = _farm_or_404(request)
    if role_for(request.user, farm) not in ("owner", "manager", "worker"):
        raise PermissionDenied()
    obj = get_object_or_404(CropCycle, pk=pk, farm=farm)
    if request.method == "POST":
        form = ObservationForm(request.POST)
        if form.is_valid():
            note = form.cleaned_data["observation"]
            with transaction.atomic():
                cur = (obj.observations or "")
                obj.observations = (cur + "\n" + note).strip() if cur else note
                try:
                    obj.save()
                    _audit(farm, request.user, "domain.crop.observe", "crop", obj.pk)
                except ValidationError as e:
                    form.add_error(None, e)
                    ctx = _ctx_farms(request, farm); ctx.update({"obj": obj, "form": form, "title": _("Megfigyelés rögzítése")})
                    return render(request, "portal/cycle_observe.html", ctx)
            messages.success(request, _("Megfigyelés rögzítve."))
            return redirect("cycle_list")
    else:
        form = ObservationForm()
    ctx = _ctx_farms(request, farm); ctx.update({"obj": obj, "form": form, "title": _("Megfigyelés rögzítése")})
    return render(request, "portal/cycle_observe.html", ctx)
