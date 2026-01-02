import json
import os

from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


def _load_json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


@require_http_methods(["GET"])
def HealthCheckView(request):
    """
    Lightweight health endpoint for uptime checks.
    """
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_http_methods(["POST"])
def TelegramWebhookView(request):
    """
    Basic Telegram webhook receiver.
    Validates optional secret header and returns an acknowledgement.
    """
    secret = os.getenv("TELEGRAM_SECRET_TOKEN", "")
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if secret and got != secret:
        return HttpResponseForbidden("forbidden")

    payload = _load_json(request)
    update_id = payload.get("update_id")

    return JsonResponse({"ok": True, "received": bool(payload), "update_id": update_id})


@csrf_exempt
@require_http_methods(["POST"])
def TelegramSetWebhookView(request):
    """
    Stub endpoint to mimic setting Telegram webhook details.
    Accepts a `url` in JSON body and echoes it back.
    """
    data = _load_json(request)
    url = data.get("url")
    if not url:
        return HttpResponseBadRequest("url is required")
    secret = os.getenv("TELEGRAM_SECRET_TOKEN", "")
    return JsonResponse({"ok": True, "url": url, "secret_token_set": bool(secret)})


@require_http_methods(["GET"])
def TelegramWebhookDiagnosticsView(request):
    """
    Returns basic diagnostics about webhook configuration (non-sensitive).
    """
    return JsonResponse(
        {
            "has_secret": bool(os.getenv("TELEGRAM_SECRET_TOKEN", "")),
            "environment": os.getenv("ENVIRONMENT", "dev"),
        }
    )
