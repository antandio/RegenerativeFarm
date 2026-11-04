"""Task1: authenticated read-only view of the canonical regenerative policy."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .regenerative_policy import (
    POLICY_VERSION, policy_sha256, policy_text, render_safe_markdown,
)


@login_required
@require_GET
def regenerative_guide_view(request):
    # Read-only: GET only, no edit UI or POST endpoint; the canonical
    # English source renders sanitized and unchanged, checksum visible.
    return render(request, "portal/regenerative_guide.html", {
        "guide_html": render_safe_markdown(policy_text()),
        "policy_version": POLICY_VERSION,
        "policy_sha256": policy_sha256(),
    })
