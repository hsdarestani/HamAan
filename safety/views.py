import json
from uuid import UUID

from django.http import HttpResponseBadRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from users.models import User
from .models import BlockedPhrase, SafetyEvent, UserRestriction


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


@csrf_exempt
@require_http_methods(["POST"])
def SafetyEventReportView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    event = SafetyEvent.objects.create(
        user=user,
        conversation_id=data.get("conversation_id") or None,
        message_id=data.get("message_id") or None,
        event_type=data.get("event_type") or SafetyEvent.EventType.OTHER,
        severity=data.get("severity") or SafetyEvent.Severity.LOW,
        rule_key=data.get("rule_key", ""),
        summary=data.get("summary", ""),
        payload=data.get("payload", {}),
        action_taken=bool(data.get("action_taken", False)),
        action_note=data.get("action_note", ""),
    )
    return JsonResponse({"ok": True, "event_id": str(event.id)})


@require_http_methods(["GET"])
def SafetyEventListView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    events = SafetyEvent.objects.filter(user=user).order_by("-created_at")[:200]
    return JsonResponse(
        {
            "ok": True,
            "events": [
                {
                    "id": str(evt.id),
                    "event_type": evt.event_type,
                    "severity": evt.severity,
                    "summary": evt.summary,
                    "created_at": evt.created_at.isoformat(),
                }
                for evt in events
            ],
        }
    )


@csrf_exempt
def UserRestrictionView(request):
    data = _load_json(request) if request.method == "POST" else {}
    user = _find_user(data, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    restriction, _ = UserRestriction.objects.get_or_create(user=user)

    if request.method == "GET":
        payload = {
            "level": restriction.level,
            "reason": restriction.reason,
            "expires_at": restriction.expires_at.isoformat() if restriction.expires_at else None,
            "block_initiation": restriction.block_initiation,
            "block_media": restriction.block_media,
            "block_purchases": restriction.block_purchases,
        }
        return JsonResponse({"ok": True, "restriction": payload})

    if request.method == "POST":
        allowed = {
            "level",
            "reason",
            "expires_at",
            "block_initiation",
            "block_media",
            "block_purchases",
            "max_msgs_per_minute",
            "max_msgs_per_day",
        }
        for key, value in data.items():
            if key in allowed:
                setattr(restriction, key, value)
        restriction.save()
        return JsonResponse({"ok": True, "updated": True})

    return HttpResponseBadRequest("unsupported_method")


@require_http_methods(["GET"])
def BlockedPhraseListView(request):
    phrases = BlockedPhrase.objects.filter(is_active=True).order_by("phrase")
    return JsonResponse(
        {
            "ok": True,
            "phrases": [
                {"id": str(bp.id), "phrase": bp.phrase, "event_type": bp.event_type, "severity": bp.severity}
                for bp in phrases
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def BlockedPhraseUpsertView(request):
    data = _load_json(request)
    phrase = data.get("phrase")
    if not phrase:
        return HttpResponseBadRequest("phrase is required")
    phrase_id = data.get("id")
    defaults = {
        "event_type": data.get("event_type", SafetyEvent.EventType.OTHER),
        "severity": data.get("severity", SafetyEvent.Severity.LOW),
        "is_active": data.get("is_active", True),
        "note": data.get("note", ""),
    }
    if phrase_id:
        try:
            bp = BlockedPhrase.objects.get(id=UUID(str(phrase_id)))
            for key, value in defaults.items():
                setattr(bp, key, value)
            bp.phrase = phrase
            bp.save()
            created = False
        except (BlockedPhrase.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "phrase_not_found"}, status=404)
    else:
        bp, created = BlockedPhrase.objects.update_or_create(phrase=phrase, defaults=defaults)
    return JsonResponse({"ok": True, "created": created, "id": str(bp.id)})
