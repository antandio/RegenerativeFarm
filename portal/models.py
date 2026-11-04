from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError(_("E-mail cím szükséges."))
        email = self.normalize_email(email.strip().lower())
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user
    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)

class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True, max_length=254)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.email

class TermsAcceptance(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="terms_acceptances")
    terms_version = models.CharField(max_length=40)
    privacy_notice_version = models.CharField(max_length=40)
    accepted_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["user","terms_version","privacy_notice_version"], name="uniq_terms_user_ver")]

PROFILE_TEXT_FIELDS = ["region_text","climate_text","farming_direction_text","soil_notes","water_notes","crops_livestock_notes","equipment_labour_notes","goals_notes","constraints_notes"]

class Farm(models.Model):
    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="farm")
    name = models.CharField(max_length=120)
    region_text = models.CharField(max_length=200, null=True, blank=True)
    climate_text = models.CharField(max_length=500, null=True, blank=True)
    farming_direction_text = models.CharField(max_length=300, null=True, blank=True)
    soil_notes = models.TextField(null=True, blank=True)
    water_notes = models.TextField(null=True, blank=True)
    crops_livestock_notes = models.TextField(null=True, blank=True)
    equipment_labour_notes = models.TextField(null=True, blank=True)
    goals_notes = models.TextField(null=True, blank=True)
    constraints_notes = models.TextField(null=True, blank=True)
    profile_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [
            models.CheckConstraint(condition=~models.Q(name=""), name="farm_name_nonempty"),
            models.CheckConstraint(condition=models.Q(profile_version__gte=1), name="farm_ver_gte1"),
            models.UniqueConstraint(fields=["id","owner"], name="uniq_farm_id_owner"),
        ]
    def profile_dict(self):
        d = {"name": self.name}
        for f in PROFILE_TEXT_FIELDS:
            d[f] = getattr(self, f)
        return d
    FIELD_LABELS = {"region_text":_("Régió, tájegység (saját szavakkal)"),"climate_text":_("Éghajlati jellemzés"),"farming_direction_text":_("Fő gazdálkodási irány"),"soil_notes":_("Talaj és saját megfigyelések"),"water_notes":_("Vízellátás módja és korlátai"),"crops_livestock_notes":_("Növények, vetésszerkezet, állatállomány"),"equipment_labour_notes":_("Gépek, bérmunka, munkaerő, időkeret"),"goals_notes":_("Célok"),"constraints_notes":_("Korlátok, költségvetés nagysága, kockázat, bizonytalanságok")}
    def completeness_missing(self):
        keys = ["region_text","climate_text","farming_direction_text","soil_notes","water_notes","crops_livestock_notes","equipment_labour_notes","goals_notes","constraints_notes"]
        return [k for k in keys if not (getattr(self, k) or "").strip()]
    def completeness_missing_labels(self):
        return [self.FIELD_LABELS[k] for k in self.completeness_missing()]

class FarmExtraField(models.Model):
    """User-defined extra label/value pair on a farm (Task 1).

    Key is unique per farm case-insensitively (DB functional constraint);
    form-level validation gives the friendly error before the DB is hit.
    """

    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="extra_fields")
    key = models.CharField(max_length=64)
    value = models.CharField(max_length=500)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(Lower("key"), "farm", name="uniq_extra_farm_lowerkey"),
            models.CheckConstraint(condition=~models.Q(key=""), name="extra_key_nonempty"),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext_lazy as _

        super().clean()
        if self.key is not None:
            self.key = str(self.key).strip()
        if not self.key:
            raise ValidationError({"key": _("A megnevezés kötelező.")})

    def __str__(self):
        return f"{self.key}={self.value}"


class FarmProfileSnapshot(models.Model):
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="snapshots")
    profile_version = models.PositiveIntegerField()
    profile_schema_version = models.PositiveSmallIntegerField(default=1)
    profile_json = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["farm","profile_version"], name="uniq_snap_farm_ver"),
            models.UniqueConstraint(fields=["id","farm"], name="uniq_snap_id_farm"),
            models.CheckConstraint(condition=models.Q(profile_schema_version=1), name="snap_schema_v1"),
        ]
        ordering = ["-created_at"]
    def save(self, *args, **kwargs):
        if self.pk is not None and FarmProfileSnapshot.objects.filter(pk=self.pk).exists():
            raise ValueError(_("A pillanatkép megváltoztathatatlan."))
        super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValueError(_("A pillanatkép közvetlenül nem törölhető."))

class AdviceRun(models.Model):
    STATUS = [("pending",_("függőben")),("success",_("sikeres")),("failed",_("sikertelen"))]
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="advice_runs")
    initiated_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="advice_runs")
    profile_snapshot = models.ForeignKey(FarmProfileSnapshot, on_delete=models.CASCADE, related_name="advice_runs")
    idempotency_key = models.CharField(max_length=80)
    language = models.CharField(max_length=10, choices=[("hu", "Magyar"), ("en", "English")], default="hu")
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    source = models.CharField(max_length=16, default="live")  # live | seed
    requested_model = models.CharField(max_length=120)
    actual_model = models.CharField(max_length=120, null=True, blank=True)
    prompt_version = models.CharField(max_length=40, default="advice_prompt_v1")
    output_schema_version = models.CharField(max_length=40, default="advice_output_v1")
    result_assumptions = models.JSONField(null=True, blank=True)
    result_focused_questions = models.JSONField(null=True, blank=True)
    result_is_general = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message_safe = models.CharField(max_length=300, null=True, blank=True)
    usage_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    usage_output_tokens = models.PositiveIntegerField(null=True, blank=True)
    attempt_started_at = models.DateTimeField(default=timezone.now)
    # Dispatch authorization boundary (Blocking2): set only by authorize_dispatch
    # in its own short transaction after re-loading current consent. A pending
    # run with this null was merely reserved, never authorized for dispatch.
    # Once set, the provider call is "dispatch-authorized": it may be sent and,
    # once in flight, cannot be unsent; later revocation affects future calls.
    dispatch_authorized_at = models.DateTimeField(null=True, blank=True)
    dispatch_consent_event_id = models.BigIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["farm","idempotency_key"], name="uniq_run_farm_idem"),
            models.UniqueConstraint(fields=["id","farm"], name="uniq_run_id_farm"),
        ]
        ordering = ["-attempt_started_at"]
        indexes = [models.Index(fields=["farm","attempt_started_at"]), models.Index(fields=["farm","status","attempt_started_at"])]

class AdviceStep(models.Model):
    advice_run = models.ForeignKey(AdviceRun, on_delete=models.CASCADE, related_name="steps")
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="advice_steps")
    position = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=160)
    action_text = models.TextField()
    rationale = models.TextField()
    assumptions = models.JSONField(default=list)
    risk_note = models.TextField()
    expert_referral = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["advice_run","position"], name="uniq_step_run_pos"),
            models.UniqueConstraint(fields=["id","farm"], name="uniq_step_id_farm"),
            models.CheckConstraint(condition=models.Q(position__gte=1, position__lte=3), name="step_pos_1_3"),
        ]
        ordering = ["position"]

class FarmAction(models.Model):
    def clean(self):
        super().clean()
        if self.source_step_id and self.source_step is not None and self.source_step.farm_id != self.farm_id:
            from django.core.exceptions import ValidationError
            raise ValidationError(_("A forráslépés csak azonos farmhoz tartozhat."))
    def save(self, *args, **kwargs):
        if self.source_step_id:
            # same-farm ownership validator (SQLite adaptation of composite FK intent)
            step_farm = AdviceStep.objects.filter(pk=self.source_step_id).values_list("farm_id", flat=True).first()
            if step_farm is not None and step_farm != self.farm_id:
                from django.core.exceptions import ValidationError
                raise ValidationError(_("A forráslépés csak azonos farmhoz tartozhat."))
            self.source_step_farm_id = self.farm_id
        super().save(*args, **kwargs)

    STATUS = [("open",_("Nyitott")),("done",_("Kész"))]
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="actions")
    source_step = models.ForeignKey(AdviceStep, on_delete=models.SET_NULL, null=True, blank=True, related_name="actions")
    source_step_farm_id = models.BigIntegerField(null=True, blank=True)
    text = models.TextField()
    status = models.CharField(max_length=16, choices=STATUS, default="open")
    done_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(source_step__isnull=True, source_step_farm_id__isnull=True) | (models.Q(source_step__isnull=False, source_step_farm_id__isnull=False))), name="action_src_consistent")]
        ordering = ["-created_at"]

class ConsentEvent(models.Model):
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="consent_events")
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="consent_events")
    scope = models.CharField(max_length=48, default="model_cloud_processing")
    event_type = models.CharField(max_length=16)  # granted | revoked
    policy_version = models.CharField(max_length=40, default="cloud_disclosure_v1")
    occurred_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(scope="model_cloud_processing"), name="consent_scope_fixed"), models.CheckConstraint(condition=models.Q(event_type__in=["granted","revoked"]), name="consent_type_chk")]
        ordering = ["-occurred_at","-id"]
    def save(self, *args, **kwargs):
        if self.pk is not None and ConsentEvent.objects.filter(pk=self.pk).exists():
            raise ValueError(_("A hozzájárulási esemény megváltoztathatatlan."))
        super().save(*args, **kwargs)

def current_consent_granted(farm):
    ev = ConsentEvent.objects.filter(farm=farm, scope="model_cloud_processing").order_by("-occurred_at","-id").first()
    return bool(ev and ev.event_type == "granted")

def latest_consent_event(farm):
    return ConsentEvent.objects.filter(farm=farm, scope="model_cloud_processing").order_by("-occurred_at","-id").first()

class LoginThrottle(models.Model):
    """Shared login-failure throttle stored in SQLite (all gunicorn workers
    share the same DB file), replacing the per-process cache counter.
    key_hash is SHA-256 of "ip|email" so no raw credential identifier is
    needed as the lookup key beyond the hash. Window is fixed from the
    first failure; expired rows are deleted opportunistically on access."""
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    fail_count = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        indexes = [models.Index(fields=["expires_at"])]

class Membership(models.Model):
    ROLE_CHOICES = [("manager", _("Menedzser")), ("worker", _("Munkatárs")), ("viewer", _("Megfigyelő"))]
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["farm", "user"], name="uniq_membership_farm_user")]

class Invitation(models.Model):
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField(max_length=254)
    role = models.CharField(max_length=16, choices=Membership.ROLE_CHOICES, default="viewer")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_invitations")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)
    def is_usable(self):
        return self.used_at is None and self.revoked_at is None and self.expires_at > timezone.now()

class AuditEvent(models.Model):
    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="audit_events")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="audit_events")
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=80, blank=True, default="")
    object_id = models.CharField(max_length=80, blank=True, default="")
    meta = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at", "-id"]
    def save(self, *args, **kwargs):
        if self.pk is not None and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValueError("Az audit-esemény megváltoztathatatlan.")
        super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValueError("Az audit-esemény nem törölhető.")

# Stage 2 domain models (tenant-scoped). Imported here so Django registers
# them under the portal app; domain_models uses lazy FK strings (no cycle).
from .domain_models import (  # noqa: E402,F401
    FarmLocation, FarmProperty, Equipment, EquipmentProperty, CropCycle, WorkItem,
    FarmingEvent, LivestockGroup, LivestockEvent, ExternalContact, RuleCategory, RuleDefinition,
    RuleActivation, Document, FarmDocument, NewsSource, SowingComparison,
    SowingComparisonAttempt,
    RULE_BODY_MAX_LENGTH,
    EQUIPMENT_CAPABILITY_CHOICES, PROPERTY_KIND_CHOICES, CROP_STATE_CHOICES,
    LIVESTOCK_SPECIES_CHOICES, LIVESTOCK_EVENT_KINDS,
    LIVESTOCK_WORK_KINDS, WORK_STATUS_CHOICES,
)
from .chat_models import ChatAttempt, ChatProposal  # noqa: E402,F401
from .agent.models import AgentRun, AgentDocGrant, AgentCall  # noqa: E402,F401
