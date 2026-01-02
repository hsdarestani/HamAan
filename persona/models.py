# persona/models.py
from __future__ import annotations

import uuid
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class Bot(TimeStampedModel):
    """
    A bot persona "template".
    One Bot can serve many users. The per-user evolution lives in BotUserState.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    code = models.SlugField(max_length=32, unique=True, db_index=True)  # e.g. "hamdam-01"
    display_name = models.CharField(max_length=48, db_index=True)        # shown to user if needed
    is_active = models.BooleanField(default=True, db_index=True)

    # Prompt contract / system prompt references
    base_prompt_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    base_prompt_text = models.TextField(blank=True, default="")  # optional inline prompt text (MVP)

    # Optional: product settings
    default_language = models.CharField(max_length=12, blank=True, default="fa", db_index=True)
    avatar_key = models.CharField(max_length=64, blank=True, default="")  # points to a static asset later

    # Operational knobs
    max_output_chars = models.PositiveSmallIntegerField(default=350)  # hard upper bound for "short answers"
    max_questions_per_reply = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = "persona_bot"
        indexes = [
            models.Index(fields=["is_active", "code"]),
            models.Index(fields=["display_name"]),
        ]

    def __str__(self) -> str:
        return f"Bot({self.code})"


class BotIdentity(TimeStampedModel):
    """
    Global evolving identity of a bot (shared baseline).
    Keep it numeric and small. Do NOT store long lore here.
    """

    bot = models.OneToOneField(Bot, on_delete=models.CASCADE, related_name="identity", primary_key=True)

    # "Core vibe"
    core_tone = models.CharField(
        max_length=16,
        choices=[
            ("QUIET", "QUIET"),
            ("PLAIN", "PLAIN"),
            ("WARM", "WARM"),
            ("DRY", "DRY"),
        ],
        default="QUIET",
        db_index=True,
    )

    # Background anchor (NOT a story text; just a seed label)
    background_seed = models.CharField(max_length=32, default="UNKNOWN_PAST", db_index=True)

    # Identity knobs (0..1). Keep away from extremes to preserve "imperfection".
    self_confidence = models.FloatField(default=0.35, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    openness = models.FloatField(default=0.25, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    talkativeness = models.FloatField(default=0.30, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    emotional_clarity = models.FloatField(default=0.25, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])

    # “Forgetting” style
    memory_strength = models.FloatField(
        default=0.60,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Higher means better recall; never perfect.",
    )
    memory_noise = models.FloatField(
        default=0.15,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Higher means more mistakes/ambiguity in recall hints.",
    )

    # Constraints baked into persona
    avoids_advice = models.BooleanField(default=True)
    avoids_therapy_tone = models.BooleanField(default=True)
    avoids_omniscience = models.BooleanField(default=True)

    class Meta:
        db_table = "persona_botidentity"
        indexes = [
            models.Index(fields=["core_tone"]),
            models.Index(fields=["background_seed"]),
        ]

    def __str__(self) -> str:
        return f"BotIdentity({self.bot_id})"


class BotUserState(TimeStampedModel):
    """
    Per-user relationship state with the bot (this is where "it grows with interaction").
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    bot = models.ForeignKey(Bot, on_delete=models.CASCADE, related_name="user_states", db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bot_states", db_index=True)

    # Relationship dynamics (0..1)
    familiarity = models.FloatField(default=0.10, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    trust = models.FloatField(default=0.10, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    emotional_closeness = models.FloatField(default=0.10, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])

    # Interaction style adaptation (0..1)
    user_pref_verbosity = models.FloatField(default=0.30, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    user_pref_questions = models.FloatField(default=0.20, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])

    # Session markers
    last_user_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_bot_reply_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_initiation_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Simple counters
    total_user_messages = models.PositiveIntegerField(default=0)
    total_bot_replies = models.PositiveIntegerField(default=0)
    shared_silence = models.PositiveIntegerField(default=0, help_text="Count of times user did not reply after bot msg.")
    conflict_count = models.PositiveIntegerField(default=0)

    # Compact per-user "soft rules" discovered (internal-only)
    # Example: {"dont_use":["کاملاً می‌فهمم"], "max_questions":1, "reply_length":"short"}
    style_rules = models.JSONField(default=dict, blank=True)

    # Whether user allows initiation for THIS bot (can differ from global user setting)
    initiation_opt_in = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "persona_botuserstate"
        indexes = [
            models.Index(fields=["bot", "user"]),
            models.Index(fields=["user", "last_user_message_at"]),
            models.Index(fields=["bot", "last_initiation_at"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["bot", "user"], name="persona_unique_bot_user_state"),
        ]

    def __str__(self) -> str:
        return f"BotUserState(bot={self.bot_id}, user={self.user_id})"


class MemoryFragment(TimeStampedModel):
    """
    Small, vague memory hints. This is NOT a transcript.
    Designed for "good but imperfect memory".
    """

    class Kind(models.TextChoices):
        TOPIC = "TOPIC", "Topic"
        PREFERENCE = "PREFERENCE", "Preference"
        FACT_SOFT = "FACT_SOFT", "Soft fact"  # e.g., "works a lot lately" (no specifics)
        RELATIONSHIP = "RELATIONSHIP", "Relationship cue"  # e.g., "user dislikes many questions"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    state = models.ForeignKey(
        BotUserState,
        on_delete=models.CASCADE,
        related_name="memory_fragments",
        db_index=True,
    )

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.TOPIC, db_index=True)

    # A compact topic key for querying/decay
    topic = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # The actual hint to inject (keep short; never quote)
    hint_text = models.CharField(max_length=220, help_text="Vague hint text; no quotes; no exact details.")

    # Confidence & decay
    confidence = models.FloatField(default=0.55, validators=[MinValueValidator(0.0), MaxValueValidator(1.0)], db_index=True)
    decay_rate = models.FloatField(
        default=0.015,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Daily decay rate (rough).",
    )

    # Recency
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    times_reinforced = models.PositiveIntegerField(default=1)

    # Whether it is eligible for injection
    is_active = models.BooleanField(default=True, db_index=True)

    # Optional: why it exists (link to message id, but as string to avoid import loops)
    source_ref = models.CharField(max_length=128, blank=True, default="", db_index=True)

    class Meta:
        db_table = "persona_memoryfragment"
        indexes = [
            models.Index(fields=["state", "is_active", "confidence"]),
            models.Index(fields=["topic", "confidence"]),
            models.Index(fields=["last_seen_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(confidence__gte=0.0) & Q(confidence__lte=1.0), name="persona_mem_conf_0_1"),
        ]

    def __str__(self) -> str:
        return f"MemoryFragment({self.kind}:{self.topic})"


class PromptSnippet(TimeStampedModel):
    """
    Optional: store reusable prompt snippets (contracts, persona addons) in DB for admin tweaking.
    Not required for MVP but useful if you want non-code prompt edits.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    key = models.SlugField(max_length=64, unique=True, db_index=True)  # e.g., "contract_v1_fa"
    title = models.CharField(max_length=96, blank=True, default="")
    text = models.TextField()

    is_active = models.BooleanField(default=True, db_index=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "persona_promptsnippet"
        indexes = [
            models.Index(fields=["is_active", "key"]),
            models.Index(fields=["version"]),
        ]

    def __str__(self) -> str:
        return f"PromptSnippet({self.key}@v{self.version})"

