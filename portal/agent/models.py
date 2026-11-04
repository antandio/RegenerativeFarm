"""WP1 durable agent run: identity, immutable manifest, candidate, critic, usage.

All source text stays untrusted data. The model never writes domain rows:
this slice stores only the run itself, its manifests, the structured
candidate, critic findings and the per-call usage ledger.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class AgentRun(models.Model):
    """One bounded planning run through stages 1-7 (WP1 plans, WP2 executes)."""

    STATUS_CHOICES = [
        ("queued", "queued"),
        ("running", "running"),
        ("preview", "preview"),
        ("blocked", "blocked"),
        ("done", "done"),
        ("error", "error"),
        ("outcome_unknown", "outcome_unknown"),
        ("cancelled", "cancelled"),
        ("deleted", "deleted"),
    ]

    farm = models.ForeignKey(
        "portal.Farm", on_delete=models.CASCADE, related_name="agent_runs")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="agent_runs")
    idempotency_key = models.CharField(max_length=80)
    payload_hash = models.CharField(max_length=64)
    goal = models.TextField()
    language = models.CharField(max_length=10, default="hu")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="queued")
    # Last reached loop stage (1-7); 0 means not started.
    stage = models.PositiveSmallIntegerField(default=0)
    # Immutable once set: sourced manifest, policy binding, candidate, critic.
    context_manifest = models.JSONField(default=dict)
    policy_version = models.CharField(max_length=40, blank=True, default="")
    policy_sha256 = models.CharField(max_length=64, blank=True, default="")
    policy_cited_sections = models.JSONField(default=list)
    candidate = models.JSONField(null=True, blank=True)
    candidate_revision = models.PositiveSmallIntegerField(default=0)
    critic_findings = models.JSONField(default=list)
    budgets = models.JSONField(default=dict)
    usage_summary = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message_safe = models.CharField(
        max_length=300, null=True, blank=True)
    # Revalidation fingerprint: consent/policy/source revisions seen at the
    # last gate; WP2 apply must match a fresh fingerprint before executing.
    revalidation = models.JSONField(default=dict)
    # Async lifecycle: requested scope snapshot (selected property/doc ids,
    # goal text hash, language) stored at enqueue time; stage_events holds
    # safe progress entries only (stage, code, timestamp), never hidden CoT.
    input_snapshot = models.JSONField(default=dict)
    stage_events = models.JSONField(default=list)
    dispatch_authorized_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "idempotency_key"],
                name="uniq_agentrun_farm_idem"),
            models.UniqueConstraint(
                fields=["id", "farm"], name="uniq_agentrun_id_farm"),
            models.CheckConstraint(
                condition=models.Q(stage__gte=0, stage__lte=7),
                name="agentrun_stage_1_7"),
        ]
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["farm", "status", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        # First binding of empty to nonempty is allowed once; later edits
        # to set values stay immutable.
        if self.pk is not None:
            locked = type(self).objects.filter(pk=self.pk).first()
            if locked is not None:
                for field in ("context_manifest", "policy_version",
                              "policy_sha256"):
                    old = getattr(locked, field)
                    new = getattr(self, field)
                    if old != new:
                        old_empty = old in (None, "", {}, [])
                        new_empty = new in (None, "", {}, [])
                        if not (old_empty and not new_empty):
                            raise ValueError(
                                "The context/source manifest and policy "
                                "binding are immutable once set.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Soft-delete only: runs stay auditable; content is hidden by status.
        self.status = "deleted"
        self.save(update_fields=["status", "updated_at"])

    def __str__(self):
        return f"agent-run {self.pk} ({self.status})"


class AgentDocGrant(models.Model):
    """Per-run, per-document cloud-processing permission (explicit select).

    Separate from the farm-wide cloud consent (ConsentEvent): a document
    section may enter the model context only when the farm consent is
    currently granted AND a grant row exists for this run + document.
    """

    run = models.ForeignKey(
        AgentRun, on_delete=models.CASCADE, related_name="doc_grants")
    document = models.ForeignKey(
        "portal.Document", on_delete=models.CASCADE,
        related_name="agent_grants")
    granted = models.BooleanField(default=False)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="agent_doc_grants")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "document"],
                name="uniq_agentdocgrant_run_doc"),
        ]

    def __str__(self):
        return f"{self.run_id}:{self.document_id}:{self.granted}"


class AgentCall(models.Model):
    """Per-call usage ledger: one reserved row per model dispatch.

    Unknown token counts and unknown cost stay NULL (never zero): NULL
    means "unknown", zero would falsely claim a free call.
    """

    KIND_CHOICES = [
        ("planner", "planner"),
        ("critic", "critic"),
        ("agri_classify", "agri_classify"),
    ]
    STATUS_CHOICES = [
        ("reserved", "reserved"),
        ("success", "success"),
        ("failed", "failed"),
        ("unknown", "unknown"),
    ]

    run = models.ForeignKey(
        AgentRun, on_delete=models.CASCADE, related_name="calls")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    model = models.CharField(max_length=120)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="reserved")
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    cost_estimate = models.FloatField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at", "id"]

    def __str__(self):
        return f"{self.kind} {self.status} ({self.run_id})"


class AgentChangeset(models.Model):
    """Immutable approval envelope for a bounded domain write batch.

    Preview rows never touch domain tables. Apply moves preview to
    applying with one conditional UPDATE, then validates and writes
    inside a single short transaction. Payload, commands and the
    before snapshot stay immutable once stored.
    """

    STATE_CHOICES = [
        ("preview", "preview"),
        ("applying", "applying"),
        ("applied", "applied"),
        ("verified", "verified"),
        ("rejected", "rejected"),
    ]

    farm = models.ForeignKey(
        "portal.Farm", on_delete=models.CASCADE,
        related_name="agent_changesets")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="agent_changesets")
    run = models.ForeignKey(
        AgentRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="changesets")
    idempotency_key = models.CharField(max_length=80)
    payload_hash = models.CharField(max_length=64)
    commands = models.JSONField(default=list)
    before_snapshot = models.JSONField(default=dict)
    before_hash = models.CharField(max_length=64, default="", blank=True)
    after_snapshot = models.JSONField(default=dict)
    after_hash = models.CharField(max_length=64, default="", blank=True)
    source_vector = models.JSONField(default=list)
    policy_version = models.CharField(max_length=40, default="", blank=True)
    policy_sha256 = models.CharField(max_length=64, default="", blank=True)
    run_critic_sha256 = models.CharField(
        max_length=64, default="", blank=True)
    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default="preview")
    expires_at = models.DateTimeField()
    receipt = models.JSONField(default=dict)
    audit_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["farm", "idempotency_key"],
                name="uniq_changeset_farm_idem"),
        ]
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["farm", "state", "created_at"]),
        ]

    # Payload, binding and provenance fields are immutable once stored;
    # only the apply pipeline moves state forward and writes the
    # after/receipt fields. Verified rows accept metadata-only saves.
    IMMUTABLE_FIELDS = ("payload_hash", "commands", "before_snapshot",
                        "before_hash", "farm_id", "actor_id", "run_id",
                        "idempotency_key", "source_vector", "policy_version",
                        "policy_sha256", "run_critic_sha256", "expires_at")
    TRANSITIONS = {"preview": ("rejected",), "applying": ("applied",),
                   "applied": ("verified",), "verified": (),
                   "rejected": ()}

    def save(self, *args, **kwargs):
        if self.pk is not None:
            locked = type(self).objects.filter(pk=self.pk).first()
            if locked is not None:
                for field in self.IMMUTABLE_FIELDS:
                    if getattr(locked, field) != getattr(self, field):
                        raise ValueError(
                            "Changeset payload and snapshots are immutable.")
                if getattr(locked, "state") != self.state and \
                        self.state not in self.TRANSITIONS.get(
                            locked.state, ()):
                    raise ValueError(
                        "Changeset state transition is not allowed.")
                if locked.state in ("verified", "rejected"):
                    for field in ("after_snapshot", "after_hash",
                                  "receipt", "audit_ids", "state"):
                        if getattr(locked, field) != getattr(self, field):
                            raise ValueError(
                                "Verified changesets are read-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Changesets are immutable and cannot be deleted.")

    def __str__(self):
        return f"changeset {self.pk} ({self.state})"
