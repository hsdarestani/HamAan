# chat/models.py
from __future__ import annotations

import uuid
from decimal import Decimal

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


class Conversation(TimeStampedModel):
    """
    One user <-> one bot conversation thread.
    For MVP: keep it simple: one active conversation per (user, bot).
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
        db_index=True,
    )

    # Bot is defined in persona app; keep FK as string to avoid import loops
    bot = models.ForeignKey(
        "persona.Bot",
        on_delete=models.PROTECT,
        related_name="conversations",
        db_index=True,
    )

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True)

    # Activity for initiation / retention / trimming context
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_user_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_bot_reply_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Lightweight state flags
    has_unread_bot_message = models.BooleanField(default=False, db_index=True)

    # Optional: last few topic tags (MVP)
    topic_hint = models.CharField(max_length=64, blank=True, default="", db_index=True)

    class Meta:
        db_table = "chat_conversation"
        indexes = [
            models.Index(fields=["user", "bot", "status"]),
            models.Index(fields=["status", "last_activity_at"]),
            models.Index(fields=["user", "last_activity_at"]),
        ]
        constraints = [
            # Ensure at most 1 ACTIVE conversation per (user, bot)
            models.UniqueConstraint(
                fields=["user", "bot"],
                condition=Q(status="ACTIVE"),
                name="chat_unique_active_conversation_per_user_bot",
            )
        ]

    def __str__(self) -> str:
        return f"Conversation({self.id})"


class Message(TimeStampedModel):
    """
    Stores all turns (user/bot/system).
    Keep text only for MVP.

    Token counts are stored for observability & pricing analytics.
    """

    class Role(models.TextChoices):
        USER = "USER", "User"
        BOT = "BOT", "Bot"
        SYSTEM = "SYSTEM", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
        db_index=True,
    )

    role = models.CharField(max_length=8, choices=Role.choices, db_index=True)

    text = models.TextField(blank=True, default="")

    # Optional: Telegram metadata
    telegram_update_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    # Token usage (best-effort; filled by LLM client)
    token_in = models.PositiveIntegerField(default=0)
    token_out = models.PositiveIntegerField(default=0)

    # Safety / moderation (lightweight)
    is_flagged = models.BooleanField(default=False, db_index=True)
    flag_type = models.CharField(max_length=32, blank=True, default="", db_index=True)

    # Ordering
    seq = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        db_table = "chat_message"
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["conversation", "seq"]),
            models.Index(fields=["role", "created_at"]),
            models.Index(fields=["telegram_update_id"]),
        ]
        constraints = [
            # Ensure seq is unique per conversation
            models.UniqueConstraint(fields=["conversation", "seq"], name="chat_unique_message_seq_per_conversation"),
            # If telegram identifiers exist, enforce uniqueness per conversation to avoid duplicates from retries
            models.UniqueConstraint(
                fields=["conversation", "telegram_message_id"],
                condition=~Q(telegram_message_id=None),
                name="chat_unique_telegram_message_id_per_conversation",
            ),
        ]

    def __str__(self) -> str:
        return f"Message({self.role}, conv={self.conversation_id}, seq={self.seq})"


class LLMCallLog(TimeStampedModel):
    """
    Observability for each model call.
    One user message can trigger 0..N model calls (retries, routing, etc.).
    """

    class Provider(models.TextChoices):
        OPENAI = "OPENAI", "OpenAI"
        GEMINI = "GEMINI", "Gemini"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        OK = "OK", "OK"
        ERROR = "ERROR", "Error"
        TIMEOUT = "TIMEOUT", "Timeout"
        RATE_LIMIT = "RATE_LIMIT", "Rate limit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="llm_calls",
        db_index=True,
    )

    # Link to the user message that triggered this call (optional but useful)
    trigger_message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_llm_calls",
        db_index=True,
        limit_choices_to={"role": "USER"},
    )

    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.OPENAI, db_index=True)
    model = models.CharField(max_length=64, db_index=True)  # e.g. gpt-4o-mini

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OK, db_index=True)
    error_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    error_message = models.CharField(max_length=255, blank=True, default="")

    # Latency + retries
    latency_ms = models.PositiveIntegerField(default=0)
    attempt = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)  # provider request id

    # Token usage
    token_in = models.PositiveIntegerField(default=0)
    token_out = models.PositiveIntegerField(default=0)

    # Cost estimate (USD), stored as decimal for reporting (best-effort)
    cost_usd = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        default=Decimal("0.000000"),
        validators=[MinValueValidator(Decimal("0.0"))],
    )

    # Prompt construction debug (small; do NOT store secrets)
    prompt_meta = models.JSONField(default=dict, blank=True)  # e.g., {"context_turns": 8, "memory_fragments": 2}

    class Meta:
        db_table = "chat_llmcalllog"
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["provider", "model", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["request_id"]),
        ]

    def __str__(self) -> str:
        return f"LLMCall({self.provider}:{self.model} {self.status})"


def next_message_seq(conversation_id: uuid.UUID) -> int:
    """
    Optional helper for assigning seq in service layer.
    Keep DB logic out of models, but this is safe utility for MVP.
    """
    last = (
        Message.objects.filter(conversation_id=conversation_id)
        .order_by("-seq")
        .values_list("seq", flat=True)
        .first()
    )
    return (last or 0) + 1

