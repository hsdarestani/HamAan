# safety/models.py
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class UserRestriction(TimeStampedModel):
    """
    App-level restriction controls.
    Use this to throttle, shadow-ban, or block abusive users without deleting data.
    """

    class Level(models.TextChoices):
        NONE = "NONE", "None"
        THROTTLE = "THROTTLE", "Throttle"
        LIMITED = "LIMITED", "Limited"
        BLOCKED = "BLOCKED", "Blocked"
        SHADOW = "SHADOW", "Shadow"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="restriction",
        db_index=True,
    )

    level = models.CharField(max_length=12, choices=Level.choices, default=Level.NONE, db_index=True)

    reason = models.CharField(max_length=255, blank=True, default="")
    internal_note = models.TextField(blank=True, default="")

    # Optional expiration
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Throttling knobs (effective when level=THROTTLE or LIMITED)
    max_msgs_per_minute = models.PositiveSmallIntegerField(default=20, validators=[MinValueValidator(1)])
    max_msgs_per_day = models.PositiveIntegerField(default=500, validators=[MinValueValidator(1)])

    # Behavior toggles
    block_initiation = models.BooleanField(default=False, db_index=True)
    block_media = models.BooleanField(default=False, db_index=True)
    block_purchases = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "safety_userrestriction"
        indexes = [
            models.Index(fields=["level"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["block_initiation"]),
        ]

    def __str__(self) -> str:
        return f"UserRestriction(user={self.user_id}, level={self.level})"

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())


class SafetyEvent(TimeStampedModel):
    """
    Logs safety-relevant events from:
      - user messages (keyword flags, spam patterns)
      - bot replies (policy fallback triggered)
      - payment abuse patterns
      - admin actions

    Keep payload small.
    """

    class EventType(models.TextChoices):
        SPAM = "SPAM", "Spam"
        ABUSE = "ABUSE", "Abuse"
        SELF_HARM_RISK = "SELF_HARM_RISK", "Self harm risk"
        HARASSMENT = "HARASSMENT", "Harassment"
        ILLEGAL = "ILLEGAL", "Illegal"
        PRIVACY = "PRIVACY", "Privacy"
        PAYMENT_FRAUD = "PAYMENT_FRAUD", "Payment fraud"
        RATE_LIMIT = "RATE_LIMIT", "Rate limit"
        OTHER = "OTHER", "Other"

    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="safety_events",
        db_index=True,
    )

    # Optional linkage to chat objects (string FK to avoid import loops)
    conversation_id = models.UUIDField(null=True, blank=True, db_index=True)
    message_id = models.UUIDField(null=True, blank=True, db_index=True)

    event_type = models.CharField(max_length=20, choices=EventType.choices, default=EventType.OTHER, db_index=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.LOW, db_index=True)

    # A short classifier output / rule name
    rule_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # What happened (short)
    summary = models.CharField(max_length=255, blank=True, default="")

    # Store small structured context (avoid storing long text; never store secrets)
    payload = models.JSONField(default=dict, blank=True)

    # Whether this event led to action
    action_taken = models.BooleanField(default=False, db_index=True)
    action_note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "safety_safetyevent"
        indexes = [
            models.Index(fields=["event_type", "created_at"]),
            models.Index(fields=["severity", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["conversation_id"]),
            models.Index(fields=["message_id"]),
        ]

    def __str__(self) -> str:
        return f"SafetyEvent({self.event_type}/{self.severity})"


class BlockedPhrase(TimeStampedModel):
    """
    Optional: simple moderation list (admin-managed).
    Use it for:
      - disallowed phrases (e.g., unsafe claims)
      - scam keywords
      - doxxing patterns (very light)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    phrase = models.CharField(max_length=128, unique=True, db_index=True)
    event_type = models.CharField(max_length=20, default=SafetyEvent.EventType.OTHER, db_index=True)
    severity = models.CharField(max_length=10, default=SafetyEvent.Severity.LOW, db_index=True)

    is_active = models.BooleanField(default=True, db_index=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "safety_blockedphrase"
        indexes = [
            models.Index(fields=["is_active", "event_type"]),
            models.Index(fields=["phrase"]),
        ]
        constraints = [
            models.CheckConstraint(check=~Q(phrase=""), name="safety_blockedphrase_phrase_nonempty"),
        ]

    def __str__(self) -> str:
        return f"BlockedPhrase({self.phrase})"

