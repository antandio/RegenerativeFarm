"""Task8 document views: owner-based library, protected download.

Visibility: own documents (private or public) plus other users' public
documents are readable; another user's private document resolves to 404
on every route. Editing is author-only. Only POST writes; GET never
mutates. File content is stored only and served back byte-identical as
an attachment, never executed nor sent to any LLM.
"""
import re
import urllib.parse

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .document_forms import DocumentForm
from .document_storage import (
    delete_storage_file,
    new_storage_name,
    private_root,
    storage_path,
    write_new_file,
)
from .models import Document


def _visible(user):
    return Document.objects.filter(Q(owner=user) | Q(is_public=True))


_FALLBACK_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _ascii_fallback(name, extension):
    """Legacy-parser-safe fallback: only [A-Za-z0-9._-], so no quote,
    semicolon or backslash can break out of the quoted-string for old
    parsers; the exact name travels in filename* (percent-encoded)."""
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    safe = _FALLBACK_UNSAFE.sub("_", ascii_name)[:80] or "document"
    return f"{safe}{extension}"


@login_required
def document_list(request):
    items = _visible(request.user).select_related("owner").order_by("-updated_at", "-id")
    return render(request, "portal/document_list.html", {"items": items})


@login_required
def document_detail(request, pk):
    obj = get_object_or_404(_visible(request.user).select_related("owner"), pk=pk)
    ctx = {"obj": obj}
    try:
        from .markdown_preview import is_previewable_extension, load_markdown_preview
        ctx["show_markdown_preview"] = is_previewable_extension(obj.extension)
        if ctx["show_markdown_preview"]:
            preview = load_markdown_preview(obj.storage_name)
            ctx["markdown_preview_available"] = bool(preview.get("available"))
            ctx["markdown_preview_html"] = preview.get("html", "")
            ctx["markdown_preview_truncated"] = bool(preview.get("truncated"))
        else:
            ctx["markdown_preview_available"] = False
            ctx["markdown_preview_html"] = ""
            ctx["markdown_preview_truncated"] = False
    except Exception:
        ctx["show_markdown_preview"] = False
        ctx["markdown_preview_available"] = False
        ctx["markdown_preview_html"] = ""
        ctx["markdown_preview_truncated"] = False
    try:
        from .farm_knowledge import is_eligible
        from .models import FarmDocument
        from .permissions import get_active_farm, role_for
        farm = get_active_farm(request)
        if farm is not None and role_for(request.user, farm) is not None:
            ctx["knowledge_farm"] = farm
            ctx["knowledge_can_manage"] = role_for(request.user, farm) in (
                "owner", "manager")
            ctx["knowledge_is_active"] = FarmDocument.objects.filter(
                farm=farm, document=obj, is_active=True).exists()
            ctx["knowledge_eligible"] = is_eligible(obj, farm)
    except Exception:
        pass
    resp = render(request, "portal/document_detail.html", ctx)
    resp["Cache-Control"] = "private, no-store"
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def document_create(request):
    if request.method == "POST":
        form = DocumentForm(request.POST, request.FILES, require_upload=True)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen dokumentumadatok."))
            return render(request, "portal/document_form.html", {"form": form}, status=400)
        info = form._validated_upload
        storage_name = new_storage_name(info["extension"])
        write_new_file(storage_name, info["data"])
        try:
            with transaction.atomic():
                obj = Document(
                    owner=request.user,
                    name=form.cleaned_data["name"].strip(),
                    description=form.cleaned_data.get("description") or "",
                    original_filename=info["original_filename"],
                    storage_name=storage_name,
                    extension=info["extension"],
                    content_type=info["content_type"],
                    size_bytes=info["size_bytes"],
                    sha256=info["sha256"],
                    is_public=bool(form.cleaned_data.get("is_public")),
                )
                obj.save()
        except Exception:
            delete_storage_file(storage_name)
            raise
        messages.success(request, _("Dokumentum feltöltve."))
        return redirect("document_detail", pk=obj.pk)
    return render(request, "portal/document_form.html",
                  {"form": DocumentForm(require_upload=True)})


@login_required
@require_http_methods(["GET", "POST"])
def document_edit(request, pk):
    obj = get_object_or_404(Document, pk=pk, owner=request.user)
    if request.method == "POST":
        form = DocumentForm(request.POST, request.FILES, instance=obj, require_upload=False)
        if not form.is_valid():
            messages.error(request, _("Érvénytelen dokumentumadatok."))
            return render(request, "portal/document_form.html",
                          {"form": form, "obj": obj}, status=400)
        info = form._validated_upload
        if info is None:
            updated = form.save(commit=False)
            updated.owner = request.user
            updated.save()
            messages.success(request, _("Dokumentum frissítve."))
            return redirect("document_detail", pk=obj.pk)
        new_name = new_storage_name(info["extension"])
        write_new_file(new_name, info["data"])
        old_name = obj.storage_name
        try:
            with transaction.atomic():
                updated = form.save(commit=False)
                updated.owner = request.user
                updated.original_filename = info["original_filename"]
                updated.storage_name = new_name
                updated.extension = info["extension"]
                updated.content_type = info["content_type"]
                updated.size_bytes = info["size_bytes"]
                updated.sha256 = info["sha256"]
                updated.save()

                def _remove_old():
                    if old_name != new_name:
                        delete_storage_file(old_name)

                transaction.on_commit(_remove_old)
        except Exception:
            delete_storage_file(new_name)
            raise
        messages.success(request, _("Dokumentum frissítve."))
        return redirect("document_detail", pk=obj.pk)
    return render(request, "portal/document_form.html",
                  {"form": DocumentForm(instance=obj, require_upload=False), "obj": obj})


@login_required
def document_download(request, pk):
    obj = get_object_or_404(_visible(request.user), pk=pk)
    try:
        path = storage_path(obj.storage_name)
    except ValueError:
        raise Http404()
    if not path.is_file() or path.is_symlink():
        raise Http404()
    fh = open(path, "rb")
    quoted = urllib.parse.quote(
        obj.original_filename or f"document{obj.extension}", safe="")
    fallback = _ascii_fallback(obj.original_filename or "document", obj.extension)
    resp = FileResponse(fh, content_type=obj.content_type, as_attachment=True,
                        filename=fallback)
    resp["Content-Disposition"] = (
        content_disposition_header(True, fallback)
        + f"; filename*=UTF-8''{quoted}"
    )
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def document_delete(request, pk):
    obj = get_object_or_404(Document, pk=pk, owner=request.user)
    if request.method == "POST":
        # Blob cleanup is the shared post_delete signal (on_commit-safe);
        # no duplicate scheduling here.
        obj.delete()
        messages.success(request, _("Dokumentum törölve."))
        return redirect("document_list")
    return render(request, "portal/document_confirm_delete.html", {"obj": obj})
