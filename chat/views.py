import json
from uuid import UUID

from django.http import HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from persona.models import Bot
from users.models import User
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
    seq = next_message_seq(conversation.id)
    message = Message.objects.create(
        conversation=conversation,
        role=role,
        text=text or "",
        seq=seq,
        telegram_message_id=(telegram_ids or {}).get("telegram_message_id"),
        telegram_update_id=(telegram_ids or {}).get("telegram_update_id"),
    )
    now = timezone.now()
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
