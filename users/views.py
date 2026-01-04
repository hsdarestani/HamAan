import json
from uuid import UUID

from django.http import HttpResponseBadRequest, HttpResponseNotAllowed, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import User, UserPrefs


def _load_json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _find_user(data, query_params):
    user_id = data.get("user_id") or query_params.get("user_id")
    telegram_id = data.get("telegram_id") or query_params.get("telegram_id")
    user = None
    if user_id:
        try:
            user = User.objects.filter(id=UUID(str(user_id))).first()
        except (ValueError, TypeError):
            user = None
    if not user and telegram_id:
        user = User.objects.filter(telegram_id=int(telegram_id)).first()
    return user


@csrf_exempt
@require_http_methods(["POST"])
def UserCreateOrUpdateFromTelegramView(request):
    data = _load_json(request)
    telegram_id = data.get("telegram_id")
    if telegram_id is None:
        return HttpResponseBadRequest("telegram_id is required")

    defaults = {
        "telegram_username": data.get("telegram_username", "") or "",
        "first_name": data.get("first_name", "") or "",
        "last_name": data.get("last_name", "") or "",
        "language_code": data.get("language_code", "") or "",
        "last_seen_at": timezone.now(),
    }
    user, created = User.objects.update_or_create(telegram_id=int(telegram_id), defaults=defaults)
    return JsonResponse({"ok": True, "created": created, "user_id": str(user.id), "telegram_id": user.telegram_id})


@require_http_methods(["GET"])
def UserProfileView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)

    payload = {
        "id": str(user.id),
        "telegram_id": user.telegram_id,
        "telegram_username": user.telegram_username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "language_code": user.language_code,
        "timezone": user.timezone,
        "is_blocked": user.is_blocked,
        "block_reason": user.block_reason,
        "marketing_opt_in": user.marketing_opt_in,
        "initiation_opt_in": user.initiation_opt_in,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
    }
    return JsonResponse({"ok": True, "user": payload})


@csrf_exempt
def UserPrefsView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)

    prefs, _ = UserPrefs.objects.get_or_create(user=user)

    if request.method == "GET":
        payload = {
            "reply_length": prefs.reply_length,
            "question_tolerance": prefs.question_tolerance,
            "tone": prefs.tone,
            "emotional_distance": prefs.emotional_distance,
            "verbosity": prefs.verbosity,
            "prefers_initiation": prefs.prefers_initiation,
            "initiation_cooldown_hours": prefs.initiation_cooldown_hours,
            "quiet_hours_enabled": prefs.quiet_hours_enabled,
            "quiet_hours_start": prefs.quiet_hours_start.isoformat() if prefs.quiet_hours_start else None,
            "quiet_hours_end": prefs.quiet_hours_end.isoformat() if prefs.quiet_hours_end else None,
        }
        return JsonResponse({"ok": True, "prefs": payload})

    if request.method == "POST":
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
        }
        updates = {k: v for k, v in data.items() if k in allowed_fields}
        if updates:
            for key, value in updates.items():
                setattr(prefs, key, value)
            prefs.save()
        return JsonResponse({"ok": True, "updated": bool(updates)})

    return HttpResponseNotAllowed(["GET", "POST"])


@csrf_exempt
@require_http_methods(["POST"])
def UserDeleteDataView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    user.delete()
    return JsonResponse({"ok": True, "deleted": True})
