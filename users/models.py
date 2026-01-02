# users/models.py
from __future__ import annotations

import uuid
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """
    Reusable timestamps. Keep it simple for MVP.
    """
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class UserManager(BaseUserManager):
    """
    Minimal custom user manager.
    We authenticate by `telegram_id` (not email/username).
    """

    use_in_migrations = True

    def _create_user(self, telegram_id: int, password: str | None = None, **extra_fields):
        if telegram_id is None:
            raise ValueError("telegram_id is required")

        extra_fields.setdefault("is_active", True)
        user = self.model(telegram_id=telegram_id, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, telegram_id: int, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(telegram_id=telegram_id, password=password, **extra_fields)

    def create_superuser(self, telegram_id: int, password: str, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(telegram_id=telegram_id, password=password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    Core user entity (Telegram-first).
    IMPORTANT:
      - Set in settings.py:
          AUTH_USER_MODEL = "users.User"
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Telegram identifiers
    telegram_id = models.BigIntegerField(unique=True, db_index=True)
    telegram_username = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # Telegram profile (best-effort; may change over time)
    first_name = models.CharField(max_length=128, blank=True, default="")
    last_name = models.CharField(max_length=128, blank=True, default="")
    language_code = models.CharField(max_length=12, blank=True, default="", db_index=True)

    # Operational flags
    is_active = models.BooleanField(default=True, db_index=True)
    is_staff = models.BooleanField(default=False, db_index=True)  # for Django admin access
    is_blocked = models.BooleanField(default=False, db_index=True)  # app-level block
    block_reason = models.CharField(max_length=255, blank=True, default="")

    # Activity tracking (useful for initiation rules / retention)
    first_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Locale/time settings (keep simple; can be refined later)
    timezone = models.CharField(max_length=64, blank=True, default="Asia/Tehran", db_index=True)

    # Privacy / user controls
    marketing_opt_in = models.BooleanField(default=False)
    initiation_opt_in = models.BooleanField(default=True)  # allow the bot to initiate messages

    objects = UserManager()

    USERNAME_FIELD = "telegram_id"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "users_user"
        indexes = [
            models.Index(fields=["is_active", "is_blocked"]),
            models.Index(fields=["last_seen_at"]),
            models.Index(fields=["telegram_username"]),
        ]

    def __str__(self) -> str:
        u = f"@{self.telegram_username}" if self.telegram_username else str(self.telegram_id)
        return f"User({u})"


class UserPrefs(TimeStampedModel):
    """
    Per-user silent personalization knobs (NOT shown to the user).
    Keep it small and predictable; no long text blobs.
    """

    class ReplyLength(models.TextChoices):
        VERY_SHORT = "VERY_SHORT", "Very short"
        SHORT = "SHORT", "Short"
        MEDIUM = "MEDIUM", "Medium"

    class QuestionTolerance(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"

    class Tone(models.TextChoices):
        VERY_PLAIN = "VERY_PLAIN", "Very plain"
        PLAIN = "PLAIN", "Plain"
        WARM = "WARM", "Warm"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="prefs",
        primary_key=True,
    )

    # Core personalization (MVP)
    reply_length = models.CharField(max_length=16, choices=ReplyLength.choices, default=ReplyLength.SHORT)
    question_tolerance = models.CharField(max_length=8, choices=QuestionTolerance.choices, default=QuestionTolerance.LOW)
    tone = models.CharField(max_length=16, choices=Tone.choices, default=Tone.VERY_PLAIN)

    # Soft knobs (0..1)
    emotional_distance = models.FloatField(
        default=0.6,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Higher = more distance, less emotional language.",
    )
    verbosity = models.FloatField(
        default=0.3,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Higher = longer responses (but still bounded by contract).",
    )

    # Interaction dynamics
    prefers_initiation = models.BooleanField(default=True)  # can diverge from user.initiation_opt_in
    initiation_cooldown_hours = models.PositiveSmallIntegerField(default=36)

    # “Quiet hours” window (simple)
    quiet_hours_enabled = models.BooleanField(default=False)
    quiet_hours_start = models.TimeField(null=True, blank=True)  # local time per user.timezone
    quiet_hours_end = models.TimeField(null=True, blank=True)

    # Lightweight counters for adaptation (MVP)
    total_user_messages = models.PositiveIntegerField(default=0)
    total_bot_replies = models.PositiveIntegerField(default=0)
    last_profile_refresh_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Compact, internal-only rules (tiny JSON)
    # Example:
    # {"avoid_topics":["therapy"], "max_questions_per_reply":1, "dont_use_phrases":["کاملاً می‌فهمم"]}
    style_rules = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "users_userprefs"

    def __str__(self) -> str:
        return f"UserPrefs({self.user_id})"

