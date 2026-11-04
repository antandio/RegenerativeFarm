"""Task5 external contact views: owner-only staff-subsection CRUD.

Mirrors the staff owner-only gate (_owner_farm): Farm owned by the
request user, role must be owner. Non-owners (viewer/manager/worker)
get PermissionDenied on read and write; other farms' contacts resolve
to 404 via farm-scoped get_object_or_404, never leaking emails/phones.
Contacts never create User/Membership rows (plain tenant model).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .domain_forms import ExternalContactForm
from .models import AuditEvent, ExternalContact, Farm
from .permissions import accessible_farms, role_for


def _owner_farm(request):
    farm = Farm.objects.filter(owner=request.user).first()
    if farm is None:
        raise PermissionDenied()
    if role_for(request.user, farm) != "owner":
        raise PermissionDenied()
    return farm


def _audit(farm, actor, action, oid="", meta=None):
    return AuditEvent.objects.create(
        farm=farm, actor=actor, action=action,
        object_type="external_contact", object_id=str(oid or ""),
        meta=meta or {})


def _ctx(request, farm, extra=None):
    ctx = {"farm": farm, "farms": accessible_farms(request.user)}
    if extra:
        ctx.update(extra)
    return ctx


@login_required
def contact_list(request):
    farm = _owner_farm(request)
    items = ExternalContact.objects.filter(farm=farm)
    return render(request, "portal/external_contact_list.html",
                  _ctx(request, farm, {"items": items}))


@login_required
def contact_detail(request, pk):
    farm = _owner_farm(request)
    obj = get_object_or_404(ExternalContact, pk=pk, farm=farm)
    return render(request, "portal/external_contact_detail.html",
                  _ctx(request, farm, {"obj": obj,
                                       "services": obj.service_list()}))


@login_required
@require_http_methods(["GET", "POST"])
def contact_create(request):
    farm = _owner_farm(request)
    if request.method == "POST":
        form = ExternalContactForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen kapcsolattartó-adatok."))
            return render(request, "portal/external_contact_form.html",
                          _ctx(request, farm, {"form": form}), status=400)
        obj = form.save(commit=False)
        obj.farm = farm
        obj.save()
        _audit(farm, request.user, "external_contact.created",
               obj.id, {"name": obj.name})
        messages.success(request, _("Külső kapcsolattartó létrehozva."))
        return redirect("external_contact_detail", pk=obj.pk)
    return render(request, "portal/external_contact_form.html",
                  _ctx(request, farm, {"form": ExternalContactForm()}))


@login_required
@require_http_methods(["GET", "POST"])
def contact_edit(request, pk):
    farm = _owner_farm(request)
    obj = get_object_or_404(ExternalContact, pk=pk, farm=farm)
    if request.method == "POST":
        form = ExternalContactForm(request.POST, instance=obj)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen kapcsolattartó-adatok."))
            return render(request, "portal/external_contact_form.html",
                          _ctx(request, farm, {"form": form, "obj": obj}),
                          status=400)
        form.save()
        _audit(farm, request.user, "external_contact.updated",
               obj.id, {"name": obj.name})
        messages.success(request, _("Külső kapcsolattartó frissítve."))
        return redirect("external_contact_detail", pk=obj.pk)
    return render(request, "portal/external_contact_form.html",
                  _ctx(request, farm,
                       {"form": ExternalContactForm(instance=obj), "obj": obj}))


@login_required
@require_http_methods(["GET", "POST"])
def contact_delete(request, pk):
    farm = _owner_farm(request)
    obj = get_object_or_404(ExternalContact, pk=pk, farm=farm)
    if request.method == "POST":
        _audit(farm, request.user, "external_contact.deleted",
               obj.id, {"name": obj.name})
        obj.delete()
        messages.success(request, _("Külső kapcsolattartó törölve."))
        return redirect("external_contact_list")
    return render(request, "portal/external_contact_confirm_delete.html",
                  _ctx(request, farm, {"obj": obj}))
