"""Task10 news-source forms: owner collection metadata, server-side URL check."""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import NewsSource


class NewsSourceForm(forms.ModelForm):
    class Meta:
        model = NewsSource
        fields = ["name", "url", "notes", "is_active"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "name": _("Portál neve"),
            "url": _("Portál URL-je"),
            "notes": _("Jegyzet"),
            "is_active": _("Aktív"),
        }
        help_texts = {
            "url": _("Csak nyilvános http(s) cím, például https://example.com/feed."),
            "notes": _("Rövid jegyzet a portálról."),
            "is_active": _("Kikapcsolva a portál a gyűjteményben marad, de inaktív."),
        }

    def clean_url(self):
        # Model.clean() normalizes again on save; returning the canonical
        # form here lets validate_unique() dedupe on the stored value.
        from .news_url import normalize_news_url
        return normalize_news_url(self.cleaned_data.get("url"))
