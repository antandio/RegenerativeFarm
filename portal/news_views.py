"""Task10 news-source views: owner-only collection CRUD.

The collection is user-owned, never farm-bound: every route scopes by
``owner=request.user`` (other users' rows resolve to 404, never leak in
list/detail/URLs/errors) and works without any farm or membership.
Only POST writes; GET never mutates (the active toggle accepts POST
only). Stored URLs are never fetched, embedded, or sent anywhere — the
list/detail pages state that the future daily scan is not running.
External links open with ``rel="noopener noreferrer" target="_blank"``
and all user content is template-escaped.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from django.db import IntegrityError, transaction

from .models import NewsSource
from .news_forms import NewsSourceForm


def _save_or_duplicate_error(request, form, obj, template, extra):
    """Save under atomic guard; a lost unique race becomes a 400 field error."""
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError:
        form.add_error("url", _("Ilyen URL már szerepel a gyűjteményedben."))
        messages.error(request, _("Érvénytelen hírforrás-adatok."))
        return render(request, template, {"form": form, **extra}, status=400)
    return None


@login_required
def news_source_list(request):
    items = NewsSource.objects.filter(owner=request.user).order_by("name", "id")
    return render(request, "portal/news_source_list.html", {"items": items})


@login_required
def news_source_detail(request, pk):
    obj = get_object_or_404(NewsSource, pk=pk, owner=request.user)
    return render(request, "portal/news_source_detail.html", {"obj": obj})


@login_required
@require_http_methods(["GET", "POST"])
def news_source_create(request):
    if request.method == "POST":
        form = NewsSourceForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen hírforrás-adatok."))
            return render(request, "portal/news_source_form.html",
                          {"form": form}, status=400)
        obj = form.save(commit=False)
        obj.owner = request.user
        failed = _save_or_duplicate_error(
            request, form, obj, "portal/news_source_form.html", {})
        if failed is not None:
            return failed
        messages.success(request, _("Hírforrás felvéve."))
        return redirect("news_source_detail", pk=obj.pk)
    return render(request, "portal/news_source_form.html",
                  {"form": NewsSourceForm(initial={"is_active": True})})


@login_required
@require_http_methods(["GET", "POST"])
def news_source_edit(request, pk):
    obj = get_object_or_404(NewsSource, pk=pk, owner=request.user)
    if request.method == "POST":
        form = NewsSourceForm(request.POST, instance=obj)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen hírforrás-adatok."))
            return render(request, "portal/news_source_form.html",
                          {"form": form, "obj": obj}, status=400)
        updated = form.save(commit=False)
        updated.owner = request.user
        failed = _save_or_duplicate_error(
            request, form, updated, "portal/news_source_form.html", {"obj": obj})
        if failed is not None:
            return failed
        messages.success(request, _("Hírforrás frissítve."))
        return redirect("news_source_detail", pk=obj.pk)
    return render(request, "portal/news_source_form.html",
                  {"form": NewsSourceForm(instance=obj), "obj": obj})


@login_required
@require_http_methods(["POST"])
def news_source_toggle(request, pk):
    obj = get_object_or_404(NewsSource, pk=pk, owner=request.user)
    obj.is_active = not obj.is_active
    obj.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request, _("Hírforrás aktiválva.") if obj.is_active
        else _("Hírforrás inaktiválva."))
    return redirect("news_source_list")


@login_required
@require_http_methods(["GET", "POST"])
def news_source_delete(request, pk):
    obj = get_object_or_404(NewsSource, pk=pk, owner=request.user)
    if request.method == "POST":
        obj.delete()
        messages.success(request, _("Hírforrás törölve."))
        return redirect("news_source_list")
    return render(request, "portal/news_source_confirm_delete.html", {"obj": obj})
