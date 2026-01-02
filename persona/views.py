import json
from uuid import UUID

from django.http import HttpResponseBadRequest, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from users.models import User
from .models import Bot, BotUserState, MemoryFragment


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
    bot_id = data.get("bot_id") or query_params.get("bot_id")
    if bot_id:
        try:
            return Bot.objects.get(id=UUID(str(bot_id)))
        except (Bot.DoesNotExist, ValueError, TypeError):
            return None
    if code:
        try:
            return Bot.objects.get(code=code)
        except Bot.DoesNotExist:
            return None
    return None


@require_http_methods(["GET"])
def BotListView(request):
    bots = Bot.objects.filter(is_active=True).order_by("display_name")
    return JsonResponse(
        {
            "ok": True,
            "bots": [
                {
                    "id": str(bot.id),
                    "code": bot.code,
                    "display_name": bot.display_name,
                    "default_language": bot.default_language,
                }
                for bot in bots
            ],
        }
    )


@require_http_methods(["GET"])
def BotProfileView(request):
    bot = _find_bot({}, request.GET)
    if not bot:
        return JsonResponse({"ok": False, "error": "bot_not_found"}, status=404)
    payload = {
        "id": str(bot.id),
        "code": bot.code,
        "display_name": bot.display_name,
        "is_active": bot.is_active,
        "base_prompt_id": bot.base_prompt_id,
        "default_language": bot.default_language,
        "avatar_key": bot.avatar_key,
    }
    return JsonResponse({"ok": True, "bot": payload})


@csrf_exempt
@require_http_methods(["POST"])
def BotSelectView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    bot = _find_bot(data, request.GET)
    if not user or not bot:
        return JsonResponse({"ok": False, "error": "user_or_bot_not_found"}, status=404)
    state, _ = BotUserState.objects.get_or_create(user=user, bot=bot)
    return JsonResponse({"ok": True, "state_id": str(state.id), "bot_id": str(bot.id)})


@require_http_methods(["GET"])
def BotUserStateView(request):
    user = _find_user({}, request.GET)
    bot = _find_bot({}, request.GET)
    if not user or not bot:
        return JsonResponse({"ok": False, "error": "user_or_bot_not_found"}, status=404)
    try:
        state = BotUserState.objects.get(user=user, bot=bot)
    except BotUserState.DoesNotExist:
        return JsonResponse({"ok": False, "error": "state_not_found"}, status=404)
    payload = {
        "id": str(state.id),
        "bot_id": str(bot.id),
        "user_id": str(user.id),
        "familiarity": state.familiarity,
        "trust": state.trust,
        "emotional_closeness": state.emotional_closeness,
        "last_user_message_at": state.last_user_message_at.isoformat() if state.last_user_message_at else None,
        "last_bot_reply_at": state.last_bot_reply_at.isoformat() if state.last_bot_reply_at else None,
        "initiation_opt_in": state.initiation_opt_in,
        "style_rules": state.style_rules,
    }
    return JsonResponse({"ok": True, "state": payload})


def _get_state_from_request(data, query_params):
    state_id = data.get("state_id") or query_params.get("state_id")
    if state_id:
        try:
            return BotUserState.objects.get(id=UUID(str(state_id)))
        except (BotUserState.DoesNotExist, ValueError, TypeError):
            return None

    user = _find_user(data, query_params)
    bot = _find_bot(data, query_params)
    if user and bot:
        try:
            return BotUserState.objects.get(user=user, bot=bot)
        except BotUserState.DoesNotExist:
            return None
    return None


@require_http_methods(["GET"])
def MemoryFragmentsListView(request):
    state = _get_state_from_request({}, request.GET)
    if not state:
        return JsonResponse({"ok": False, "error": "state_not_found"}, status=404)
    fragments = state.memory_fragments.filter(is_active=True).order_by("-confidence", "-last_seen_at")
    return JsonResponse(
        {
            "ok": True,
            "fragments": [
                {
                    "id": str(mf.id),
                    "kind": mf.kind,
                    "topic": mf.topic,
                    "hint_text": mf.hint_text,
                    "confidence": mf.confidence,
                    "last_seen_at": mf.last_seen_at.isoformat(),
                    "times_reinforced": mf.times_reinforced,
                }
                for mf in fragments
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def MemoryFragmentUpsertView(request):
    data = _load_json(request)
    state = _get_state_from_request(data, request.GET)
    if not state:
        return JsonResponse({"ok": False, "error": "state_not_found"}, status=404)

    fragment_id = data.get("fragment_id")
    last_seen = None
    if data.get("last_seen_at"):
        last_seen = parse_datetime(str(data.get("last_seen_at")))

    attrs = {
        "kind": data.get("kind") or MemoryFragment.Kind.TOPIC,
        "topic": data.get("topic", "") or "",
        "hint_text": data.get("hint_text", "")[:220],
        "confidence": float(data.get("confidence", 0.55)),
        "times_reinforced": int(data.get("times_reinforced", 1)),
        "is_active": data.get("is_active", True),
    }
    if last_seen:
        attrs["last_seen_at"] = last_seen

    if fragment_id:
        try:
            fragment = MemoryFragment.objects.get(id=UUID(str(fragment_id)), state=state)
        except (MemoryFragment.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "fragment_not_found"}, status=404)
        for key, value in attrs.items():
            if value is not None:
                setattr(fragment, key, value)
        fragment.save()
        created = False
    else:
        fragment = MemoryFragment.objects.create(state=state, **attrs)
        created = True

    return JsonResponse({"ok": True, "created": created, "fragment_id": str(fragment.id)})


@csrf_exempt
@require_http_methods(["POST"])
def MemoryFragmentDeactivateView(request):
    data = _load_json(request)
    fragment_id = data.get("fragment_id")
    if not fragment_id:
        return HttpResponseBadRequest("fragment_id is required")
    try:
        fragment = MemoryFragment.objects.get(id=UUID(str(fragment_id)))
    except (MemoryFragment.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "fragment_not_found"}, status=404)
    fragment.is_active = False
    fragment.save(update_fields=["is_active"])
    return JsonResponse({"ok": True, "deactivated": True})
