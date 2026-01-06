import json
from uuid import UUID

from django.http import HttpResponseBadRequest, JsonResponse
from django.db import IntegrityError
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from automation.models import InitiationEvent, InitiationRule
from persona.models import Bot, BotIdentity, BotUserState, MemoryFragment
from persona.relationship import update_relationship_memory
from safety.models import BlockedPhrase, SafetyEvent, UserRestriction
from users.models import User, UserPrefs
from .models import Conversation, LLMCallLog, Message, next_message_seq


def _load_json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _find_user(data, query_params):
    telegram_id = data.get("telegram_id") or query_params.get("telegram_id")
    if not telegram_id:
        return None
    try:
        return User.objects.get(telegram_id=int(telegram_id))
    except User.DoesNotExist:
        return None


def _find_bot(data, query_params):
    code = data.get("bot_code") or query_params.get("bot_code")
    if not code:
        return None
    try:
        return Bot.objects.get(code=code)
    except Bot.DoesNotExist:
        return None


def _get_conversation_from_request(data, query_params):
    conv_id = data.get("conversation_id") or query_params.get("conversation_id")
    if conv_id:
        try:
            return Conversation.objects.get(id=UUID(str(conv_id)))
        except (Conversation.DoesNotExist, ValueError, TypeError):
            return None
    user = _find_user(data, query_params)
    bot = _find_bot(data, query_params)
    if user and bot:
        return Conversation.objects.filter(user=user, bot=bot, status=Conversation.Status.ACTIVE).first()
    return None


def _ensure_user_context(user, bot):
    """
    Ensure all per-user/per-bot companion models exist for downstream updates.
    """
    prefs, _ = UserPrefs.objects.get_or_create(user=user)
    UserRestriction.objects.get_or_create(user=user)
    state, _ = BotUserState.objects.get_or_create(user=user, bot=bot)
    InitiationRule.objects.get_or_create(bot=bot)
    BotIdentity.objects.get_or_create(bot=bot)
    return state, prefs


def _touch_user_prefs_counts(user_id, role, now):
    updates = {"updated_at": now}
    if role == Message.Role.USER:
        updates["total_user_messages"] = F("total_user_messages") + 1
    elif role == Message.Role.BOT:
        updates["total_bot_replies"] = F("total_bot_replies") + 1
    UserPrefs.objects.filter(user_id=user_id).update(**updates)


def _ack_initiation_if_needed(state_id, now):
    latest = (
        InitiationEvent.objects.filter(state_id=state_id, status=InitiationEvent.Status.SENT)
        .order_by("-scheduled_for", "-created_at")
        .first()
    )
    if latest and not latest.user_replied:
        InitiationEvent.objects.filter(id=latest.id).update(
            user_replied=True, user_replied_at=now, status=InitiationEvent.Status.ACKED, updated_at=now
        )


def _maybe_log_blocked_phrase(user_id, conversation_id, message_id, text):
    text_lower = (text or "").lower()
    if not text_lower:
        return
    for phrase in BlockedPhrase.objects.filter(is_active=True):
        if phrase.phrase.lower() in text_lower:
            SafetyEvent.objects.create(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                event_type=phrase.event_type,
                severity=phrase.severity,
                rule_key="blocked_phrase",
                summary=f"Matched phrase: {phrase.phrase}",
                payload={"matched_phrase": phrase.phrase},
            )
            break


def _upsert_memory_fragment(state, text, now, source_ref):
    topic = (state.bot.default_language or "general")[:64]
    text_snippet = (text or "").strip()[:220]
    if not text_snippet:
        text_snippet = "interaction"
    fragment, created = MemoryFragment.objects.get_or_create(
        state=state,
        topic=topic,
        defaults={
            "kind": MemoryFragment.Kind.TOPIC,
            "hint_text": text_snippet,
            "confidence": 0.55,
            "source_ref": str(source_ref),
            "last_seen_at": now,
        },
    )
    if not created:
        MemoryFragment.objects.filter(id=fragment.id).update(
            hint_text=text_snippet,
            last_seen_at=now,
            times_reinforced=F("times_reinforced") + 1,
            source_ref=str(source_ref),
        )


def _recent_user_texts(conversation: Conversation, limit: int = 20) -> list[str]:
    user_messages = (
        conversation.messages.filter(role=Message.Role.USER)
        .order_by("-seq")
        .values_list("text", flat=True)[:limit]
    )
    return [text for text in user_messages if text]


@csrf_exempt
@require_http_methods(["POST"])
def ConversationCreateOrGetView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    bot = _find_bot(data, request.GET)
    if not user or not bot:
        return JsonResponse({"ok": False, "error": "user_or_bot_not_found"}, status=404)

    conversation, created = Conversation.objects.get_or_create(
        user=user,
        bot=bot,
        status=Conversation.Status.ACTIVE,
        defaults={"last_activity_at": timezone.now()},
    )
    _ensure_user_context(user, bot)
    User.objects.filter(id=user.id).update(last_seen_at=timezone.now(), updated_at=timezone.now())
    if not created:
        Conversation.objects.filter(id=conversation.id).update(last_activity_at=timezone.now())
    return JsonResponse({"ok": True, "conversation_id": str(conversation.id), "created": created})


@require_http_methods(["GET"])
def ConversationListView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    conversations = Conversation.objects.filter(user=user).order_by("-last_activity_at")
    return JsonResponse(
        {
            "ok": True,
            "conversations": [
                {
                    "id": str(c.id),
                    "bot_id": str(c.bot_id),
                    "status": c.status,
                    "last_activity_at": c.last_activity_at.isoformat(),
                    "has_unread_bot_message": c.has_unread_bot_message,
                    "topic_hint": c.topic_hint,
                }
                for c in conversations
            ],
        }
    )


@require_http_methods(["GET"])
def ConversationDetailView(request):
    conversation = _get_conversation_from_request({}, request.GET)
    if not conversation:
        return JsonResponse({"ok": False, "error": "conversation_not_found"}, status=404)
    payload = {
        "id": str(conversation.id),
        "user_id": str(conversation.user_id),
        "bot_id": str(conversation.bot_id),
        "status": conversation.status,
        "last_activity_at": conversation.last_activity_at.isoformat(),
        "last_user_message_at": conversation.last_user_message_at.isoformat() if conversation.last_user_message_at else None,
        "last_bot_reply_at": conversation.last_bot_reply_at.isoformat() if conversation.last_bot_reply_at else None,
        "has_unread_bot_message": conversation.has_unread_bot_message,
    }
    return JsonResponse({"ok": True, "conversation": payload})


@require_http_methods(["GET"])
def MessageListView(request):
    conversation = _get_conversation_from_request({}, request.GET)
    if not conversation:
        return JsonResponse({"ok": False, "error": "conversation_not_found"}, status=404)
    messages = conversation.messages.order_by("seq", "created_at")
    return JsonResponse(
        {
            "ok": True,
            "messages": [
                {
                    "id": str(m.id),
                    "role": m.role,
                    "text": m.text,
                    "seq": m.seq,
                    "created_at": m.created_at.isoformat(),
                    "telegram_message_id": m.telegram_message_id,
                }
                for m in messages
            ],
        }
    )


def _create_message(conversation, role, text, telegram_ids=None):
    now = timezone.now()

    # Ensure persona/user state exists and update per-role counters/timestamps
    state, _ = _ensure_user_context(conversation.user, conversation.bot)
    if role == Message.Role.USER:
        BotUserState.objects.filter(id=state.id).update(
            last_user_message_at=now, total_user_messages=F("total_user_messages") + 1, updated_at=now
        )
        _ack_initiation_if_needed(state.id, now)
    elif role == Message.Role.BOT:
        BotUserState.objects.filter(id=state.id).update(
            last_bot_reply_at=now, total_bot_replies=F("total_bot_replies") + 1, updated_at=now
        )
    _touch_user_prefs_counts(conversation.user_id, role, now)

    # Track user activity
    user_updates = {"last_seen_at": now, "updated_at": now}
    if role == Message.Role.USER:
        user_updates["last_message_at"] = now
    User.objects.filter(id=conversation.user_id).update(**user_updates)

    seq = next_message_seq(conversation.id)
    message = Message.objects.create(
        conversation=conversation,
        role=role,
        text=text or "",
        seq=seq,
        telegram_message_id=(telegram_ids or {}).get("telegram_message_id"),
        telegram_update_id=(telegram_ids or {}).get("telegram_update_id"),
    )

    if role == Message.Role.USER:
        _maybe_log_blocked_phrase(conversation.user_id, conversation.id, message.id, text)

    # Seed or reinforce lightweight memory fragments from the interaction
    _upsert_memory_fragment(state, text, now, source_ref=message.id)
    update_relationship_memory(state, _recent_user_texts(conversation), latest_text=text, now=now)

    updates = {"last_activity_at": now, "updated_at": now}
    if role == Message.Role.USER:
        updates["last_user_message_at"] = now
        updates["has_unread_bot_message"] = False
    elif role == Message.Role.BOT:
        updates["last_bot_reply_at"] = now
        updates["has_unread_bot_message"] = True
    Conversation.objects.filter(id=conversation.id).update(**updates)
    return message


@csrf_exempt
@require_http_methods(["POST"])
def MessageCreateUserView(request):
    data = _load_json(request)
    conversation = _get_conversation_from_request(data, request.GET)
    if not conversation:
        return JsonResponse({"ok": False, "error": "conversation_not_found"}, status=404)
    text = data.get("text", "")
    message = _create_message(conversation, Message.Role.USER, text, telegram_ids=data)
    return JsonResponse({"ok": True, "message_id": str(message.id), "seq": message.seq})


@csrf_exempt
@require_http_methods(["POST"])
def MessageCreateBotView(request):
    data = _load_json(request)
    conversation = _get_conversation_from_request(data, request.GET)
    if not conversation:
        return JsonResponse({"ok": False, "error": "conversation_not_found"}, status=404)
    text = data.get("text", "")
    message = _create_message(conversation, Message.Role.BOT, text, telegram_ids=data)
    return JsonResponse({"ok": True, "message_id": str(message.id), "seq": message.seq})


@require_http_methods(["GET"])
def LLMCallLogListView(request):
    conversation = _get_conversation_from_request({}, request.GET)
    if not conversation:
        return JsonResponse({"ok": False, "error": "conversation_not_found"}, status=404)
    logs = conversation.llm_calls.order_by("-created_at")[:100]
    return JsonResponse(
        {
            "ok": True,
            "logs": [
                {
                    "id": str(log.id),
                    "provider": log.provider,
                    "model": log.model,
                    "status": log.status,
                    "latency_ms": log.latency_ms,
                    "token_in": log.token_in,
                    "token_out": log.token_out,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def ConversationUpdateView(request):
    """
    Allow the bot runtime to update its own configuration based on a conversation.

    This endpoint intentionally accepts multiple update targets in one call so an agent
    can react to conversation messages by:
      - adjusting per-user preferences (UserPrefs)
      - nudging the bot's identity (BotIdentity)
      - updating the per-user state (BotUserState)
      - tweaking initiation rules and creating initiation events
      - appending bot/user messages
      - upserting memory fragments
    """

    data = _load_json(request)
    conversation = _get_conversation_from_request(data, request.GET)
    if not conversation:
        return JsonResponse({"ok": False, "error": "conversation_not_found"}, status=404)

    state, prefs = _ensure_user_context(conversation.user, conversation.bot)
    now = timezone.now()
    results: dict[str, object] = {"conversation_id": str(conversation.id)}

    # User preferences
    prefs_payload = data.get("user_prefs")
    if isinstance(prefs_payload, dict):
        allowed_fields = {
            "reply_length",
            "question_tolerance",
            "tone",
            "emotional_distance",
            "verbosity",
            "prefers_initiation",
            "initiation_cooldown_hours",
            "quiet_hours_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "style_rules",
        }
        updates = {k: v for k, v in prefs_payload.items() if k in allowed_fields}
        if updates:
            for key, value in updates.items():
                setattr(prefs, key, value)
            prefs.save(update_fields=[*updates.keys(), "updated_at"])
            results["user_prefs_updated"] = sorted(updates.keys())

    # Bot identity
    identity_payload = data.get("bot_identity")
    if isinstance(identity_payload, dict):
        identity, _ = BotIdentity.objects.get_or_create(bot=conversation.bot)
        allowed_fields = {
            "core_tone",
            "background_seed",
            "self_confidence",
            "openness",
            "talkativeness",
            "emotional_clarity",
            "memory_strength",
            "memory_noise",
            "identity_profile",
            "avoids_advice",
            "avoids_therapy_tone",
            "avoids_omniscience",
        }
        updates = {k: v for k, v in identity_payload.items() if k in allowed_fields}
        if updates:
            BotIdentity.objects.filter(bot=conversation.bot).update(**updates, updated_at=now)
            results["bot_identity_updated"] = sorted(updates.keys())

    # Bot (global) fields
    bot_payload = data.get("bot")
    if isinstance(bot_payload, dict):
        allowed_fields = {
            "display_name",
            "base_prompt_id",
            "base_prompt_text",
            "default_language",
            "avatar_key",
            "max_output_chars",
            "max_questions_per_reply",
            "is_active",
        }
        updates = {k: v for k, v in bot_payload.items() if k in allowed_fields}
        if updates:
            Bot.objects.filter(id=conversation.bot_id).update(**updates, updated_at=now)
            results["bot_updated"] = sorted(updates.keys())

    # BotUserState
    state_payload = data.get("bot_user_state")
    if isinstance(state_payload, dict):
        allowed_fields = {
            "familiarity",
            "trust",
            "emotional_closeness",
            "user_pref_verbosity",
            "user_pref_questions",
            "shared_silence",
            "conflict_count",
            "style_rules",
            "relationship_memory",
            "initiation_opt_in",
        }
        updates = {k: v for k, v in state_payload.items() if k in allowed_fields}
        if updates:
            BotUserState.objects.filter(id=state.id).update(**updates, updated_at=now)
            results["bot_user_state_updated"] = sorted(updates.keys())

    # Initiation rule tweaks
    initiation_rule_payload = data.get("initiation_rule")
    if isinstance(initiation_rule_payload, dict):
        rule, _ = InitiationRule.objects.get_or_create(bot=conversation.bot)
        allowed_fields = {
            "enabled",
            "cooldown_hours",
            "max_per_day",
            "max_per_week",
            "min_familiarity",
            "min_trust",
            "allowed_start_hour",
            "allowed_end_hour",
            "max_chars",
            "allow_question",
            "templates",
        }
        updates = {k: v for k, v in initiation_rule_payload.items() if k in allowed_fields}
        if updates:
            InitiationRule.objects.filter(id=rule.id).update(**updates, updated_at=now)
            results["initiation_rule_updated"] = sorted(updates.keys())

    # Initiation event creation (scheduled message)
    initiation_event_payload = data.get("initiation_event")
    if isinstance(initiation_event_payload, dict):
        scheduled_for = initiation_event_payload.get("scheduled_for")
        scheduled_dt = parse_datetime(str(scheduled_for)) if scheduled_for else None
        try:
            event = InitiationEvent.objects.create(
                state=state,
                bot=conversation.bot,
                user=conversation.user,
                trigger=initiation_event_payload.get("trigger") or InitiationEvent.Trigger.MANUAL,
                status=initiation_event_payload.get("status") or InitiationEvent.Status.PLANNED,
                scheduled_for=scheduled_dt,
                message_text=initiation_event_payload.get("message_text", "")[:220],
                idempotency_key=initiation_event_payload.get("idempotency_key", "")[:128],
                meta=initiation_event_payload.get("meta", {}),
            )
            results["initiation_event_id"] = str(event.id)
        except IntegrityError:
            existing = InitiationEvent.objects.filter(
                state=state, idempotency_key=initiation_event_payload.get("idempotency_key", "")
            ).first()
            if existing:
                results["initiation_event_id"] = str(existing.id)

    # Message append (user/bot/system)
    created_messages: list[dict[str, object]] = []
    for message_payload in data.get("messages", []) or []:
        role = message_payload.get("role")
        if role not in (Message.Role.USER, Message.Role.BOT, Message.Role.SYSTEM):
            continue
        text = message_payload.get("text", "")
        telegram_ids = {
            "telegram_message_id": message_payload.get("telegram_message_id"),
            "telegram_update_id": message_payload.get("telegram_update_id"),
        }
        message = _create_message(conversation, role, text, telegram_ids=telegram_ids)
        created_messages.append({"id": str(message.id), "role": message.role, "seq": message.seq})
    if created_messages:
        results["messages_created"] = created_messages

    # Memory fragments upsert
    fragment_results: list[dict[str, object]] = []
    for fragment_payload in data.get("memory_fragments", []) or []:
        fragment_id = fragment_payload.get("fragment_id") or fragment_payload.get("id")
        last_seen = None
        if fragment_payload.get("last_seen_at"):
            last_seen = parse_datetime(str(fragment_payload.get("last_seen_at")))

        attrs = {
            "kind": fragment_payload.get("kind") or MemoryFragment.Kind.TOPIC,
            "topic": fragment_payload.get("topic", "") or "",
            "hint_text": (fragment_payload.get("hint_text", "") or "")[:220],
            "confidence": float(fragment_payload.get("confidence", 0.55)),
            "times_reinforced": int(fragment_payload.get("times_reinforced", 1)),
            "is_active": fragment_payload.get("is_active", True),
        }
        if last_seen:
            attrs["last_seen_at"] = last_seen

        if fragment_id:
            try:
                fragment = MemoryFragment.objects.get(id=UUID(str(fragment_id)), state=state)
            except (MemoryFragment.DoesNotExist, ValueError, TypeError):
                continue
            for key, value in attrs.items():
                setattr(fragment, key, value)
            fragment.save()
            fragment_results.append({"id": str(fragment.id), "updated": True})
        else:
            fragment = MemoryFragment.objects.create(state=state, **attrs)
            fragment_results.append({"id": str(fragment.id), "created": True})
    if fragment_results:
        results["memory_fragments"] = fragment_results

    return JsonResponse({"ok": True, "results": results})
