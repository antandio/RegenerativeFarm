from django.http import Http404
from functools import wraps
from .models import Farm, Membership

def accessible_farms(user):
    farms = list(Farm.objects.filter(owner=user))
    seen = {f.id for f in farms}
    for m in Membership.objects.filter(user=user, is_active=True).select_related("farm"):
        if m.farm_id not in seen:
            farms.append(m.farm)
            seen.add(m.farm_id)
    return farms

def role_for(user, farm):
    if user is None or not getattr(user, "is_authenticated", False) or farm is None:
        return None
    if farm.owner_id == user.pk:
        return "owner"
    m = Membership.objects.filter(farm=farm, user=user, is_active=True).first()
    return m.role if m else None

def get_active_farm(request):
    user = request.user
    fid = request.session.get("active_farm_id")
    if fid is not None:
        try:
            farm = Farm.objects.get(pk=int(fid))
        except (Farm.DoesNotExist, ValueError, TypeError):
            try:
                request.session.pop("active_farm_id", None)
            except Exception:
                pass
            fid = None
            farm = None
        else:
            if role_for(user, farm) is None:
                raise Http404()
            return farm
    own = Farm.objects.filter(owner=user).first()
    if own:
        return own
    m = Membership.objects.filter(user=user, is_active=True).select_related("farm").first()
    return m.farm if m else None

def require_role(*allowed):
    def deco(view):
        @wraps(view)
        def inner(request, *a, **kw):
            farm = get_active_farm(request)
            if farm is None:
                raise Http404()
            role = role_for(request.user, farm)
            if role not in allowed:
                from django.core.exceptions import PermissionDenied
                raise PermissionDenied()
            request.active_farm = farm
            request.active_role = role
            return view(request, *a, **kw)
        return inner
    return deco
