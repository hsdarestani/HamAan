import json
from uuid import UUID

from django.http import HttpResponseBadRequest, JsonResponse
from django.db.models import F
from django.utils import timezone
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
