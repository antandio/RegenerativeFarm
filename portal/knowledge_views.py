"""Task9 knowledge views: active installs + marketplace on one farm page.

Reads use fresh DB state on every request: eligible marketplace rows are
author-of-farm-owner OR currently public (other users' private documents
never appear, not even by name); active rows are re-checked with the same
filter so stale relations never leak private name/metadata. Only metadata
(name, owner badge, public/private, added time) renders here — download
stays behind the protected task8 download view with its own fresh check
(a knowledge entry alone never grants download of a private document).
All roles may read; only owner/manager get add/remove forms, and the
backend denies worker/viewer regardless of template hiding.
Entries are management only: the page states they do not feed any
decision-making, and no planning/chat/advice code reads FarmDocument.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .farm_knowledge import (
    activate_document,
    deactivate_document,
    fresh_document,
    is_eligible,
)
from .models import Document, FarmDocument
from .permissions import get_active_farm, role_for


@login_required
def knowledge_list(request):
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    marketplace = Document.objects.filter(
        Q(owner_id=farm.owner_id) | Q(is_public=True)).select_related(
        "owner").order_by("-updated_at", "-id")
    actives = [a for a in FarmDocument.objects.filter(
        farm=farm, is_active=True).select_related(
        "document", "document__owner"
    ).order_by("-updated_at", "-id") if is_eligible(a.document, farm)]
    return render(request, "portal/farm_knowledge_list.html", {
        "farm": farm,
        "actives": actives,
        "active_document_ids": {a.document_id for a in actives},
        "marketplace": marketplace,
        "can_manage": role in ("owner", "manager"),
    })


@login_required
@require_http_methods(["POST"])
def knowledge_add(request, pk):
    """Task9: idempotent install. Private docs of others -> 404, no row."""
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    activate_document(farm=farm, document=fresh_document(pk), actor=request.user)
    messages.success(request, _("Dokumentum hozzáadva."))
    return redirect("farm_knowledge_list")


@login_required
@require_http_methods(["POST"])
def knowledge_remove(request, pk):
    """Task9: idempotent uninstall; missing row is a no-op success."""
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    deactivate_document(farm=farm, document_id=pk, actor=request.user)
    messages.success(request, _("Dokumentum eltávolítva."))
    return redirect("farm_knowledge_list")
