"""Task6 rule views: user-owned (never farm-scoped) rule definitions.

Visibility: own rules (private or public) plus other users' public
rules are readable; another user's private rule resolves to 404 on
every route. Editing is author-only: get_object_or_404 filters by
owner=request.user, so managers/members get 404 on others' rules.
Only POST writes; GET never mutates. Bodies are stored text rendered
escaped (Django autoescape + pre-wrap CSS), never executed or sent
to any LLM.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .domain_forms import RuleCategoryForm, RuleDefinitionForm
from .models import RuleCategory, RuleDefinition


def _visible(user):
    return RuleDefinition.objects.filter(
        Q(owner=user) | Q(is_public=True)).select_related("category")


def _own_category(user, name):
    obj, _ = RuleCategory.objects.get_or_create(owner=user, name=name)
    return obj


@login_required
def rule_list(request):
    items = _visible(request.user).order_by("-updated_at", "-id")
    return render(request, "portal/rule_list.html", {"items": items})


@login_required
def rule_detail(request, pk):
    obj = get_object_or_404(_visible(request.user), pk=pk)
    return render(request, "portal/rule_detail.html", {"obj": obj})


@login_required
@require_http_methods(["GET", "POST"])
def rule_create(request):
    if request.method == "POST":
        form = RuleDefinitionForm(request.POST, user=request.user)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen szabályadatok."))
            return render(request, "portal/rule_form.html",
                          {"form": form}, status=400)
        obj = form.save(commit=False)
        obj.owner = request.user
        new_name = (form.cleaned_data.get("new_category") or "").strip()
        if new_name:
            obj.category = _own_category(request.user, new_name)
        obj.save()
        messages.success(request, _("Szabályleírás létrehozva."))
        return redirect("rule_detail", pk=obj.pk)
    return render(request, "portal/rule_form.html",
                  {"form": RuleDefinitionForm(user=request.user)})


@login_required
@require_http_methods(["GET", "POST"])
def rule_edit(request, pk):
    obj = get_object_or_404(RuleDefinition, pk=pk, owner=request.user)
    if request.method == "POST":
        # Message-only pre-edit value: form.save(commit=False) mutates
        # obj in place, so the old is_public is read before binding.
        # The public->private revoke itself lives in
        # RuleDefinition.save() (single atomic transaction shared by
        # every save path); the view must not duplicate it.
        was_public = RuleDefinition.objects.values_list(
            "is_public", flat=True).get(pk=obj.pk)
        form = RuleDefinitionForm(request.POST, instance=obj, user=request.user)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen szabályadatok."))
            return render(request, "portal/rule_form.html",
                          {"form": form, "obj": obj}, status=400)
        updated = form.save(commit=False)
        updated.owner = request.user
        new_name = (form.cleaned_data.get("new_category") or "").strip()
        if new_name:
            updated.category = _own_category(request.user, new_name)
        updated.save()
        if was_public and not updated.is_public:
            messages.info(request, _(
                "A szabály priváttá vált; más farmok telepítései deaktiválva."))
        messages.success(request, _("Szabályleírás frissítve."))
        return redirect("rule_detail", pk=obj.pk)
    return render(request, "portal/rule_form.html",
                  {"form": RuleDefinitionForm(instance=obj, user=request.user),
                   "obj": obj})


@login_required
def farm_rule_list(request):
    """Task7: active installs + marketplace for the active farm.

    Reads use fresh DB state on every request: eligible marketplace rows
    are author-of-farm-owner OR currently public (other users' private
    rules never appear, not even by name); active rows re-checked with
    the same filter so stale relations never leak private name/body/
    category. Bodies render escaped; text is never executed nor sent to
    any LLM. All roles may read; only owner/manager get POST forms, and
    the backend denies worker/viewer regardless of template hiding.
    """
    from .farm_rules import is_eligible
    from .models import RuleActivation
    from .permissions import get_active_farm, role_for
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    role = role_for(request.user, farm)
    if role is None:
        raise Http404()
    marketplace = RuleDefinition.objects.filter(
        Q(owner_id=farm.owner_id) | Q(is_public=True)).select_related(
        "category", "owner").order_by("-updated_at", "-id")
    actives = [a for a in RuleActivation.objects.filter(
        farm=farm, is_active=True).select_related(
        "rule", "rule__category", "rule__owner"
    ).order_by("-updated_at", "-id") if is_eligible(a.rule, farm)]
    return render(request, "portal/farm_rule_list.html", {
        "farm": farm,
        "actives": actives,
        "active_rule_ids": {a.rule_id for a in actives},
        "marketplace": marketplace,
        "can_manage": role in ("owner", "manager"),
    })


@login_required
@require_http_methods(["POST"])
def farm_rule_activate(request, pk):
    """Task7: idempotent install. Private rules of others -> 404, no row."""
    from .farm_rules import activate_rule, fresh_rule
    from .permissions import get_active_farm
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    activate_rule(farm=farm, rule=fresh_rule(pk), actor=request.user)
    messages.success(request, _("Szabály aktiválva."))
    return redirect("farm_rule_list")


@login_required
@require_http_methods(["POST"])
def farm_rule_deactivate(request, pk):
    """Task7: idempotent uninstall; missing row is a no-op success."""
    from .farm_rules import deactivate_rule
    from .permissions import get_active_farm
    farm = get_active_farm(request)
    if farm is None:
        raise Http404()
    deactivate_rule(farm=farm, rule_id=pk, actor=request.user)
    messages.success(request, _("Szabály deaktiválva."))
    return redirect("farm_rule_list")


@login_required
@require_http_methods(["GET", "POST"])
def category_create(request):
    if request.method == "POST":
        form = RuleCategoryForm(request.POST, owner=request.user)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen kategóriaadatok."))
            return render(request, "portal/rule_category_form.html",
                          {"form": form}, status=400)
        cat = form.save(commit=False)
        cat.owner = request.user
        cat.save()
        messages.success(request, _("Kategória létrehozva."))
        return redirect("rule_new")
    return render(request, "portal/rule_category_form.html",
                  {"form": RuleCategoryForm(owner=request.user)})
