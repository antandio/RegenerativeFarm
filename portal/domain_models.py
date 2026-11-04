"""Stage 2 tenant-scoped domain models. FK targets use lazy strings so
portal.models can import these names at its bottom without a cycle."""
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import m2m_changed, post_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .document_storage import delete_storage_file

EQUIPMENT_CAPABILITY_CHOICES = [
    ("tillage", _("Talajművelés")), ("sowing", _("Vetés")),
    ("harvest", _("Betakarítás")), ("irrigation", _("Öntözés")),
    ("transport", _("Szállítás")), ("livestock", _("Állattartás")),
    ("other", _("Egyéb")),
]
EQUIPMENT_CAPABILITY_VALUES = frozenset(c[0] for c in EQUIPMENT_CAPABILITY_CHOICES)

PROPERTY_KIND_CHOICES = [
    ("field", _("Szántóföld")), ("orchard", _("Gyümölcsös")),
    ("garden", _("Kert")), ("greenhouse", _("Üvegház/fóliasátor")),
    ("processing", _("Feldolgozó")), ("office", _("Iroda")),
    ("machine_hall", _("Gépszín")), ("barn", _("Istálló/ól")),
    ("pasture", _("Legelő")), ("other", _("Egyéb")),
]
AGRICULTURAL_KINDS = frozenset({"field", "orchard", "garden", "greenhouse"})

CROP_STATE_CHOICES = [
    ("current", _("Aktuális")), ("planned", _("Tervezett")), ("history", _("Előzmény")),
]


class FarmLocation(models.Model):
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="locations")
    name = models.CharField(max_length=120)
    latitude = models.DecimalField(max_digits=8, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    timezone = models.CharField(max_length=64)
    class Meta:
        ordering = ["name"]
    def clean(self):
        super().clean()
        if not (self.name or "").strip():
            raise ValidationError({"name": _("A név kötelező.")})
        try:
            lat = float(self.latitude)
        except (TypeError, ValueError):
            raise ValidationError({"latitude": _("Érvénytelen szélesség.")})
        try:
            lon = float(self.longitude)
        except (TypeError, ValueError):
            raise ValidationError({"longitude": _("Érvénytelen hosszúság.")})
        if not (-90 <= lat <= 90):
            raise ValidationError({"latitude": _("A szélesség -90 és 90 között legyen.")})
        if not (-180 <= lon <= 180):
            raise ValidationError({"longitude": _("A hosszúság -180 és 180 között legyen.")})
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(str(self.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError({"timezone": _("Érvénytelen időzóna.")})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name


class FarmProperty(models.Model):
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="domain_properties")
    location = models.ForeignKey(FarmLocation, on_delete=models.PROTECT, related_name="properties")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=PROPERTY_KIND_CHOICES)
    area_hectares = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True, default="")
    boundary_geojson = models.JSONField(null=True, blank=True, default=None)
    point_latitude = models.DecimalField(max_digits=8, decimal_places=6, null=True, blank=True, default=None)
    point_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, default=None)
    class Meta:
        ordering = ["name"]
    @property
    def map_area_hectares(self):
        from .geometry import estimate_polygon_area_hectares
        if not self.boundary_geojson:
            return None
        try:
            return estimate_polygon_area_hectares(self.boundary_geojson)
        except Exception:
            return None
    def clean(self):
        super().clean()
        if not (self.name or "").strip():
            raise ValidationError({"name": _("A név kötelező.")})
        try:
            area = float(self.area_hectares)
        except (TypeError, ValueError):
            raise ValidationError({"area_hectares": _("Érvénytelen terület.")})
        if area < 0:
            raise ValidationError({"area_hectares": _("A terület nem lehet negatív.")})
        if self.location_id and self.farm_id and self.location.farm_id != self.farm_id:
            raise ValidationError({"location": _("A helyszín csak azonos farmhoz tartozhat.")})
        from .geometry import validate_polygon_geojson, validate_property_point
        if self.boundary_geojson not in (None, "", {}):
            try:
                validate_polygon_geojson(self.boundary_geojson)
            except ValidationError as e:
                raise ValidationError({"boundary_geojson": e.messages})
        if self.point_latitude is None and self.point_longitude is None:
            pass
        else:
            try:
                validate_property_point(self.point_latitude, self.point_longitude)
            except ValidationError as e:
                raise ValidationError({"point_latitude": e.messages})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name


class Equipment(models.Model):
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="equipment")
    name = models.CharField(max_length=120)
    capabilities = models.JSONField(default=list)
    available = models.BooleanField(default=True)
    hours_per_day = models.DecimalField(max_digits=4, decimal_places=1, default=8)
    properties = models.ManyToManyField("portal.FarmProperty", through="portal.EquipmentProperty",
                                        related_name="assigned_equipment", blank=True)
    class Meta:
        ordering = ["name"]
    def clean(self):
        super().clean()
        if not (self.name or "").strip():
            raise ValidationError({"name": _("A név kötelező.")})
        caps = self.capabilities or []
        if not isinstance(caps, list):
            raise ValidationError({"capabilities": _("Érvénytelen képességlista.")})
        if len(caps) > 20:
            raise ValidationError({"capabilities": _("Legfeljebb 20 képesség.")})
        for c in caps:
            if not isinstance(c, str) or len(c) > 40 or c not in EQUIPMENT_CAPABILITY_VALUES:
                raise ValidationError({"capabilities": _("Ismeretlen képesség: %s") % str(c)[:40]})
        try:
            h = float(self.hours_per_day)
        except (TypeError, ValueError):
            raise ValidationError({"hours_per_day": _("Érvénytelen óraszám.")})
        if not (0 < h <= 24):
            raise ValidationError({"hours_per_day": _("A napi óraszám 0 és 24 között legyen.")})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name


class EquipmentProperty(models.Model):
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name="property_links")
    property = models.ForeignKey("portal.FarmProperty", on_delete=models.CASCADE, related_name="equipment_links")
    class Meta:
        constraints = [models.UniqueConstraint(fields=["equipment", "property"], name="uniq_eq_prop")]
    def clean(self):
        super().clean()
        if self.equipment_id and self.property_id and self.equipment.farm_id != self.property.farm_id:
            raise ValidationError(_("A gép és a terület csak azonos farmhoz tartozhat."))
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


@receiver(m2m_changed, sender=Equipment.properties.through)
def _equipment_m2m_same_farm(sender, instance, action, pk_set, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    from portal.models import FarmProperty as FP
    bad = FP.objects.filter(pk__in=pk_set).exclude(farm_id=instance.farm_id).exists()
    if bad:
        raise ValidationError(_("A géphez csak azonos farm területe rendelhető."))


class CropCycle(models.Model):
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="crop_cycles")
    property = models.ForeignKey("portal.FarmProperty", on_delete=models.PROTECT, related_name="crop_cycles")
    crop_name = models.CharField(max_length=120)
    state = models.CharField(max_length=16, choices=CROP_STATE_CHOICES, default="current")
    growth_stage = models.CharField(max_length=120, blank=True, default="")
    sow_date = models.DateField(null=True, blank=True)
    expected_end_date = models.DateField(null=True, blank=True)
    observations = models.TextField(blank=True, default="")
    # Soft-hide preserves WorkItems and journal links; default manager stays unfiltered.
    is_hidden = models.BooleanField(default=False)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_reason = models.CharField(max_length=32, blank=True, default="")
    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["property", "state"], condition=Q(state="current"), name="uniq_cycle_current_per_property"),
        ]
    def clean(self):
        super().clean()
        if not (self.crop_name or "").strip():
            raise ValidationError({"crop_name": _("A növénynév kötelező.")})
        if self.property_id and self.farm_id and self.property.farm_id != self.farm_id:
            raise ValidationError({"property": _("A terület csak azonos farmhoz tartozhat.")})
        if self.property_id and self.property.kind not in AGRICULTURAL_KINDS:
            raise ValidationError({"property": _("Vetésforgó csak mezőgazdasági területhez rögzíthető.")})
        if self.sow_date and self.expected_end_date and self.sow_date > self.expected_end_date:
            raise ValidationError(_("A vetés dátuma nem lehet későbbi a várható befejezésnél."))
        if len(self.observations or "") > 2000:
            raise ValidationError({"observations": _("Legfeljebb 2000 karakter.")})
        # Only current stays unique per property; multiple planned rows allowed.
        if self.state == "current" and self.property_id:
            qs = CropCycle.objects.filter(property_id=self.property_id, state=self.state)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError({"state": _("Ezen a területen már van ilyen állapotú ciklus.")})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.crop_name


LIVESTOCK_WORK_KINDS = [
    ("grazing", _("Legeltetés")), ("feeding", _("Etetés / takarmányozás")),
    ("health", _("Egészségügyi teendő")), ("other", _("Egyéb állatmunka")),
]
LIVESTOCK_WORK_KIND_VALUES = frozenset(c[0] for c in LIVESTOCK_WORK_KINDS)
WORK_STATUS_CHOICES = [("open", _("Nyitott")), ("done", _("Kész"))]
WORK_STATUS_VALUES = frozenset(c[0] for c in WORK_STATUS_CHOICES)
LIVESTOCK_WORK_EVENT_MAP = {"grazing": "grazing", "feeding": "other", "health": "health", "other": "other"}


class WorkItem(models.Model):
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="work_items")
    crop_cycle = models.ForeignKey(CropCycle, on_delete=models.CASCADE, related_name="work_items",
                                   null=True, blank=True)
    property = models.ForeignKey("portal.FarmProperty", on_delete=models.PROTECT, related_name="work_items",
                                 null=True, blank=True)
    livestock_group = models.ForeignKey("LivestockGroup", on_delete=models.CASCADE,
                                        related_name="work_items", null=True, blank=True)
    livestock_kind = models.CharField(max_length=16, choices=LIVESTOCK_WORK_KINDS, blank=True, default="")
    status = models.CharField(max_length=16, choices=WORK_STATUS_CHOICES, default="open")
    completed_at = models.DateField(null=True, blank=True)
    completed_event = models.OneToOneField("portal.LivestockEvent", on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name="completed_work_item")
    title = models.CharField(max_length=120)
    capability = models.CharField(max_length=16, choices=EQUIPMENT_CAPABILITY_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    estimated_hours = models.DecimalField(max_digits=6, decimal_places=1, default=1)
    equipment = models.ForeignKey(Equipment, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="work_items")
    missing = models.BooleanField(default=False)
    recommendation = models.CharField(max_length=240, blank=True, default="")
    guidance = models.CharField(max_length=240, blank=True, default="")
    class Meta:
        ordering = ["start_date", "id"]
    def clean(self):
        super().clean()
        if self.capability not in EQUIPMENT_CAPABILITY_VALUES:
            from django.core.exceptions import ValidationError as VE
            raise VE({"capability": _("Ismeretlen képesség.")})
        if self.status not in WORK_STATUS_VALUES:
            from django.core.exceptions import ValidationError as VE
            raise VE({"status": _("Ismeretlen állapot.")})
        if self.start_date and self.end_date and self.start_date > self.end_date:
            from django.core.exceptions import ValidationError as VE
            raise VE(_("A kezdés nem lehet későbbi a befejezésnél."))
        if self.livestock_group_id:
            if self.crop_cycle_id:
                from django.core.exceptions import ValidationError as VE
                raise VE(_("Az állatmunka nem kapcsolódhat vetésforgóhoz."))
            if self.livestock_kind not in LIVESTOCK_WORK_KIND_VALUES:
                from django.core.exceptions import ValidationError as VE
                raise VE({"livestock_kind": _("Ismeretlen állatmunka-típus.")})
            try:
                gfarm = self.livestock_group.farm_id
            except Exception:
                gfarm = None
            if gfarm is not None and self.farm_id and gfarm != self.farm_id:
                from django.core.exceptions import ValidationError as VE
                raise VE({"livestock_group": _("A csoport csak azonos farmhoz tartozhat.")})
            try:
                gactive = self.livestock_group.is_active
            except Exception:
                gactive = True
            if not gactive:
                from django.core.exceptions import ValidationError as VE
                raise VE({"livestock_group": _("Archivált csoportra nem tervezhető munka.")})
        else:
            if self.livestock_kind:
                from django.core.exceptions import ValidationError as VE
                raise VE({"livestock_kind": _("Állatmunka-típus csak csoportos munkánál adható meg.")})
            if not self.crop_cycle_id:
                from django.core.exceptions import ValidationError as VE
                raise VE({"crop_cycle": _("Vetésforgó vagy állatcsoport kötelező.")})
            if not self.property_id:
                from django.core.exceptions import ValidationError as VE
                raise VE({"property": _("Terület kötelező.")})
        if self.crop_cycle_id and self.farm_id and self.crop_cycle.farm_id != self.farm_id:
            from django.core.exceptions import ValidationError as VE
            raise VE(_("A művelet csak azonos farm ciklusához tartozhat."))
        if self.property_id and self.farm_id and self.property.farm_id != self.farm_id:
            from django.core.exceptions import ValidationError as VE
            raise VE(_("A művelet csak azonos farm területéhez tartozhat."))
        if self.equipment_id and self.farm_id and self.equipment.farm_id != self.farm_id:
            from django.core.exceptions import ValidationError as VE
            raise VE(_("A gép csak azonos farmhoz tartozhat."))
        if self.completed_event_id and self.farm_id:
            try:
                jfarm = self.completed_event.farm_id
            except Exception:
                jfarm = None
            if jfarm is not None and jfarm != self.farm_id:
                from django.core.exceptions import ValidationError as VE
                raise VE(_("A kapcsolt esemény csak azonos farmhoz tartozhat."))
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.title


EVENT_TYPE_CHOICES = [
    ("sowing", _("Vetés")),
    ("spraying", _("Permetezés")),
    ("fertilization", _("Tápanyag-utánpótlás")),
    ("irrigation", _("Öntözés")),
    ("harvesting", _("Betakarítás")),
    ("tillage", _("Talajművelés")),
    ("weeding_pest", _("Gyomirtás / növényvédelem")),
    ("other", _("Egyéb")),
]
EVENT_TYPE_VALUES = frozenset(c[0] for c in EVENT_TYPE_CHOICES)


class FarmingEvent(models.Model):
    """Stage A: actual farming-event journal (not planned WorkItems)."""
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="farming_events")
    property = models.ForeignKey("portal.FarmProperty", on_delete=models.PROTECT, related_name="farming_events")
    crop_cycle = models.ForeignKey("portal.CropCycle", on_delete=models.SET_NULL, null=True, blank=True, related_name="farming_events")
    # Stable crop identity of the journal row; blank allowed for legacy rows.
    crop_name_snapshot = models.CharField(max_length=120, blank=True, default="")
    event_date = models.DateField()
    event_type = models.CharField(max_length=16, choices=EVENT_TYPE_CHOICES)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=32, blank=True, default="")
    materials = models.CharField(max_length=240, blank=True, default="")
    treated_area_hectares = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey("portal.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_farming_events")
    updated_by = models.ForeignKey("portal.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="updated_farming_events")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["event_date", "id"]
    def clean(self):
        super().clean()
        if self.property_id and self.farm_id:
            try:
                pfarm = self.property.farm_id
            except Exception:
                pfarm = None
            if pfarm is not None and pfarm != self.farm_id:
                raise ValidationError({"property": _("A terület csak azonos farmhoz tartozhat.")})
        if self.crop_cycle_id and self.farm_id:
            try:
                cfarm = self.crop_cycle.farm_id
                cprop = self.crop_cycle.property_id
            except Exception:
                cfarm = cprop = None
            if cfarm is not None and cfarm != self.farm_id:
                raise ValidationError({"crop_cycle": _("A vetésforgó csak azonos farmhoz tartozhat.")})
            if cprop is not None and self.property_id and cprop != self.property_id:
                raise ValidationError({"crop_cycle": _("A vetésforgó csak ugyanahhoz a területhez tartozhat.")})
        if self.event_type not in EVENT_TYPE_VALUES:
            raise ValidationError({"event_type": _("Ismeretlen eseménytípus.")})
        if self.quantity is not None:
            if self.quantity < 0:
                raise ValidationError({"quantity": _("A mennyiség nem lehet negatív.")})
            if not (self.unit or "").strip():
                raise ValidationError({"unit": _("Mennyiség megadásakor a mértékegység kötelező.")})
        if self.treated_area_hectares is not None and self.treated_area_hectares < 0:
            raise ValidationError({"treated_area_hectares": _("A kezelt terület nem lehet negatív.")})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return "%s %s" % (self.event_date, self.event_type)


LIVESTOCK_EVENT_KINDS = [
    ("birth", _("Születés")), ("purchase", _("Vásárlás")),
    ("sale", _("Eladás")), ("death", _("Elhullás")),
    ("move", _("Átmozgatás")), ("health", _("Egészségügyi beavatkozás")),
    ("grazing", _("Legeltetés")), ("other", _("Egyéb")),
]
LIVESTOCK_EVENT_KIND_VALUES = frozenset(c[0] for c in LIVESTOCK_EVENT_KINDS)
LIVESTOCK_POSITIVE_KINDS = frozenset({"birth", "purchase"})
LIVESTOCK_NEGATIVE_KINDS = frozenset({"sale", "death", "move"})
LIVESTOCK_ZERO_KINDS = frozenset({"health", "grazing", "other"})


class LivestockEvent(models.Model):
    """Task4: dated livestock event. Count moves happen only via record().

    record() runs in a transaction with select_for_update on the group
    (and target): it enforces chronological entry (no backdating behind
    the group's latest event), rejects any move that would make a live
    count negative, and optionally writes one linked FarmingEvent of type
    "other" (never a new journal model, at most one per event). Deleting
    a journal entry SET_NULLs the trace link without touching this row.
    """
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="livestock_events")
    group = models.ForeignKey("LivestockGroup", on_delete=models.CASCADE, related_name="livestock_events")
    target_group = models.ForeignKey(
        "LivestockGroup", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incoming_livestock_moves")
    event_date = models.DateField()
    kind = models.CharField(max_length=16, choices=LIVESTOCK_EVENT_KINDS)
    headcount_delta = models.IntegerField(default=0)
    property = models.ForeignKey(
        "portal.FarmProperty", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="livestock_events")
    farming_event = models.OneToOneField(
        "portal.FarmingEvent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="livestock_event")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["event_date", "id"]
    def clean(self):
        super().clean()
        if self.kind not in LIVESTOCK_EVENT_KIND_VALUES:
            raise ValidationError({"kind": _("Ismeretlen eseménytípus.")})
        if self.kind in LIVESTOCK_POSITIVE_KINDS and not (self.headcount_delta or 0) > 0:
            raise ValidationError({"headcount_delta": _("Születés/vásárlás csak pozitív létszámmal rögzíthető.")})
        if self.kind in LIVESTOCK_NEGATIVE_KINDS and not (self.headcount_delta or 0) < 0:
            raise ValidationError({"headcount_delta": _("Eladás/elhullás/átmozgatás csak negatív létszámmal rögzíthető.")})
        if self.kind in LIVESTOCK_ZERO_KINDS and (self.headcount_delta or 0) != 0:
            raise ValidationError({"headcount_delta": _("Egészségügyi/legeltetési/egyéb esemény nem mozgat létszámot.")})
        grp = self.group if getattr(self, "group_id", None) else None
        if grp is not None and self.farm_id and grp.farm_id != self.farm_id:
            raise ValidationError({"group": _("A csoport csak azonos farmhoz tartozhat.")})
        if grp is not None and not grp.is_active:
            raise ValidationError({"group": _("Archivált csoportra nem rögzíthető esemény.")})
        tgt = self.target_group if getattr(self, "target_group_id", None) else None
        if self.kind == "move":
            if tgt is None:
                raise ValidationError({"target_group": _("Átmozgatáshoz célcsoport kötelező.")})
            if tgt.pk == getattr(self.group, "pk", None):
                raise ValidationError({"target_group": _("A célcsoport nem lehet azonos a forrással.")})
            if self.farm_id and tgt.farm_id != self.farm_id:
                raise ValidationError({"target_group": _("A célcsoport csak azonos farmhoz tartozhat.")})
            if not tgt.is_active:
                raise ValidationError({"target_group": _("Archivált csoportba nem mozgatható.")})
        elif tgt is not None:
            raise ValidationError({"target_group": _("Célcsoport csak átmozgatásnál adható meg.")})
        if self.property_id and self.farm_id:
            try:
                pfarm = self.property.farm_id
            except Exception:
                pfarm = None
            if pfarm is not None and pfarm != self.farm_id:
                raise ValidationError({"property": _("A terület csak azonos farmhoz tartozhat.")})
        if self.farming_event_id and self.farm_id:
            try:
                jfarm = self.farming_event.farm_id
            except Exception:
                jfarm = None
            if jfarm is not None and jfarm != self.farm_id:
                raise ValidationError({"farming_event": _("A naplóbejegyzés csak azonos farmhoz tartozhat.")})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return "%s %s %+d" % (self.event_date, self.kind, self.headcount_delta or 0)
    @classmethod
    def record(cls, farm, group, event_date, kind, headcount_delta=0,
               target_group=None, property=None, notes="",
               farming_event=None, create_journal=False, actor=None):
        """Atomically validate, journal-link and apply one dated event."""
        from django.db import transaction as _tx
        farm_id = farm.pk if hasattr(farm, "pk") else farm
        with _tx.atomic():
            try:
                grp = LivestockGroup.objects.select_for_update().get(
                    pk=group.pk if hasattr(group, "pk") else group, farm_id=farm_id)
            except LivestockGroup.DoesNotExist:
                raise ValidationError({"group": _("A csoport csak azonos farmhoz tartozhat.")})
            tgt = None
            if target_group is not None:
                try:
                    tgt = LivestockGroup.objects.select_for_update().get(
                        pk=target_group.pk if hasattr(target_group, "pk") else target_group,
                        farm_id=farm_id)
                except LivestockGroup.DoesNotExist:
                    raise ValidationError({"target_group": _("A célcsoport csak azonos farmhoz tartozhat.")})
            prop = property
            if prop is not None and getattr(prop, "farm_id", farm_id) != farm_id:
                raise ValidationError({"property": _("A terület csak azonos farmhoz tartozhat.")})
            if farming_event is not None and getattr(farming_event, "farm_id", farm_id) != farm_id:
                raise ValidationError({"farming_event": _("A naplóbejegyzés csak azonos farmhoz tartozhat.")})
            if kind not in LIVESTOCK_EVENT_KIND_VALUES:
                raise ValidationError({"kind": _("Ismeretlen eseménytípus.")})
            if not grp.is_active:
                raise ValidationError({"group": _("Archivált csoportra nem rögzíthető esemény.")})
            if kind == "move":
                if tgt is None:
                    raise ValidationError({"target_group": _("Átmozgatáshoz célcsoport kötelező.")})
                if tgt.pk == grp.pk:
                    raise ValidationError({"target_group": _("A célcsoport nem lehet azonos a forrással.")})
                if not tgt.is_active:
                    raise ValidationError({"target_group": _("Archivált csoportba nem mozgatható.")})
            latest = cls.objects.filter(group=grp).order_by("-event_date", "-id").first()
            if latest is not None and event_date < latest.event_date:
                raise ValidationError(_("Az esemény dátuma nem lehet korábbi a csoport legutóbbi eseményénél."))
            if tgt is not None:
                t_latest = cls.objects.filter(group=tgt).order_by("-event_date", "-id").first()
                if t_latest is not None and event_date < t_latest.event_date:
                    raise ValidationError(_("Az esemény dátuma nem lehet korábbi a célcsoport legutóbbi eseményénél."))
            new_count = grp.headcount + (headcount_delta or 0)
            if new_count < 0:
                raise ValidationError(_("Az esemény negatívba vinné a csoport létszámát."))
            journal = farming_event
            if journal is None and create_journal:
                jprop = prop or grp.housing_property
                if jprop is None:
                    raise ValidationError(_("Naplóbejegyzéshez terület kell (esemény-terület vagy szállás)."))
                from .domain_models import FarmingEvent as _FE
                journal = _FE(
                    farm_id=farm_id, property=jprop, event_date=event_date,
                    event_type="other",
                    description="[Állat: %s] %s%s" % (
                        grp.name, dict(LIVESTOCK_EVENT_KINDS).get(kind, kind),
                        (": " + notes.strip()) if (notes or "").strip() else ""),
                    quantity=abs(headcount_delta) if headcount_delta else None,
                    unit="db" if headcount_delta else "",
                    notes=(notes or ""))
                if actor is not None:
                    journal.created_by = actor
                    journal.updated_by = actor
                journal.save()
            ev = cls(farm_id=farm_id, group=grp, target_group=tgt,
                     event_date=event_date, kind=kind,
                     headcount_delta=headcount_delta or 0,
                     property=prop, farming_event=journal, notes=notes or "")
            ev.full_clean()
            super(LivestockEvent, ev).save()
            grp.headcount = new_count
            grp.save()
            if tgt is not None:
                tgt.headcount = tgt.headcount - (headcount_delta or 0)
                tgt.save()
            return ev
    def reverse_delete(self):
        """Delete this event, reversing its count move (journal stays).

        Safe reversal: the row is re-fetched under lock (a repeated call
        raises instead of double-reversing the count); only the latest
        event of the source chronology — and, for moves, of the target
        chronology too — may be reversed; archived groups and events
        linked to completed planned work refuse with a clear error.
        """
        from django.db import transaction as _tx
        from django.db.models import Q as _Q
        with _tx.atomic():
            try:
                locked = LivestockEvent.objects.select_for_update().get(pk=self.pk)
            except LivestockEvent.DoesNotExist:
                raise ValidationError(_("Az esemény már vissza lett vonva."))
            grp = LivestockGroup.objects.select_for_update().get(pk=locked.group_id)
            tgt = None
            if locked.target_group_id:
                tgt = LivestockGroup.objects.select_for_update().get(
                    pk=locked.target_group_id)

            def _latest_for(group_id):
                return (LivestockEvent.objects
                        .filter(_Q(group_id=group_id) | _Q(target_group_id=group_id))
                        .order_by("-event_date", "-id").first()
                        )
            latest = _latest_for(grp.pk)
            if latest is None or latest.pk != locked.pk:
                raise ValidationError(
                    _("Csak a csoport legutóbbi eseménye vonható vissza."))
            if tgt is not None:
                t_latest = _latest_for(tgt.pk)
                if t_latest is None or t_latest.pk != locked.pk:
                    raise ValidationError(
                        _("Az átmozgatás nem vonható vissza: a célcsoporton újabb esemény van."))
            if not grp.is_active or (tgt is not None and not tgt.is_active):
                raise ValidationError(
                    _("Archivált csoport eseménye nem vonható vissza."))
            if WorkItem.objects.filter(completed_event_id=locked.pk).exists():
                raise ValidationError(
                    _("Az esemény elvégzett munkához kapcsolódik, ezért nem vonható vissza."))
            restored = grp.headcount - (locked.headcount_delta or 0)
            if restored < 0:
                raise ValidationError(_("Az esemény nem vonható vissza: a csoport létszáma negatívba menne."))
            if tgt is not None:
                t_restored = tgt.headcount + (locked.headcount_delta or 0)
                if t_restored < 0:
                    raise ValidationError(_("Az átmozgatás nem vonható vissza: a célcsoport létszáma negatívba menne."))
                tgt.headcount = t_restored
                tgt.save()
            grp.headcount = restored
            grp.save()
            locked.delete()


LIVESTOCK_SPECIES_CHOICES = [
    ("cattle", _("Szarvasmarha")), ("sheep", _("Juh")),
    ("goat", _("Kecske")), ("pig", _("Sertés")),
    ("poultry", _("Baromfi")), ("horse", _("Ló")),
    ("bee", _("Méh")), ("other", _("Egyéb")),
]
LIVESTOCK_SPECIES_VALUES = frozenset(c[0] for c in LIVESTOCK_SPECIES_CHOICES)


class LivestockGroup(models.Model):
    """Task4: usable livestock group foundation (single model, no Animal yet).

    Species is a fixed choice plus a free custom label (breed_label), so no
    separate species table is needed. headcount is the live count; dated
    LivestockEvent rows move it. Archiving (is_active=False) preserves
    history instead of deleting; housing reuses tenant FarmProperty
    (barn/pasture kinds included).
    """
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="livestock_groups")
    name = models.CharField(max_length=120)
    species = models.CharField(max_length=16, choices=LIVESTOCK_SPECIES_CHOICES)
    breed_label = models.CharField(max_length=120, blank=True, default="")
    headcount = models.PositiveIntegerField(default=0)
    housing_property = models.ForeignKey(
        "portal.FarmProperty", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="housed_livestock_groups")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["farm", "name"], name="uniq_livestock_group_farm_name"),
        ]
    def clean(self):
        super().clean()
        if not (self.name or "").strip():
            raise ValidationError({"name": _("A név kötelező.")})
        if self.species not in LIVESTOCK_SPECIES_VALUES:
            raise ValidationError({"species": _("Ismeretlen faj.")})
        if self.headcount is None or self.headcount < 0:
            raise ValidationError({"headcount": _("A létszám nem lehet negatív.")})
        if self.housing_property_id and self.farm_id:
            try:
                pfarm = self.housing_property.farm_id
            except Exception:
                pfarm = None
            if pfarm is not None and pfarm != self.farm_id:
                raise ValidationError({"housing_property": _("A szállás csak azonos farm területén lehet.")})
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name


def normalize_services(raw):
    """Newline-canonical services list: strip lines, drop empties, join."""
    lines = [(ln or "").strip() for ln in str(raw or "").splitlines()]
    return "\n".join(ln for ln in lines if ln)


class ExternalContact(models.Model):
    """Task5: external hireable nonmember contact (tenant-scoped).

    Never a User/Membership: no login, no invitation, no role. Contact
    requires phone OR email (at least one meaningful value); services is
    a newline-canonical free-text list (one requestable job per line, no
    fixed schema); notes is a short description; availability optional.
    """
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="external_contacts")
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=64, blank=True, default="")
    email = models.EmailField(max_length=254, blank=True, default="")
    services = models.TextField()
    notes = models.TextField(blank=True, default="")
    availability = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["name"]
    def clean(self):
        super().clean()
        name = (self.name or "").strip()
        if not name:
            raise ValidationError({"name": _("A név kötelező.")})
        self.name = name
        phone = (self.phone or "").strip()
        email = (self.email or "").strip()
        if not phone and not email:
            raise ValidationError(_("Telefon vagy e-mail megadása kötelező."))
        if phone and len(phone) < 5:
            raise ValidationError({"phone": _("A telefonszám túl rövid.")})
        self.phone = phone
        self.email = email
        canonical = normalize_services(self.services)
        if not canonical:
            raise ValidationError({"services": _("Legalább egy kérhető munka megadása kötelező.")})
        self.services = canonical
        self.notes = (self.notes or "").strip()
        if len(self.notes) > 1000:
            raise ValidationError({"notes": _("A leírás legfeljebb 1000 karakter lehet.")})
        self.availability = (self.availability or "").strip()
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    def service_list(self):
        return [ln for ln in (self.services or "").split("\n") if ln.strip()]
    def __str__(self):
        return self.name


RULE_BODY_MAX_LENGTH = 100000

class RuleCategory(models.Model):
    """Task6: user-owned rule category. Name unique per owner; another
    owner's same name neither collides nor becomes selectable. No farm
    dependency: usable without any farm."""
    owner = models.ForeignKey("portal.User", on_delete=models.CASCADE, related_name="rule_categories")
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="uniq_rulecat_owner_name")]
    def clean(self):
        super().clean()
        name = (self.name or "").strip()
        if not name:
            raise ValidationError({"name": _("A név kötelező.")})
        self.name = name
    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        # Field + model validation only: per-owner uniqueness is proven
        # at DB level (UniqueConstraint -> IntegrityError), so unique
        # validation (validate_unique/validate_constraints) is skipped.
        self.clean_fields()
        self.clean()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name


class RuleDefinitionQuerySet(models.QuerySet):
    """Block silent is_public bypasses: QuerySet.update skips save()."""
    def update(self, **kwargs):
        if "is_public" in kwargs:
            raise ValueError(
                "RuleDefinition.is_public must be changed via save(), "
                "not QuerySet.update(), so public->private revokes other "
                "farms. Raw SQL bypasses this guard by design; use save().")
        return super().update(**kwargs)
    def bulk_update(self, objs, fields, **kwargs):
        if fields is not None and "is_public" in set(fields):
            raise ValueError(
                "RuleDefinition.is_public must be changed via save(), "
                "not bulk_update(), so public->private revokes other "
                "farms. Raw SQL bypasses this guard by design; use save().")
        return super().bulk_update(objs, fields, **kwargs)


class RuleDefinitionManager(models.Manager.from_queryset(RuleDefinitionQuerySet)):
    """Default manager exposing the is_public bulk guards."""
    def bulk_update(self, objs, fields, **kwargs):
        if fields is not None and "is_public" in set(fields):
            raise ValueError(
                "RuleDefinition.is_public must be changed via save(), "
                "not bulk_update(), so public->private revokes other "
                "farms. Raw SQL bypasses this guard by design; use save().")
        return super().bulk_update(objs, fields, **kwargs)


class RuleDefinition(models.Model):
    """Task6: user-owned long-text rule description. Owner is the User,
    never the active farm. Stored text only: never eval/exec/template
    rendered, never sent to any LLM; the UI states it is a description,
    not enforced compliance and no legal claim is made.
    Task7: public->private revoke lives here (not in a view) so every
    save() path revokes atomically; bulk is_public flips are rejected
    (use save()); raw SQL bypasses ORM guards by design."""
    owner = models.ForeignKey("portal.User", on_delete=models.CASCADE, related_name="rule_definitions")
    category = models.ForeignKey(RuleCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="rules")
    name = models.CharField(max_length=200)
    body = models.TextField()
    is_public = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["-updated_at", "-id"]
    def clean(self):
        super().clean()
        name = (self.name or "").strip()
        if not name:
            raise ValidationError({"name": _("A név kötelező.")})
        self.name = name
        if len(self.body or "") > RULE_BODY_MAX_LENGTH:
            raise ValidationError({"body": _("A szöveg legfeljebb 100000 karakter lehet.")})
        if self.category_id and self.owner_id and self.category.owner_id != self.owner_id:
            raise ValidationError({"category": _("A kategória csak saját kategória lehet.")})
    objects = RuleDefinitionManager()
    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.clean_fields()
        self.clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "is_public" not in set(update_fields):
            super().save(*args, **kwargs)
            return
        from django.db import transaction as _tx
        with _tx.atomic():
            was_public = None
            if self.pk is not None:
                try:
                    locked = type(self).objects.select_for_update().get(pk=self.pk)
                except type(self).DoesNotExist:
                    locked = None
                if locked is not None:
                    was_public = locked.is_public
            super().save(*args, **kwargs)
            if was_public and not self.is_public:
                RuleActivation.objects.filter(
                    rule_id=self.pk, is_active=True).exclude(
                    farm__owner_id=self.owner_id).update(is_active=False)
    def __str__(self):
        return self.name


class RuleActivation(models.Model):
    """Task7: a farm installs a rule definition (own-farm-author or public).

    Eligibility is farm-based and installer-independent: rule R may be
    installed on farm F iff R.owner_id == F.owner_id OR R is currently
    public. Stored live FK only — no name/body/category snapshot, so
    author edits render on next read. is_active=False rows are kept
    (never auto-reactivated on re-publication). Stored text is never
    executed nor sent to any LLM; the UI states no compliance assertion.
    """
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE,
                             related_name="rule_activations")
    rule = models.ForeignKey(RuleDefinition, on_delete=models.CASCADE,
                             related_name="activations")
    activated_by = models.ForeignKey("portal.User", on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name="rule_activations")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["farm", "rule"],
                                    name="uniq_ruleactivation_farm_rule"),
        ]
    def __str__(self):
        return f"{self.farm_id}:{self.rule_id}"


class DocumentQuerySet(models.QuerySet):
    """Task8: block silent is_public bypasses (RuleDefinition pattern)."""
    def update(self, **kwargs):
        if "is_public" in kwargs:
            raise ValueError(
                "Document.is_public must be changed via save(), "
                "not QuerySet.update(). Raw SQL bypasses this guard by design; use save().")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        if fields is not None and "is_public" in set(fields):
            raise ValueError(
                "Document.is_public must be changed via save(), "
                "not bulk_update(). Raw SQL bypasses this guard by design; use save().")
        return super().bulk_update(objs, fields, **kwargs)


class DocumentManager(models.Manager.from_queryset(DocumentQuerySet)):
    """Default manager exposing the is_public bulk guards."""
    def bulk_update(self, objs, fields, **kwargs):
        if fields is not None and "is_public" in set(fields):
            raise ValueError(
                "Document.is_public must be changed via save(), "
                "not bulk_update(). Raw SQL bypasses this guard by design; use save().")
        return super().bulk_update(objs, fields, **kwargs)


class Document(models.Model):
    """Task8: user-owned secure document. Owner is the User, never the
    active farm; farm binding is task9's job. Stored file lives under
    PRIVATE_DOCUMENT_ROOT under a server-generated UUID name; the DB
    never stores a client path. File content is stored only, never
    parsed/executed, never sent to any LLM, never fetched from URLs."""
    owner = models.ForeignKey("portal.User", on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    original_filename = models.CharField(max_length=255)
    storage_name = models.CharField(max_length=64, unique=True)
    extension = models.CharField(max_length=16)
    content_type = models.CharField(max_length=128)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    is_public = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = DocumentManager()

    class Meta:
        ordering = ["-updated_at", "-id"]

    def clean(self):
        super().clean()
        name = (self.name or "").strip()
        if not name:
            from django.core.exceptions import ValidationError as _VE
            raise _VE({"name": _("A név kötelező.")})
        self.name = name

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.clean_fields(exclude=["owner"])
        self.clean()
        # Task9 shared-lock revoke: public->private in the SAME txn as the
        # save (RuleDefinition pattern). update_fields without is_public
        # skips the lock; bulk is_public flips are rejected by the
        # QuerySet/manager guards (raw SQL bypasses by design; use save()).
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "is_public" not in set(update_fields):
            super().save(*args, **kwargs)
            return
        with transaction.atomic():
            was_public = None
            if self.pk is not None:
                try:
                    locked = type(self).objects.select_for_update().get(pk=self.pk)
                except type(self).DoesNotExist:
                    locked = None
                if locked is not None:
                    was_public = locked.is_public
            super().save(*args, **kwargs)
            if was_public and not self.is_public:
                FarmDocument.objects.filter(
                    document_id=self.pk, is_active=True).exclude(
                    farm__owner_id=self.owner_id).update(is_active=False)

    def __str__(self):
        return self.name


class FarmDocument(models.Model):
    """Task9: a farm installs a document into its knowledge base (managed).

    Eligibility is farm-based and installer-independent: document D may be
    installed on farm F iff D.owner_id == F.owner_id OR D is currently
    public. Stored live FK only — no name/description snapshot, so author
    metadata edits render live while the row stays eligible. is_active=False
    rows are kept (never auto-reactivated on re-publication). Entries are
    management only: nothing here executes document content, sends it to any
    LLM, or feeds plans/chat/advice.
    """
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE,
                             related_name="farm_documents")
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name="farm_entries")
    added_by = models.ForeignKey("portal.User", on_delete=models.SET_NULL,
                                 null=True, blank=True,
                                 related_name="farm_document_adds")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["farm", "document"],
                                    name="uniq_farmdocument_farm_doc"),
        ]

    def __str__(self):
        return f"{self.farm_id}:{self.document_id}"


class NewsSource(models.Model):
    """Task10: user-owned news-portal collection entry (CRUD only).

    Owner is the User, never the active farm: the collection is usable
    without any farm and independent of farm memberships; there is no
    sharing, no public/private switch, and no farm activation (tasks.md
    asks for none). ``url`` stores the canonical form produced by
    :func:`portal.news_url.normalize_news_url` (fragment stripped,
    default port dropped, IDNA/lowercased host), and
    ``unique(owner, url)`` dedupes per owner while other owners may
    store the same normalized URL.

    No network/scheduler/cloud/LLM is attached: rows are collection
    metadata only. The future daily scan worker (not implemented) must
    revalidate resolved IPs on every fetch and defend against DNS
    rebinding/redirects — the syntactic checks here are storage-time
    defense only, not fetch-time SSRF protection.
    """
    owner = models.ForeignKey("portal.User", on_delete=models.CASCADE,
                              related_name="news_sources")
    name = models.CharField(max_length=200)
    url = models.CharField(max_length=2000)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "url"],
                                    name="uniq_newssource_owner_url"),
        ]

    def clean(self):
        super().clean()
        name = (self.name or "").strip()
        if not name:
            raise ValidationError({"name": _("A név kötelező.")})
        self.name = name
        from .news_url import normalize_news_url
        try:
            self.url = normalize_news_url(self.url)
        except ValidationError as e:
            raise ValidationError({"url": e.messages})
        if len(self.notes or "") > 2000:
            raise ValidationError({"notes": _("A jegyzet legfeljebb 2000 karakter.")})

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        # Field + model validation only: per-owner duplicate URLs are
        # proven at DB level (UniqueConstraint -> IntegrityError), so
        # unique validation is skipped here; forms surface it as a
        # field error via validate_unique().
        self.clean_fields(exclude=["owner"])
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class SowingComparison(models.Model):
    """Review identity for one actual-vs-planned pair plus current state.

    The comparison row is the review identity (decision lifecycle); every
    paid dispatch is preserved as an immutable SowingComparisonAttempt row
    beneath it. References use FKs (event CASCADE, plan and property
    SET_NULL for preservation) while the compared values are immutable
    snapshots, so later renames or edits can be detected via input_hash
    without rewriting history. dispatch_authorized_at mirrors the latest
    attempt dispatch and is never cleared.
    """

    STATUS = [
        ("pending", "pending"),
        ("success", "success"),
        ("failed", "failed"),
        ("superseded", "superseded"),
    ]
    DECISIONS = [
        ("match", "match"),
        ("mismatch", "mismatch"),
        ("uncertain", "uncertain"),
    ]
    REVIEWS = [
        ("pending_review", "pending_review"),
        ("confirmed_mismatch", "confirmed_mismatch"),
        ("resolved_matched", "resolved_matched"),
        ("dismissed_retained", "dismissed_retained"),
        ("dismissed_deleted", "dismissed_deleted"),
        ("superseded", "superseded"),
    ]
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE,
                             related_name="sowing_comparisons")
    initiated_by = models.ForeignKey("portal.User", on_delete=models.CASCADE,
                                     related_name="sowing_comparisons")
    farming_event = models.ForeignKey("portal.FarmingEvent", on_delete=models.CASCADE,
                                      related_name="sowing_comparisons")
    planned_cycle = models.ForeignKey("portal.CropCycle", on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      related_name="sowing_comparisons")
    property = models.ForeignKey("portal.FarmProperty", on_delete=models.SET_NULL,
                                 null=True, blank=True,
                                 related_name="sowing_comparisons")
    crop_snapshot = models.CharField(max_length=120, blank=True, default="")
    event_date = models.DateField()
    planned_crop_snapshot = models.CharField(max_length=120, blank=True, default="")
    planned_sow_date = models.DateField()
    input_hash = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=80)
    language = models.CharField(max_length=10,
                                choices=[("hu", "Magyar"), ("en", "English")],
                                default="hu")
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    decision = models.CharField(max_length=16, blank=True, default="")
    reason_code = models.CharField(max_length=32, blank=True, default="")
    review_state = models.CharField(max_length=32, choices=REVIEWS,
                                    default="pending_review")
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message_safe = models.CharField(max_length=300, null=True, blank=True)
    requested_model = models.CharField(max_length=120)
    actual_model = models.CharField(max_length=120, null=True, blank=True)
    usage_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    usage_output_tokens = models.PositiveIntegerField(null=True, blank=True)
    attempt_started_at = models.DateTimeField(default=timezone.now)
    # Scheduling reservation: set when a dispatch is queued, cleared when
    # the worker claims an attempt or the reservation is released. A fresh
    # value makes duplicate retries no-ops; a stale one is recoverable and
    # is never confused with a live dispatched attempt.
    queued_at = models.DateTimeField(null=True, blank=True)
    dispatch_authorized_at = models.DateTimeField(null=True, blank=True)
    dispatch_consent_event_id = models.BigIntegerField(null=True, blank=True)
    current_attempt = models.ForeignKey(
        "portal.SowingComparisonAttempt", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["farm", "idempotency_key"],
                                    name="uniq_sowing_farm_idem"),
            models.UniqueConstraint(fields=["id", "farm"],
                                    name="uniq_sowing_id_farm"),
            # One actual consumes at most one plan: at most one consumed
            # review row per farming event (DB-level backstop; service
            # guards with a per-event lock before reaching this).
            models.UniqueConstraint(
                fields=["farming_event"],
                condition=Q(review_state__in=["resolved_matched",
                                              "dismissed_deleted"]),
                name="uniq_sowing_consumed_per_event"),
        ]
        ordering = ["-attempt_started_at"]
        indexes = [
            models.Index(fields=["farm", "attempt_started_at"]),
            models.Index(fields=["farm", "status", "attempt_started_at"]),
        ]

    def __str__(self):
        return "sowing %s vs %s" % (self.farming_event_id, self.planned_cycle_id)


class SowingComparisonAttempt(models.Model):
    """Immutable per-dispatch ledger row beneath a SowingComparison.

    One row per paid dispatch, never reused or cleared. Dispatch identity
    (comparison, farm, actor, token, timestamp, consent, requested model)
    is frozen at creation; only completion fields (status, decision,
    model, usage, error, completion time) are filled once when the attempt
    finishes. Quota counts every dispatched attempt row.
    """

    STATUS = [
        ("pending", "pending"),
        ("success", "success"),
        ("failed", "failed"),
    ]
    comparison = models.ForeignKey(
        "portal.SowingComparison", on_delete=models.CASCADE,
        related_name="attempts")
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE,
                             related_name="sowing_attempts")
    initiated_by = models.ForeignKey("portal.User", on_delete=models.CASCADE,
                                     related_name="sowing_attempts")
    attempt_token = models.CharField(max_length=64, unique=True, db_index=True)
    dispatched_at = models.DateTimeField(default=timezone.now, db_index=True)
    dispatch_consent_event_id = models.BigIntegerField(null=True, blank=True)
    requested_model = models.CharField(max_length=120)
    actual_model = models.CharField(max_length=120, null=True, blank=True)
    usage_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    usage_output_tokens = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    decision = models.CharField(max_length=16, blank=True, default="")
    reason_code = models.CharField(max_length=32, blank=True, default="")
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message_safe = models.CharField(max_length=300, null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["dispatched_at"]
        indexes = [
            models.Index(fields=["farm", "dispatched_at"]),
            models.Index(fields=["comparison", "dispatched_at"]),
        ]

    def save(self, *args, **kwargs):
        # Freeze dispatch identity on updates; completion fields stay mutable.
        if self.pk is not None:
            try:
                old = SowingComparisonAttempt.objects.get(pk=self.pk)
            except SowingComparisonAttempt.DoesNotExist:
                old = None
            if old is not None:
                for field in ("comparison_id", "farm_id", "initiated_by_id",
                              "attempt_token", "dispatched_at",
                              "dispatch_consent_event_id", "requested_model"):
                    if getattr(old, field) != getattr(self, field):
                        raise ValueError("Sowing attempt dispatch identity is immutable.")
        super().save(*args, **kwargs)

    def __str__(self):
        return "sowing attempt %s" % self.attempt_token


@receiver(post_delete, sender=Document)
def _delete_document_blob(sender, instance, **kwargs):
    """Shared on_commit-safe blob cleanup for every row-deletion path:
    the detail view, queryset deletes, and owner (User) cascade deletes
    (account deletion is a plain ORM user.delete(), so it is covered).
    A rolled-back transaction discards the callback and retains the file."""
    storage_name = getattr(instance, "storage_name", None)
    if not storage_name:
        return
    transaction.on_commit(lambda: delete_storage_file(storage_name))
