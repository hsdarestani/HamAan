# automation/models.py
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class InitiationRule(TimeStampedModel):
    """
    Per-bot initiation settings (global).
    The actual decision logic lives in the initiation engine / scheduler.

    NOTE: We keep rules numeric + small. No long text logic here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    bot = models.OneToOneField(
        "persona.Bot",
        on_delete=models.CASCADE,
        related_name="initiation_rule",
        db_index=True,
    )

    enabled = models.BooleanField(default=True, db_index=True)

    # Cadence controls
    cooldown_hours = models.PositiveSmallIntegerField(default=36)
    max_per_day = models.PositiveSmallIntegerField(default=1)
    max_per_week = models.PositiveSmallIntegerField(default=3)

    # Eligibility thresholds (per user relationship state)
    min_familiarity = models.FloatField(default=0.10)
    min_trust = models.FloatField(default=0.05)

    # Basic time-window (server time). If you want per-user quiet hours, handle via users.UserPrefs.
    allowed_start_hour = models.PositiveSmallIntegerField(default=10)  # 0..23
    allowed_end_hour = models.PositiveSmallIntegerField(default=23)    # 0..23

    # Content style constraints
    max_chars = models.PositiveSmallIntegerField(default=180)
    allow_question = models.BooleanField(default=False)

    # Optional message templates (keep short; in production you'd do a generator)
    # Example: ["یه لحظه یادم افتادی.", "امروز ساکت بودی."]
    templates = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "automation_initiationrule"
        indexes = [
            models.Index(fields=["enabled"]),
            models.Index(fields=["cooldown_hours"]),
        ]

    def __str__(self) -> str:
        return f"InitiationRule(bot={self.bot_id}, enabled={self.enabled})"


class InitiationEvent(TimeStampedModel):
    """
    A log of initiation attempts/sends.
    Used for rate limiting, analytics, and idempotency.
    """

    class Trigger(models.TextChoices):
        SCHEDULER = "SCHEDULER", "Scheduler"
        MANUAL = "MANUAL", "Manual"
        RECOVERY = "RECOVERY", "Recovery"

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "Planned"
        SENT = "SENT", "Sent"
        SKIPPED = "SKIPPED", "Skipped"
        FAILED = "FAILED", "Failed"
        ACKED = "ACKED", "Acked"  # user replied after initiation

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    state = models.ForeignKey(
        "persona.BotUserState",
        on_delete=models.CASCADE,
        related_name="initiation_events",
        db_index=True,
    )

    bot = models.ForeignKey(
        "persona.Bot",
        on_delete=models.CASCADE,
        related_name="initiation_events",
        db_index=True,
    )

    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="initiation_events",
        db_index=True,
    )

    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.SCHEDULER, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PLANNED, db_index=True)

    # When it was intended to be sent vs actually sent
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Content (what we sent)
    message_text = models.CharField(max_length=220, blank=True, default="")

    # Telegram metadata (for debugging)
    telegram_message_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    # User reaction
    user_replied = models.BooleanField(default=False, db_index=True)
    user_replied_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Idempotency key to avoid duplicates (e.g., per user per day)
    idempotency_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # Optional failure details
    error_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    error_message = models.CharField(max_length=255, blank=True, default="")

    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "automation_initiationevent"
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["bot", "created_at"]),
            models.Index(fields=["scheduled_for"]),
            models.Index(fields=["sent_at"]),
        ]
        constraints = [
            # If idempotency_key is present, enforce uniqueness per state (bot-user pair)
            models.UniqueConstraint(
                fields=["state", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="automation_unique_initiation_idem_per_state",
            ),
        ]

    def __str__(self) -> str:
        return f"InitiationEvent(state={self.state_id}, status={self.status})"


class ScheduledJob(TimeStampedModel):
    """
    Optional lightweight job ledger (useful if you don't want to rely only on Celery backend state).
    Not required for MVP, but helps tracking scheduled executions.

    Example uses:
      - "initiation_sweep" runs every X minutes
      - "memory_decay" runs daily
    """

    class JobType(models.TextChoices):
        INITIATION_SWEEP = "INITIATION_SWEEP", "Initiation sweep"
        MEMORY_DECAY = "MEMORY_DECAY", "Memory decay"
        HOUSEKEEPING = "HOUSEKEEPING", "Housekeeping"

    class Status(models.TextChoices):
        OK = "OK", "OK"
        ERROR = "ERROR", "Error"
        RUNNING = "RUNNING", "Running"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    job_type = models.CharField(max_length=24, choices=JobType.choices, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OK, db_index=True)

    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)

    error_message = models.CharField(max_length=255, blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "automation_scheduledjob"
        indexes = [
            models.Index(fields=["job_type", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"ScheduledJob({self.job_type}:{self.status})"

