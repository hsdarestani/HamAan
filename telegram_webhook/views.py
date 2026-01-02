from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
import os

@csrf_exempt
def webhook(request):
    # Optional: verify secret header from Cloudflare Worker or Telegram
    secret = os.getenv("TELEGRAM_SECRET_TOKEN", "")
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if secret and got != secret:
        return HttpResponseForbidden("forbidden")

    # For now: just ACK
    return JsonResponse({"ok": True})

