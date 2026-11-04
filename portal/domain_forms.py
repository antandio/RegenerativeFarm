"""Stage 2 forms: farm-scoped querysets, explicit through-save for equipment M2M."""
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from .domain_models import FarmLocation, FarmProperty, Equipment, EquipmentProperty, CropCycle, FarmingEvent, LivestockGroup, LivestockEvent, WorkItem, ExternalContact, RuleCategory, RuleDefinition, RULE_BODY_MAX_LENGTH, EQUIPMENT_CAPABILITY_CHOICES, PROPERTY_KIND_CHOICES, CROP_STATE_CHOICES, LIVESTOCK_WORK_KINDS


class ExternalContactForm(forms.ModelForm):
    """Task5: phone OR email enforced by the model; services free lines."""
    class Meta:
        model = ExternalContact
        fields = ["name", "phone", "email", "services", "notes", "availability"]
        widgets = {
            "services": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "name": _("Név"),
            "phone": _("Telefonszám"),
            "email": _("E-mail"),
            "services": _("Kérhető munkák"),
            "notes": _("Rövid leírás"),
            "availability": _("Elérhetőség (pl. mikor hívható)"),
        }
        help_texts = {
            "services": _("Soronként egy munka (pl. szántás, vetés)."),
        }

class LocationForm(forms.ModelForm):
    class Meta:
        model = FarmLocation
        fields = ["name", "latitude", "longitude", "timezone"]
        labels = {"name": _("Helyszín neve"), "latitude": _("Szélesség"), "longitude": _("Hosszúság"), "timezone": _("Időzóna")}

class PropertyForm(forms.ModelForm):
    boundary_geojson = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}),
        label=_("Határvonal (GeoJSON)"),
        help_text=_("Egy zárt Polygon gyűrű WGS84-ben, max. 200 pont, 32 KiB. Üresen hagyva nincs polygon."))
    point_latitude = forms.CharField(required=False, label=_("Pont szélesség"))
    point_longitude = forms.CharField(required=False, label=_("Pont hosszúság"))
    class Meta:
        model = FarmProperty
        fields = ["location", "name", "kind", "area_hectares", "notes",
                  "boundary_geojson", "point_latitude", "point_longitude"]
        labels = {"location": _("Helyszín"), "name": _("Terület neve"), "kind": _("Típus"), "area_hectares": _("Terület (ha)"), "notes": _("Megjegyzés")}
    def __init__(self, *a, farm=None, **kw):
        super().__init__(*a, **kw)
        self.farm = farm
        if farm is not None:
            self.fields["location"].queryset = FarmLocation.objects.filter(farm=farm)
        inst = getattr(self, "instance", None)
        if inst is not None and getattr(inst, "boundary_geojson", None):
            import json as _json
            try:
                self.initial["boundary_geojson"] = _json.dumps(inst.boundary_geojson)
            except Exception:
                pass
    def clean_boundary_geojson(self):
        import json as _json
        from .geometry import MAX_RAW_BYTES, validate_polygon_geojson, _reject_constant
        raw = (self.cleaned_data.get("boundary_geojson") or "").strip()
        if not raw:
            return None
        if len(raw.encode("utf-8")) > MAX_RAW_BYTES:
            raise ValidationError(_("A polygon JSON túl nagy (max. 32 KiB)."))
        try:
            val = _json.loads(raw, parse_constant=_reject_constant)
        except ValidationError:
            raise
        except Exception:
            raise ValidationError(_("Érvénytelen polygon GeoJSON."))
        validate_polygon_geojson(val)
        return val
    def clean(self):
        from .geometry import validate_property_point
        cleaned = super().clean()
        lat, lon = cleaned.get("point_latitude"), cleaned.get("point_longitude")
        lat_e = lat is None or (isinstance(lat, str) and not lat.strip())
        lon_e = lon is None or (isinstance(lon, str) and not lon.strip())
        if lat_e and lon_e:
            cleaned["point_latitude"] = cleaned["point_longitude"] = None
        else:
            lat_v, lon_v = validate_property_point("" if lat_e else lat, "" if lon_e else lon)
            cleaned["point_latitude"], cleaned["point_longitude"] = lat_v, lon_v
        return cleaned
    def clean_location(self):
        loc = self.cleaned_data.get("location")
        if loc is not None and self.farm is not None and loc.farm_id != self.farm.id:
            raise ValidationError(_("A helyszín csak azonos farmhoz tartozhat."))
        return loc

class EquipmentForm(forms.ModelForm):
    capabilities = forms.MultipleChoiceField(choices=EQUIPMENT_CAPABILITY_CHOICES, required=False, label=_("Képességek"))
    properties = forms.ModelMultipleChoiceField(queryset=FarmProperty.objects.none(), required=False, label=_("Területek"))
    class Meta:
        model = Equipment
        fields = ["name", "capabilities", "available", "hours_per_day", "properties"]
        labels = {"name": _("Gép neve"), "available": _("Elérhető"), "hours_per_day": _("Napi óraszám")}
    def __init__(self, *a, farm=None, **kw):
        super().__init__(*a, **kw)
        self.farm = farm
        if farm is not None:
            self.fields["properties"].queryset = FarmProperty.objects.filter(farm=farm)
        if self.instance and self.instance.pk:
            try:
                caps = self.instance.capabilities or []
                self.initial["capabilities"] = list(caps)
                self.initial["properties"] = list(self.instance.properties.values_list("pk", flat=True))
            except Exception:
                pass
    def clean_capabilities(self):
        return list(self.cleaned_data.get("capabilities") or [])
    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.capabilities = list(self.cleaned_data.get("capabilities") or [])
        props = list(self.cleaned_data.get("properties") or [])
        if commit:
            obj.save()
            self._save_m2m_through(obj, props)
        else:
            self._pending_props = props
        return obj
    def save_m2m(self):
        obj = self.instance
        props = getattr(self, "_pending_props", list(self.cleaned_data.get("properties") or []))
        self._save_m2m_through(obj, props)
        super().save_m2m() if hasattr(super(), "save_m2m") else None
    def _save_m2m_through(self, obj, props):
        want = {p.pk for p in props}
        have = set(obj.properties.values_list("pk", flat=True)) if obj.pk else set()
        for p in props:
            if p.farm_id != obj.farm_id:
                raise ValidationError(_("A géphez csak azonos farm területe rendelhető."))
        EquipmentProperty.objects.filter(equipment=obj).exclude(property_id__in=want).delete()
        for pid in want - have:
            EquipmentProperty.objects.create(equipment=obj, property_id=pid)

class CropCycleForm(forms.ModelForm):
    """Manual plans only: create forces planned, edit locks existing state."""
    class Meta:
        model = CropCycle
        fields = ["property", "crop_name", "state", "growth_stage", "sow_date", "expected_end_date", "observations"]
        widgets = {"sow_date": forms.DateInput(attrs={"type": "date"}), "expected_end_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"property": _("Terület"), "crop_name": _("Növény neve"), "state": _("Állapot"), "growth_stage": _("Fejlődési szakasz"), "sow_date": _("Vetés dátuma"), "expected_end_date": _("Várható befejezés"), "observations": _("Megfigyelések")}
    def __init__(self, *a, farm=None, **kw):
        super().__init__(*a, **kw)
        self.farm = farm
        if farm is not None:
            self.fields["property"].queryset = FarmProperty.objects.filter(farm=farm)
        # Lock state against tampered POST: new rows are always planned.
        if self.instance is not None and getattr(self.instance, "pk", None):
            self.fields["state"].disabled = True
            self.fields["state"].initial = self.instance.state
        else:
            self.fields["state"].disabled = True
            self.fields["state"].initial = "planned"

    def clean_state(self):
        # Ignore POST; new rows become planned, edits keep existing state.
        if self.instance is not None and getattr(self.instance, "pk", None):
            return self.instance.state
        return "planned"

    def clean_sow_date(self):
        # Planned rows require a sowing date for matching, but past dates are
        # allowed so old unfulfilled plans can be corrected. Legacy
        # current/history keep optional dates and nulls keep loading.
        d = self.cleaned_data.get("sow_date")
        if self.instance is not None and getattr(self.instance, "pk", None):
            state = self.instance.state
        else:
            state = "planned"
        if state == "planned":
            if not d:
                raise ValidationError(_("A vetés dátuma kötelező."))
        return d

class LivestockGroupForm(forms.ModelForm):
    housing_property = forms.ModelChoiceField(
        queryset=FarmProperty.objects.none(), required=False, label=_("Szállás (terület)"))
    class Meta:
        model = LivestockGroup
        fields = ["name", "species", "breed_label", "headcount", "housing_property", "notes"]
        labels = {"name": _("Csoport neve"), "species": _("Faj"),
                  "breed_label": _("Fajta / egyedi megnevezés"), "headcount": _("Induló létszám"),
                  "notes": _("Megjegyzés")}
    def __init__(self, *a, farm=None, **kw):
        super().__init__(*a, **kw)
        self.farm = farm
        if farm is not None:
            self.fields["housing_property"].queryset = FarmProperty.objects.filter(farm=farm)
    def clean_housing_property(self):
        prop = self.cleaned_data.get("housing_property")
        if prop is not None and self.farm is not None and prop.farm_id != self.farm.id:
            raise ValidationError(_("A szállás csak azonos farm területén lehet."))
        return prop


class LivestockGroupEditForm(LivestockGroupForm):
    """Edit form: live headcount is create-only, never rewritten here.

    Count moves happen only through dated LivestockEvent rows, so the
    edit form carries no headcount field; the current count is shown
    read-only by the template from the edited object.
    """
    class Meta(LivestockGroupForm.Meta):
        fields = ["name", "species", "breed_label", "housing_property", "notes"]


class LivestockEventForm(forms.Form):
    event_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}), label=_("Dátum"))
    kind = forms.ChoiceField(choices=[], label=_("Eseménytípus"))
    headcount_delta = forms.IntegerField(label=_("Létszámváltozás"),
                                         help_text=_("Születés/vásárlás: pozitív; eladás/elhullás/átmozgatás: negatív; egészség/legeltetés/egyéb: 0."))
    target_group = forms.ModelChoiceField(
        queryset=LivestockGroup.objects.none(), required=False,
        label=_("Célcsoport (átmozgatásnál)"))
    property = forms.ModelChoiceField(
        queryset=FarmProperty.objects.none(), required=False,
        label=_("Terület (pl. legeltetés helye)"))
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                            label=_("Megjegyzés"))
    create_journal = forms.BooleanField(
        required=False, initial=True,
        label=_("Naplóbejegyzés készítése"),
        help_text=_("Egy „Egyéb” típusú tényleges naplóbejegyzés a gazdálkodási naplóba."))
    def __init__(self, *a, farm=None, group=None, **kw):
        from .domain_models import LIVESTOCK_EVENT_KINDS
        super().__init__(*a, **kw)
        self.farm = farm
        self.fields["kind"].choices = list(LIVESTOCK_EVENT_KINDS)
        if farm is not None:
            self.fields["target_group"].queryset = LivestockGroup.objects.filter(
                farm=farm, is_active=True)
            self.fields["property"].queryset = FarmProperty.objects.filter(farm=farm)
        if group is not None:
            self.fields["target_group"].queryset = LivestockGroup.objects.filter(
                farm=farm, is_active=True).exclude(pk=group.pk)


class LivestockWorkItemForm(forms.ModelForm):
    livestock_kind = forms.ChoiceField(choices=[], label=_("Munka típusa"))
    property = forms.ModelChoiceField(
        queryset=FarmProperty.objects.none(), required=False,
        label=_("Terület (pl. legelő)"))
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                            label=_("Megjegyzés"))
    class Meta:
        model = WorkItem
        fields = ["livestock_kind", "title", "start_date", "end_date",
                  "estimated_hours", "property", "notes"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"title": _("Munka megnevezése"), "start_date": _("Kezdés"),
                  "end_date": _("Befejezés"), "estimated_hours": _("Becsült óra")}
    def __init__(self, *a, farm=None, group=None, **kw):
        super().__init__(*a, **kw)
        self.farm = farm
        # Capability is fixed for animal work; preset before validation.
        self.instance.capability = "other"
        # Group is attached at save() in the view; preset before model
        # validation so _post_clean (full_clean) sees a group-bound item.
        if group is not None:
            self.instance.livestock_group = group
        self.fields["livestock_kind"].choices = list(LIVESTOCK_WORK_KINDS)
        if farm is not None:
            self.fields["property"].queryset = FarmProperty.objects.filter(farm=farm)
        if self.instance and self.instance.pk and self.instance.recommendation:
            self.fields["notes"].initial = self.instance.recommendation
    def clean_property(self):
        prop = self.cleaned_data.get("property")
        if prop is not None and self.farm is not None and prop.farm_id != self.farm.id:
            raise ValidationError(_("A terület csak azonos farmhoz tartozhat."))
        return prop
    def save(self, commit=True, group=None):
        obj = super().save(commit=False)
        if group is not None:
            obj.livestock_group = group
        obj.crop_cycle = None
        obj.capability = "other"
        obj.equipment = None
        obj.missing = False
        obj.recommendation = (self.cleaned_data.get("notes") or "")[:240]
        if commit:
            obj.save()
        return obj


class LivestockWorkDoneForm(forms.Form):
    event_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}), label=_("Elvégzés dátuma"))
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                            label=_("Megjegyzés"))


class ObservationForm(forms.Form):
    observation = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=2000, label=_("Megfigyelés"))
    def clean_observation(self):
        t = (self.cleaned_data.get("observation") or "").strip()
        if not t:
            raise forms.ValidationError(_("A megfigyelés kötelező."))
        return t


class FarmingEventForm(forms.ModelForm):
    crop_cycle = forms.ModelChoiceField(queryset=CropCycle.objects.none(), required=False, label=_("Vetésforgó"))
    class Meta:
        model = FarmingEvent
        fields = ["property", "crop_cycle", "crop_name_snapshot", "event_date", "event_type", "description", "quantity", "unit", "materials", "treated_area_hectares", "notes"]
        widgets = {"event_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"property": _("Terület"), "crop_name_snapshot": _("Vetett növény"), "event_date": _("Dátum"), "event_type": _("M\u0171velett\u00edpus"), "description": _("Le\u00edr\u00e1s"), "quantity": _("Mennyis\u00e9g"), "unit": _("M\u00e9rt\u00e9kegys\u00e9g"), "materials": _("Felhaszn\u00e1lt anyagok"), "treated_area_hectares": _("Kezelt ter\u00fclet (ha)"), "notes": _("Megjegyz\u00e9s")}
    def __init__(self, *a, farm=None, **kw):
        super().__init__(*a, **kw)
        self.farm = farm
        # Explicit crop label with guidance; sowing requires a crop name.
        self.fields["crop_name_snapshot"].help_text = _(
            "A ténylegesen vetett növény neve. Vetésnél kötelező.")
        if farm is not None:
            from django.db.models import Q as _Q
            self.fields["property"].queryset = FarmProperty.objects.filter(farm=farm)
            # Exclude soft-hidden plans, but retain the existing historical link.
            qs = CropCycle.objects.filter(farm=farm, is_hidden=False)
            linked = getattr(getattr(self, "instance", None), "crop_cycle_id", None)
            if linked:
                qs = CropCycle.objects.filter(farm=farm).filter(
                    _Q(is_hidden=False) | _Q(pk=linked))
            self.fields["crop_cycle"].queryset = qs
    def clean(self):
        # Sowing needs an entered crop or an explicit linked cycle to derive
        # from; explicit input wins. A missing POST key on edit preserves the
        # stored snapshot so legacy clients cannot blank history.
        cleaned = super().clean()
        inst = getattr(self, "instance", None)
        if (inst is not None and getattr(inst, "pk", None)
                and "crop_name_snapshot" not in (self.data or {})):
            cleaned["crop_name_snapshot"] = inst.crop_name_snapshot or ""
            return cleaned
        entered = (cleaned.get("crop_name_snapshot") or "").strip()
        link = cleaned.get("crop_cycle")
        if cleaned.get("event_type") == "sowing":
            if entered:
                cleaned["crop_name_snapshot"] = entered
            elif link is not None:
                cleaned["crop_name_snapshot"] = (link.crop_name or "").strip()
            else:
                raise forms.ValidationError(
                    _("Vetésnél a vetett növény megadása kötelező."))
        else:
            cleaned["crop_name_snapshot"] = entered
        return cleaned


class RuleCategoryForm(forms.ModelForm):
    """Task6: dedicated category create. Owner comes from the view."""
    class Meta:
        model = RuleCategory
        fields = ["name"]
        labels = {"name": _("Kategória neve")}
    def __init__(self, *a, owner=None, **kw):
        super().__init__(*a, **kw)
        self.owner = owner
    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError(_("A név kötelező."))
        if self.owner is not None and RuleCategory.objects.filter(owner=self.owner, name=name).exists():
            raise forms.ValidationError(_("Ilyen nevű kategóriád már van."))
        return name


class RuleDefinitionForm(forms.ModelForm):
    """Task6: choose own category or create one inline via new_category.

    No owner field by design: forged owner POST keys are ignored, the
    view always assigns request.user."""
    category = forms.ModelChoiceField(
        queryset=RuleCategory.objects.none(), required=False, label=_("Kategória"))
    new_category = forms.CharField(
        max_length=120, required=False, label=_("Új kategória"),
        help_text=_("Ha nincs megfelelő kategória, írd be az új nevét."))
    class Meta:
        model = RuleDefinition
        fields = ["name", "category", "body", "is_public"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 12}),
        }
        labels = {
            "name": _("Szabály neve"),
            "body": _("Szabály szövege"),
            "is_public": _("Nyilvános mások számára"),
        }
        help_texts = {
            "body": _("Hosszú szöveges leírás. Tárolt leírás: a rendszer nem ellenőrzi és nem hajtja végre."),
        }
    def __init__(self, *a, user=None, **kw):
        super().__init__(*a, **kw)
        self.user = user
        # Task6: body is stored verbatim; Django CharField strips by
        # default, which would silently alter long user text.
        self.fields["body"].strip = False
        if user is not None:
            self.fields["category"].queryset = RuleCategory.objects.filter(owner=user)
    def clean_category(self):
        cat = self.cleaned_data.get("category")
        if cat is not None and self.user is not None and cat.owner_id != self.user.id:
            raise forms.ValidationError(_("A kategória csak saját kategória lehet."))
        return cat
    def clean_body(self):
        body = self.cleaned_data.get("body") or ""
        if len(body) > RULE_BODY_MAX_LENGTH:
            raise forms.ValidationError(_("A szöveg legfeljebb 100000 karakter lehet."))
        return body
    def clean(self):
        cleaned = super().clean()
        cat = cleaned.get("category")
        new_name = (cleaned.get("new_category") or "").strip()
        if cat is None and not new_name:
            raise forms.ValidationError(_("Válassz kategóriát vagy hozz létre újat."))
        if cat is not None and new_name:
            raise forms.ValidationError(_("Egyszerre csak az egyik: válassz kategóriát vagy adj meg újat."))
        return cleaned
