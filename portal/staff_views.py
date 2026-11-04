import hashlib
import secrets
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.utils.translation import gettext as _
from .forms import InviteForm
from .models import Farm, Membership, Invitation, AuditEvent
from .permissions import role_for, accessible_farms

INVITE_TTL_HOURS = 72

def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

def _audit(farm, actor, action, otype="", oid="", meta=None):
    return AuditEvent.objects.create(farm=farm, actor=actor, action=action, object_type=otype, object_id=str(oid or ""), meta=meta or {})

def _owner_farm(request):
    farm = Farm.objects.filter(owner=request.user).first()
    if farm is None:
        raise PermissionDenied()
    if role_for(request.user, farm) != "owner":
        raise PermissionDenied()
    return farm

def _members_ctx(farm, user):
    return {
        "farm": farm,
        "memberships": Membership.objects.filter(farm=farm, is_active=True).select_related("user"),
        "invitations": Invitation.objects.filter(farm=farm, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now()),
        "form": InviteForm(),
        "farms": accessible_farms(user),
    }

@login_required
def members_view(request):
    farm = _owner_farm(request)
    return render(request, "portal/members.html", _members_ctx(farm, request.user))

@login_required
@require_POST
def invite_view(request):
    farm = _owner_farm(request)
    form = InviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Érvénytelen meghívó-adatok."))
        return render(request, "portal/members.html", {**_members_ctx(farm, request.user), "form": form}, status=400)
    email = form.cleaned_data["email"].strip().lower()
    role = form.cleaned_data["role"]
    now = timezone.now()
    if Invitation.objects.filter(farm=farm, email=email, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now).exists():
        messages.error(request, _("Ehhez az e-mailhez már van függő meghívó."))
        return render(request, "portal/members.html", _members_ctx(farm, request.user), status=400)
    from django.contrib.auth import get_user_model as _gum
    _existing_user = _gum().objects.filter(email=email).first()
    if _existing_user is not None and Membership.objects.filter(
            farm=farm, user=_existing_user, is_active=True).exists():
        messages.error(request, _("Ez a felhasználó már aktív tag."))
        return render(request, "portal/members.html", _members_ctx(farm, request.user), status=400)
    raw = secrets.token_urlsafe(32)
    inv = Invitation.objects.create(farm=farm, email=email, role=role, token_hash=_hash(raw), created_by=request.user, expires_at=now + timedelta(hours=INVITE_TTL_HOURS))
    _audit(farm, request.user, "invitation.created", "invitation", inv.id, {"email": email, "role": role})
    link = reverse("invitation_accept", args=[raw])
    return render(request, "portal/members.html", {**_members_ctx(farm, request.user), "new_invite_link": link, "new_invite_email": email}, status=201)

@login_required
@require_POST
def revoke_view(request, inv_id):
    farm = _owner_farm(request)
    inv = get_object_or_404(Invitation, pk=inv_id, farm=farm)
    if inv.revoked_at is None and inv.used_at is None:
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        _audit(farm, request.user, "invitation.revoked", "invitation", inv.id, {"email": inv.email})
    return redirect("members")

@login_required
@require_POST
def change_role_view(request, mem_id):
    farm = _owner_farm(request)
    mem = get_object_or_404(Membership, pk=mem_id, farm=farm)
    role = (request.POST.get("role") or "").strip()
    if role not in ("manager", "worker", "viewer"):
        messages.error(request, _("Érvénytelen szerep."))
        return redirect("members")
    mem.role = role
    mem.save(update_fields=["role"])
    _audit(farm, request.user, "membership.role_changed", "membership", mem.id, {"role": role})
    return redirect("members")

@login_required
@require_POST
def remove_member_view(request, mem_id):
    farm = _owner_farm(request)
    mem = get_object_or_404(Membership, pk=mem_id, farm=farm)
    _audit(farm, request.user, "membership.removed", "membership", mem.id, {"user": mem.user.email})
    mem.delete()
    return redirect("members")

@login_required
@require_http_methods(["GET", "POST"])
def invitation_accept_view(request, token):
    try:
        inv = Invitation.objects.select_related("farm").get(token_hash=_hash(token))
    except Invitation.DoesNotExist:
        raise Http404()
    if not inv.is_usable():
        raise Http404()
    if request.method == "POST":
        from django.db import transaction as _txn
        with _txn.atomic():
            try:
                fresh = Invitation.objects.select_for_update().get(pk=inv.pk)
            except Invitation.DoesNotExist:
                raise Http404()
            if not fresh.is_usable():
                raise Http404()
            if (request.user.email or "").strip().lower() != (fresh.email or "").strip().lower():
                raise PermissionDenied()
            existing = Membership.objects.select_for_update().filter(
                farm=fresh.farm, user=request.user).first()
            if existing is not None and existing.is_active:
                # Single-use burn: keep the current role, never overwrite
                # an active membership from the invite.
                fresh.used_at = timezone.now()
                fresh.save(update_fields=["used_at"])
                _audit(fresh.farm, request.user, "invitation.accepted", "invitation",
                       fresh.id, {"email": fresh.email, "kept_role": existing.role})
                request.session["active_farm_id"] = fresh.farm_id
                return redirect("dashboard")
            Membership.objects.update_or_create(
                farm=fresh.farm, user=request.user,
                defaults={"role": fresh.role, "is_active": True})
            fresh.used_at = timezone.now()
            fresh.save(update_fields=["used_at"])
            _audit(fresh.farm, request.user, "invitation.accepted", "invitation",
                   fresh.id, {"email": fresh.email})
            request.session["active_farm_id"] = fresh.farm_id
            return redirect("dashboard")
    return render(request, "portal/invitation_accept.html", {"invitation": inv})
