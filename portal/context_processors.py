from .models import Farm
from django.http import Http404

def owner_nav(request):
    u = getattr(request, "user", None)
    if u is None or not getattr(u, "is_authenticated", False):
        return {}
    return {"is_farm_owner": Farm.objects.filter(owner=u).exists()}


def page_title(request):
    """Localized per-route document title from the actual resolver view name.

    Uses request.resolver_match.url_name so every menu route (including
    form/detail views) gets its own title. Falls back to the generic
    portal title only for genuinely unknown routes.
    """
    from django.utils.translation import gettext as _
    name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    titles = {
        "dashboard": _("Áttekintés – gazdaportál"),
        "farm": _("Gazdaságom – gazdaportál"),
        "location_list": _("Helyszínek – gazdaportál"),
        "location_new": _("Új helyszín – gazdaportál"),
        "location_edit": _("Helyszín szerkesztése – gazdaportál"),
        "location_delete": _("Helyszín törlése – gazdaportál"),
        "property_list": _("Területek – gazdaportál"),
        "property_new": _("Új terület – gazdaportál"),
        "property_edit": _("Terület szerkesztése – gazdaportál"),
        "property_delete": _("Terület törlése – gazdaportál"),
        "equipment_list": _("Gépek – gazdaportál"),
        "equipment_new": _("Új gép – gazdaportál"),
        "equipment_edit": _("Gép szerkesztése – gazdaportál"),
        "equipment_delete": _("Gép törlése – gazdaportál"),
        "cycle_list": _("Vetésforgó – gazdaportál"),
        "cycle_new": _("Új vetésforgó – gazdaportál"),
        "cycle_edit": _("Vetésforgó szerkesztése – gazdaportál"),
        "cycle_delete": _("Vetésforgó törlése – gazdaportál"),
        "cycle_observe": _("Megfigyelés – gazdaportál"),
        "event_list": _("Gazdálkodási napló – gazdaportál"),
        "event_new": _("Új bejegyzés – gazdaportál"),
        "event_edit": _("Bejegyzés szerkesztése – gazdaportál"),
        "event_delete": _("Bejegyzés törlése – gazdaportál"),
        "plans": _("Munkaterv – gazdaportál"),
        "utilization": _("Kihasználtság – gazdaportál"),
        "weather": _("Időjárás – gazdaportál"),
        "weather_refresh": _("Időjárás-frissítés – gazdaportál"),
        "actions": _("Teendők – gazdaportál"),
        "action_create": _("Új teendő – gazdaportál"),
        "action_edit": _("Teendő szerkesztése – gazdaportál"),
        "action_delete": _("Teendő törlése – gazdaportál"),
        "action_toggle": _("Teendő állapota – gazdaportál"),
        "action_from_step": _("Mentés lépésből – gazdaportál"),
        "chat": _("Csevegés – gazdaportál"),
        "chat_proposal_confirm": _("Javaslat megerősítése – gazdaportál"),
        "advice_request": _("Tanács kérése – gazdaportál"),
        "advice_history": _("Tanácselőzmények – gazdaportál"),
        "advice_detail": _("Tanács részletei – gazdaportál"),
        "rule_list": _("Szabályleírások – gazdaportál"),
        "rule_new": _("Új szabályleírás – gazdaportál"),
        "rule_detail": _("Szabályleírás – gazdaportál"),
        "rule_edit": _("Szabályleírás szerkesztése – gazdaportál"),
        "rule_category_new": _("Új kategória – gazdaportál"),
        "members": _("Tagok – gazdaportál"),
        "invite": _("Meghívás – gazdaportál"),
        "invite_revoke": _("Meghívó visszavonása – gazdaportál"),
        "member_change": _("Szerepváltás – gazdaportál"),
        "member_remove": _("Tag eltávolítása – gazdaportál"),
        "invitation_accept": _("Meghívó elfogadása – gazdaportál"),
        "account": _("Fiók – gazdaportál"),
        "consent": _("Hozzájárulás – gazdaportál"),
        "export": _("Adatexport – gazdaportál"),
        "export_info": _("Adatexport tájékoztató – gazdaportál"),
        "account_delete": _("Fiók törlése – gazdaportál"),
        "info": _("Tudnivalók – gazdaportál"),
        "select_active_farm": _("Aktív farm váltása – gazdaportál"),
        "login": _("Belépés – gazdaportál"),
        "register": _("Regisztráció – gazdaportál"),
    }
    return {"page_title": titles.get(name, _("Regeneratív átállás – gazdaportál"))}


def global_farm_selector(request):
    u = getattr(request, "user", None)
    if u is None or not getattr(u, "is_authenticated", False):
        return {}
    try:
        from .permissions import accessible_farms, get_active_farm, role_for
        farms = sorted(accessible_farms(u), key=lambda f: f.id)
        try:
            active = get_active_farm(request)
        except Http404:
            active = farms[0] if farms else None
            try:
                if active is not None:
                    request.session["active_farm_id"] = active.id
                else:
                    request.session.pop("active_farm_id", None)
            except Exception:
                pass
        active_id = active.id if active is not None else None
        role = role_for(u, active) if active is not None else None
        from django.utils.translation import get_language
        _en = (get_language() or "").startswith("en")
        _labels = (
            {"owner": "owner", "manager": "manager", "worker": "worker", "viewer": "viewer"}
            if _en
            else {"owner": "tulajdonos", "manager": "menedzser", "worker": "munkatárs", "viewer": "megfigyelő"})
        role_label = _labels.get(role, role or "")
        # Confirmed-mismatch badge for the active farm; pending never raises it.
        # Retained differences keep a distinct indicator; hide/match clears it.
        mismatch_count = 0
        retained_count = 0
        try:
            if active is not None:
                from .models import SowingComparison
                mismatch_count = SowingComparison.objects.filter(
                    farm_id=active.id,
                    review_state="confirmed_mismatch").count()
                retained_count = SowingComparison.objects.filter(
                    farm_id=active.id,
                    review_state="dismissed_retained").count()
        except Exception:
            mismatch_count = 0
            retained_count = 0
        return {
            "global_farms": farms,
            "global_active_farm": active,
            "global_active_farm_id": active_id,
            "global_active_role": role,
            "global_active_role_label": role_label,
            "sowing_mismatch_count": mismatch_count,
            "sowing_retained_count": retained_count,
        }
    except Exception:
        return {}
