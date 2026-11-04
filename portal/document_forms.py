"""Task8 document forms: metadata + server-side file validation."""
from django import forms
from django.utils.translation import gettext_lazy as _

from .document_storage import validate_upload
from .models import Document


class DocumentForm(forms.ModelForm):
    upload = forms.FileField(
        required=False,
        label=_("Fájl"),
        help_text=_("PDF, TXT, MD, CSV, DOCX, legfeljebb 10 MiB."),
    )

    class Meta:
        model = Document
        fields = ["name", "description", "is_public"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {
            "name": _("Dokumentum neve"),
            "description": _("Leírás"),
            "is_public": _("Nyilvános mások számára"),
        }
        help_texts = {
            "description": _("Rövid leírás a dokumentumról."),
        }

    def __init__(self, *a, require_upload=False, **kw):
        super().__init__(*a, **kw)
        self.require_upload = require_upload
        self.fields["upload"].required = require_upload
        self._validated_upload = None

    def clean_upload(self):
        up = self.cleaned_data.get("upload")
        if up is None:
            if self.require_upload:
                raise forms.ValidationError(_("A fájl kötelező."))
            return None
        info = validate_upload(up)
        self._validated_upload = info
        return up

    def clean(self):
        cleaned = super().clean()
        if self.require_upload and self._validated_upload is None and not self.errors.get("upload"):
            raise forms.ValidationError(_("A fájl kötelező."))
        return cleaned
