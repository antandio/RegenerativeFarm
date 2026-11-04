"""Sowing review actions: POST-only owner/manager retry/retain/hide.

English comments per project convention. Farm-scoped, role rechecked,
stale-guarded; GET never schedules or calls the provider.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .models import AuditEvent, CropCycle, FarmingEvent, SowingComparison
from .permissions import get_active_farm, role_for

WRITE_ROLES = ("owner", "manager")


def _farm_or_404(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    return farm


def _check_write(request, farm):
    if role_for(request.user, farm) not in WRITE_ROLES:
        raise PermissionDenied()


def _stale_or_mismatch(row):
    """True when the pair no longer matches the stored input hash/window."""
    from .crop_sowing import _pair_still_valid, canonical_input_hash
    if not _pair_still_valid(row):
        return True
    try:
        event = FarmingEvent.objects.get(pk=row.farming_event_id)
        cand = CropCycle.objects.get(pk=row.planned_cycle_id)
    except Exception:
        return True
    ihash = canonical_input_hash(
        row.farm_id, event.pk, event.property_id,
        (event.crop_name_snapshot or "").strip(), event.event_date,
        cand.pk, (cand.crop_name or "").strip(), cand.sow_date)
    return ihash != row.input_hash


@login_required
@require_http_methods(["POST"])
def sowing_retry(request, pk):
    """Explicit owner/manager retry; consent/quota gates live in the service."""
    farm = _farm_or_404(request)
    _check_write(request, farm)
    # Farm scoping first; service rechecks role and staleness.
    get_object_or_404(SowingComparison, pk=pk, farm=farm)
    from .crop_sowing import retry_sowing_comparison
    row, scheduled = retry_sowing_comparison(request.user, farm, pk)
    AuditEvent.objects.create(
        farm=farm, actor=request.user, action="sowing.retry",
        object_type="sowing", object_id=str(row.pk),
        meta={"event_id": row.farming_event_id,
              "planned_cycle_id": row.planned_cycle_id,
              "scheduled": bool(scheduled)})
    if scheduled:
        messages.success(request, _("Újrapróbálás elindítva."))
    else:
        messages.info(request, _("Nincs szükség újrapróbálásra."))
    return redirect("cycle_list")


@login_required
@require_http_methods(["POST"])
def sowing_retain(request, pk):
    """Dismiss a review while keeping the plan visible as retained difference."""
    farm = _farm_or_404(request)
    _check_write(request, farm)
    with transaction.atomic():
        try:
            row = SowingComparison.objects.select_for_update().get(
                pk=pk, farm_id=farm.pk)
        except SowingComparison.DoesNotExist:
            raise Http404()
        from .crop_sowing import _lock_event_for_update
        _lock_event_for_update(row.farming_event_id)
        if row.review_state not in ("pending_review", "confirmed_mismatch"):
            raise Http404()
        if _stale_or_mismatch(row):
            now = timezone.now()
            SowingComparison.objects.filter(pk=row.pk).update(
                status="superseded", review_state="superseded",
                completed_at=now)
            AuditEvent.objects.create(
                farm=farm, actor=request.user, action="sowing.stale",
                object_type="sowing", object_id=str(row.pk),
                meta={"event_id": row.farming_event_id})
            messages.success(request, _("Elavult összevetés lezárva."))
            return redirect("cycle_list")
        now = timezone.now()
        SowingComparison.objects.filter(pk=row.pk).update(
            status="success", review_state="dismissed_retained",
            completed_at=now)
        AuditEvent.objects.create(
            farm=farm, actor=request.user, action="sowing.retain",
            object_type="sowing", object_id=str(row.pk),
            meta={"event_id": row.farming_event_id,
                  "planned_cycle_id": row.planned_cycle_id})
    messages.success(request, _("Eltérés megtartva."))
    return redirect("cycle_list")


@login_required
@require_http_methods(["POST"])
def sowing_hide(request, pk):
    """Soft-hide the planned cycle; journal and work items are preserved."""
    farm = _farm_or_404(request)
    _check_write(request, farm)
    with transaction.atomic():
        try:
            row = SowingComparison.objects.select_for_update().get(
                pk=pk, farm_id=farm.pk)
        except SowingComparison.DoesNotExist:
            raise Http404()
        from .crop_sowing import _event_consumed, _lock_event_for_update
        _lock_event_for_update(row.farming_event_id)
        if row.review_state not in ("pending_review", "confirmed_mismatch"):
            raise Http404()
        if _stale_or_mismatch(row):
            now = timezone.now()
            SowingComparison.objects.filter(pk=row.pk).update(
                status="superseded", review_state="superseded",
                completed_at=now)
            AuditEvent.objects.create(
                farm=farm, actor=request.user, action="sowing.stale",
                object_type="sowing", object_id=str(row.pk),
                meta={"event_id": row.farming_event_id})
            messages.success(request, _("Elavult összevetés lezárva."))
            return redirect("cycle_list")
        if _event_consumed(farm.pk, row.farming_event_id, exclude_pk=row.pk):
            now = timezone.now()
            SowingComparison.objects.filter(pk=row.pk).update(
                status="superseded", review_state="superseded",
                completed_at=now)
            AuditEvent.objects.create(
                farm=farm, actor=request.user, action="sowing.stale",
                object_type="sowing", object_id=str(row.pk),
                meta={"event_id": row.farming_event_id})
            messages.success(request, _("Elavult összevetés lezárva."))
            return redirect("cycle_list")
        now = timezone.now()
        if row.planned_cycle_id:
            try:
                plan = CropCycle.objects.select_for_update().get(
                    pk=row.planned_cycle_id, farm_id=farm.pk)
            except CropCycle.DoesNotExist:
                plan = None
            if plan is not None:
                plan.is_hidden = True
                plan.hidden_at = now
                plan.hidden_reason = "dismissed"
                plan.save()
        SowingComparison.objects.filter(pk=row.pk).update(
            status="success", review_state="dismissed_deleted",
            completed_at=now)
        AuditEvent.objects.create(
            farm=farm, actor=request.user, action="sowing.hide",
            object_type="sowing", object_id=str(row.pk),
            meta={"event_id": row.farming_event_id,
                  "planned_cycle_id": row.planned_cycle_id})
    messages.success(request, _("Terv elrejtve."))
    return redirect("cycle_list")
