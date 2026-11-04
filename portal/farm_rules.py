"""Task7 service: farm activation of rule definitions (stored text only).

Safe durable eligibility (farm-based, installer-independent): rule R may
be installed on farm F iff R.owner_id == F.owner_id OR R is currently
public. Consequence: a manager's privately authored rule canNOT be
installed on a managed farm until the author makes it public — private
author rules activate only on the author's OWN farm. Every read uses
fresh DB state, so a public->private flip immediately hides name/body/
category on all non-author farms. No snapshot columns: author
category/body edits render live. Nothing here executes rule text or
contacts any LLM.
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404

from .models import RuleActivation, RuleDefinition
from .permissions import role_for


def fresh_rule(pk):
    """Fresh rule row or Http404 (never a stale cached instance)."""
    try:
        return RuleDefinition.objects.select_related(
            "category", "owner").get(pk=pk)
    except RuleDefinition.DoesNotExist:
        raise Http404()


def is_eligible(rule, farm):
    """Fresh eligibility: author-owns-farm OR currently public."""
    return rule.owner_id == farm.owner_id or bool(rule.is_public)


def require_manager(user, farm):
    """Fresh role check: only owner/manager may (de)activate. No session cache."""
    if role_for(user, farm) not in ("owner", "manager"):
        raise PermissionDenied()


def activate_rule(*, farm, rule, actor):
    """Idempotent activation: unique(farm, rule), explicit is_active=True.

    The passed rule may be stale (loaded before a concurrent
    public->private save). Eligibility is therefore re-checked on the
    locked fresh row INSIDE the same transaction, never on the passed
    object. Permission (fresh active-farm role) is checked per request.
    """
    require_manager(actor, farm)
    rule_id = rule.pk if hasattr(rule, "pk") else rule
    with transaction.atomic():
        try:
            locked = RuleDefinition.objects.select_for_update().get(pk=rule_id)
        except RuleDefinition.DoesNotExist:
            raise Http404()
        if not is_eligible(locked, farm):
            raise Http404()
        act, _ = RuleActivation.objects.select_for_update().get_or_create(
            farm=farm, rule=locked,
            defaults={"activated_by": actor, "is_active": True})
        if not act.is_active or act.activated_by_id != actor.pk:
            act.is_active = True
            act.activated_by = actor
            act.save(update_fields=["is_active", "activated_by", "updated_at"])
    return act


def deactivate_rule(*, farm, rule_id, actor):
    """Idempotent deactivation; missing row is a no-op (still success)."""
    require_manager(actor, farm)
    with transaction.atomic():
        act = RuleActivation.objects.select_for_update().filter(
            farm=farm, rule_id=rule_id).first()
        if act is not None and act.is_active:
            act.is_active = False
            act.save(update_fields=["is_active", "updated_at"])
    return act


def revoke_other_farm_installs(rule):
    """Public->private: atomically deactivate ALL non-author-farm installs.

    Single UPDATE (atomic). Rows are kept is_active=False so re-publication
    never auto-reactivates. The author's OWN farm installs are untouched.
    """
    with transaction.atomic():
        return RuleActivation.objects.filter(
            rule=rule, is_active=True).exclude(
            farm__owner_id=rule.owner_id).update(is_active=False)
