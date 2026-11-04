from django import forms
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import Farm, FarmAction, FarmExtraField, Membership
User = get_user_model()

class RegisterForm(forms.Form):
    email = forms.EmailField(label=_("E-mail cím"), max_length=254)
    password = forms.CharField(label=_("Jelszó"), widget=forms.PasswordInput, min_length=10, max_length=128)
    password2 = forms.CharField(label=_("Jelszó újra"), widget=forms.PasswordInput, max_length=128)
    accept_terms = forms.BooleanField(label=_("Elfogadom az ÁSZF-et és az adatkezelési tájékoztatót."), required=True)
    cloud_consent = forms.BooleanField(label=_("Hozzájárulok, hogy profiladataimat tanácsadás céljából a modellszolgáltató felhőjébe küldjék. (Később visszavonható.)"), required=False)
    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(_("Ezzel az e-mail címmel már létezik fiók."))
        return email
    def clean(self):
        cd = super().clean()
        p1, p2 = cd.get("password"), cd.get("password2")
        if p1 and p2 and p1 != p2:
            raise ValidationError(_("A két jelszó nem egyezik."))
        if p1:
            try:
                validate_password(p1)
            except ValidationError as e:
                raise ValidationError({"password": e.messages})
        return cd

class LoginForm(forms.Form):
    email = forms.EmailField(label=_("E-mail cím"))
    password = forms.CharField(label=_("Jelszó"), widget=forms.PasswordInput)

class FarmForm(forms.ModelForm):
    class Meta:
        model = Farm
        fields = ["name","region_text","climate_text","farming_direction_text","soil_notes","water_notes","crops_livestock_notes","equipment_labour_notes","goals_notes","constraints_notes"]
        labels = {"name":_("Gazdaság neve"),"region_text":_("Régió, tájegység (saját szavakkal)"),"climate_text":_("Éghajlati jellemzés"),"farming_direction_text":_("Fő gazdálkodási irány"),"soil_notes":_("Talaj és saját megfigyelések"),"water_notes":_("Vízellátás módja és korlátai"),"crops_livestock_notes":_("Növények, vetésszerkezet, állatállomány"),"equipment_labour_notes":_("Gépek, bérmunka, munkaerő, időkeret"),"goals_notes":_("Célok"),"constraints_notes":_("Korlátok, költségvetés nagysága, kockázat, bizonytalanságok")}
        help_texts = {"name": _("Adja meg gazdasága nevét. Nem kötelező valós földrajzi név; példa: Tanyám."),
            "region_text": _("Írja le saját szavaival, hol gazdálkodik. Opcionális; példa: Duna–Tisza köze."),
            "climate_text": _("Éghajlati jellemzés saját szavakkal. Opcionális; példa: aszályos nyarak."),
            "farming_direction_text": _("Fő gazdálkodási irány. Opcionális; példa: szántó + kis állatállomány."),
            "soil_notes": _("Saját megfigyelések, nem laboratóriumi adat. Opcionális."),
            "water_notes": _("Vízellátás módja és korlátai. Opcionális; példa: fúrt kút, öntözés nincs."),
            "crops_livestock_notes": _("Növények, vetésszerkezet, állatállomány. Opcionális."),
            "equipment_labour_notes": _("Gépek, bérmunka, munkaerő, időkeret. Opcionális; időkeret órában, gépkapacitás megadható."),
            "goals_notes": _("Célok saját szavakkal. Opcionális."),
            "constraints_notes": _("Korlátok, költségvetés nagysága, kockázat, bizonytalanságok. Opcionális; terület hektárban, költségvetés helyi pénznemben megadható.")}
        widgets = {"name": forms.Textarea(attrs={"rows":1, "maxlength": "120"}),
            "region_text": forms.Textarea(attrs={"rows":2, "maxlength": "200"}),
            "climate_text": forms.Textarea(attrs={"rows":2, "maxlength": "500"}),
            "farming_direction_text": forms.Textarea(attrs={"rows":2, "maxlength": "300"}),
            "soil_notes": forms.Textarea(attrs={"rows":3}),"water_notes": forms.Textarea(attrs={"rows":3}),"crops_livestock_notes": forms.Textarea(attrs={"rows":3}),"equipment_labour_notes": forms.Textarea(attrs={"rows":3}),"goals_notes": forms.Textarea(attrs={"rows":3}),"constraints_notes": forms.Textarea(attrs={"rows":3})}
    def clean_name(self):
        n = (self.cleaned_data.get("name") or "").strip()
        if not n:
            raise ValidationError(_("A gazdaság neve kötelező."))
        return n
    def clean(self):
        cd = super().clean()
        for f in ["soil_notes","water_notes","crops_livestock_notes","equipment_labour_notes","goals_notes","constraints_notes"]:
            v = cd.get(f)
            if v and len(v) > 4000:
                raise ValidationError({f: _("Legfeljebb 4000 karakter.")})
            if v is not None and not str(v).strip():
                cd[f] = None
        for f in ["region_text","climate_text","farming_direction_text"]:
            v = cd.get(f)
            if v is not None and not str(v).strip():
                cd[f] = None
        return cd

class FarmExtraFieldForm(forms.ModelForm):
    class Meta:
        model = FarmExtraField
        fields = ["key", "value"]
        labels = {"key": _("Megnevezés"), "value": _("Érték")}
        help_texts = {"key": _("Rövid, egyedi megnevezés (legfeljebb 64 karakter)."),
                      "value": _("A mező értéke (legfeljebb 500 karakter).")}
        widgets = {"key": forms.TextInput(attrs={"maxlength": "64"}),
                   "value": forms.TextInput(attrs={"maxlength": "500"})}

    def clean_key(self):
        k = (self.cleaned_data.get("key") or "").strip()
        if not k:
            raise ValidationError(_("A megnevezés kötelező."))
        return k

    def clean_value(self):
        return (self.cleaned_data.get("value") or "").strip()


class BaseFarmExtraFormSet(forms.BaseInlineFormSet):
    """Friendly cross-row duplicate check (stripped, case-insensitive,
    ignoring rows marked for deletion)."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        seen = {}
        for form in self.forms:
            cd = getattr(form, "cleaned_data", None)
            if not cd or cd.get("DELETE"):
                continue
            k = cd.get("key") or ""
            if not k:
                continue
            norm = k.strip().casefold()
            if norm in seen:
                form.add_error("key", _("Ez a megnevezés már szerepel."))
            else:
                seen[norm] = form


FarmExtraFormSet = forms.inlineformset_factory(
    Farm, FarmExtraField, form=FarmExtraFieldForm, formset=BaseFarmExtraFormSet,
    fields=["key", "value"], extra=1, can_delete=True,
    max_num=20, validate_max=True, absolute_max=40,
)


class ActionForm(forms.ModelForm):
    class Meta:
        model = FarmAction
        fields = ["text"]
        labels = {"text": _("Teendő szövege")}
        widgets = {"text": forms.Textarea(attrs={"rows":3})}
    def clean_text(self):
        t = (self.cleaned_data.get("text") or "").strip()
        if not t or len(t) > 2000:
            raise ValidationError(_("A teendő 1–2000 karakter legyen."))
        return t


class InviteForm(forms.Form):
    email = forms.EmailField(max_length=254, label=_("E-mail cím"))
    role = forms.ChoiceField(choices=Membership.ROLE_CHOICES, label=_("Szerep"))
