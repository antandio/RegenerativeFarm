"""Task9 service: farm knowledge-base installs of documents (managed only).

Safe durable eligibility (farm-based, installer-independent): document D may
be installed on farm F iff D.owner_id == F.owner_id OR D is currently
public. Consequence: a manager's privately authored document canNOT be
installed on a managed farm until the author makes it public — private
author documents install only on the author's OWN farm. Every read uses
fresh DB state, so a public->private flip immediately hides name/metadata/
download on all non-author farms. No snapshot columns: author metadata
edits render live. Nothing here executes document content or contacts any
LLM; entries never feed plans/chat/advice (management only).
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404

from .models import Document, FarmDocument
from .permissions import role_for


def fresh_document(pk):
    """Fresh document row or Http404 (never a stale cached instance)."""
    try:
        return Document.objects.select_related("owner").get(pk=pk)
    except Document.DoesNotExist:
        raise Http404()


def is_eligible(document, farm):
    """Fresh eligibility: author-owns-farm OR currently public."""
    return document.owner_id == farm.owner_id or bool(document.is_public)


def require_manager(user, farm):
    """Fresh role check: only owner/manager may (de)activate. No session cache."""
    if role_for(user, farm) not in ("owner", "manager"):
        raise PermissionDenied()


def activate_document(*, farm, document, actor):
    """Idempotent activation: unique(farm, document), explicit is_active=True.

    The passed document may be stale (loaded before a concurrent
    public->private save). Eligibility is therefore re-checked on the
    locked fresh row INSIDE the same transaction, never on the passed
    object. Permission (fresh active-farm role) is checked per request.
    Relies on the global SQLite IMMEDIATE writer serialization.
    """
    require_manager(actor, farm)
    document_id = document.pk if hasattr(document, "pk") else document
    with transaction.atomic():
        try:
            locked = Document.objects.select_for_update().get(pk=document_id)
        except Document.DoesNotExist:
            raise Http404()
        if not is_eligible(locked, farm):
            raise Http404()
        act, _ = FarmDocument.objects.select_for_update().get_or_create(
            farm=farm, document=locked,
            defaults={"added_by": actor, "is_active": True})
        if not act.is_active or act.added_by_id != actor.pk:
            act.is_active = True
            act.added_by = actor
            act.save(update_fields=["is_active", "added_by", "updated_at"])
    return act


def deactivate_document(*, farm, document_id, actor):
    """Idempotent deactivation; missing row is a no-op (still success)."""
    require_manager(actor, farm)
    with transaction.atomic():
        act = FarmDocument.objects.select_for_update().filter(
            farm=farm, document_id=document_id).first()
        if act is not None and act.is_active:
            act.is_active = False
            act.save(update_fields=["is_active", "updated_at"])
    return act


def revoke_other_farm_installs(document):
    """Public->private: atomically deactivate ALL non-author-farm installs.

    Single UPDATE (atomic). Rows are kept is_active=False so re-publication
    never auto-reactivates. The author's OWN farm installs are untouched.
    """
    with transaction.atomic():
        return FarmDocument.objects.filter(
            document=document, is_active=True).exclude(
            farm__owner_id=document.owner_id).update(is_active=False)
