"""Stage5 CHAT models: ChatAttempt + ChatProposal.

Write-gating: provider-suggested writes NEVER execute directly.
They are stored as pending ChatProposal rows and only applied by an
explicit, CSRF-protected UI confirmation POST (see chat.confirm_proposal).
"""
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ChatConversation(models.Model):
    """Stable default conversation per (farm, owner user).

    English comments per project convention. One row with key="default"
    per (farm, owner); async attempts attach here so history stays
    private to initiated_by (see docs/CHAT_ASYNC.md).
    """
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE,
                             related_name="chat_conversations")
    owner = models.ForeignKey("portal.User", on_delete=models.CASCADE,
                              related_name="chat_conversations")
    key = models.CharField(max_length=32, default="default")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["farm", "owner", "key"],
                                    name="uniq_chatconv_farm_owner_key"),
        ]
        indexes = [models.Index(fields=["farm", "owner"])]


class ChatAttempt(models.Model):
    STATUS = [("pending", _("függőben")), ("success", _("sikeres")), ("failed", _("sikertelen"))]
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="chat_attempts")
    initiated_by = models.ForeignKey("portal.User", on_delete=models.CASCADE, related_name="chat_attempts")
    conversation = models.ForeignKey(ChatConversation, null=True, blank=True,
                                     on_delete=models.CASCADE, related_name="attempts")
    # Tombstone delete marker; a set row is never claimed or resurrected.
    deleted_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=80)
    language = models.CharField(max_length=10, choices=[("hu", "Magyar"), ("en", "English")], default="hu")
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    source = models.CharField(max_length=16, default="live")
    requested_model = models.CharField(max_length=120)
    actual_model = models.CharField(max_length=120, null=True, blank=True)
    prompt_version = models.CharField(max_length=40, default="chat_prompt_v1")
    output_schema_version = models.CharField(max_length=40, default="chat_output_v1")
    user_message = models.TextField()
    # Parcel scope frozen at reservation; never mutated afterwards.
    selection_snapshot = models.JSONField(default=dict)
    answer_text = models.TextField(null=True, blank=True)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message_safe = models.CharField(max_length=300, null=True, blank=True)
    usage_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    usage_output_tokens = models.PositiveIntegerField(null=True, blank=True)
    attempt_started_at = models.DateTimeField(default=timezone.now)
    # Dispatch authorization boundary (same semantics as AdviceRun):
    # set only by authorize_chat_dispatch in its own short transaction after
    # re-loading current consent. Null = merely reserved, never dispatchable.
    dispatch_authorized_at = models.DateTimeField(null=True, blank=True)
    dispatch_consent_event_id = models.BigIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.pk is not None:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or "selection_snapshot" in update_fields:
                old = (ChatAttempt.objects.filter(pk=self.pk)
                       .values_list("selection_snapshot", flat=True).first())
                if old is not None and old != self.selection_snapshot:
                    raise ValueError("selection_snapshot is immutable")
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["farm", "idempotency_key"], name="uniq_chat_farm_idem"),
            models.UniqueConstraint(fields=["id", "farm"], name="uniq_chat_id_farm"),
        ]
        ordering = ["-attempt_started_at"]
        indexes = [models.Index(fields=["farm", "attempt_started_at"]),
                   models.Index(fields=["farm", "status", "attempt_started_at"])]


class ChatProposal(models.Model):
    KIND_CHOICES = [("action_create", _("Teendő létrehozása")), ("action_toggle", _("Teendő állapotváltása")),
                    ("plans_generate", _("Tervkészítés")), ("navigate", _("Navigáció"))]
    STATUS = [("pending", _("függőben")), ("done", _("végrehajtva")), ("failed", _("sikertelen"))]
    attempt = models.ForeignKey(ChatAttempt, on_delete=models.CASCADE, related_name="proposals")
    farm = models.ForeignKey("portal.Farm", on_delete=models.CASCADE, related_name="chat_proposals")
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    # Typed payloads (allowlist): action_create -> payload_text;
    # action_toggle -> payload_action; plans_generate -> none; navigate -> payload_link.
    payload_text = models.TextField(null=True, blank=True)
    payload_action = models.ForeignKey("portal.FarmAction", on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="chat_proposals")
    payload_link = models.CharField(max_length=64, null=True, blank=True)
    # Toggle snapshot: records target text + status at proposal creation so
    # confirm can reject stale state and apply the desired (toggled) state.
    snapshot_text = models.TextField(null=True, blank=True)
    snapshot_status = models.CharField(max_length=16, null=True, blank=True)
    result_action = models.ForeignKey("portal.FarmAction", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="chat_created")

    def save(self, *args, **kwargs):
        if (self.kind == "action_toggle" and
                (self.snapshot_text is None or self.snapshot_status is None)):
            try:
                tid = int((self.payload_text or "").strip())
            except (ValueError, AttributeError):
                tid = None
            if tid and self.farm_id:
                from .models import FarmAction as _FA
                tgt = _FA.objects.filter(pk=tid, farm_id=self.farm_id).first()
                if tgt is not None:
                    self.snapshot_text = tgt.text
                    self.snapshot_status = tgt.status
        super().save(*args, **kwargs)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["attempt", "position"], name="uniq_chatprop_attempt_pos"),
            models.UniqueConstraint(fields=["id", "farm"], name="uniq_chatprop_id_farm"),
        ]
        ordering = ["position"]
