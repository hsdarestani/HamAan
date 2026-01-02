import json

from django.http import HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from persona.models import Bot, BotUserState
from users.models import User
from .models import InitiationEvent, InitiationRule, ScheduledJob


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
    bot_code = data.get("bot_code") or query_params.get("bot_code")
    if not bot_code:
        return None
    try:
        return Bot.objects.get(code=bot_code)
    except Bot.DoesNotExist:
        return None


@csrf_exempt
def InitiationRuleView(request):
    bot = _find_bot(_load_json(request) if request.method == "POST" else {}, request.GET)
    if not bot:
        return JsonResponse({"ok": False, "error": "bot_not_found"}, status=404)

    if request.method == "GET":
        rule, _ = InitiationRule.objects.get_or_create(bot=bot)
        payload = {
            "id": str(rule.id),
            "bot_id": str(bot.id),
            "enabled": rule.enabled,
            "cooldown_hours": rule.cooldown_hours,
            "max_per_day": rule.max_per_day,
            "max_per_week": rule.max_per_week,
            "min_familiarity": rule.min_familiarity,
            "min_trust": rule.min_trust,
            "templates": rule.templates,
        }
        return JsonResponse({"ok": True, "rule": payload})

    if request.method == "POST":
        data = _load_json(request)
        rule, _ = InitiationRule.objects.get_or_create(bot=bot)
        allowed = {
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
        for key, value in data.items():
            if key in allowed:
                setattr(rule, key, value)
        rule.save()
        return JsonResponse({"ok": True, "updated": True, "rule_id": str(rule.id)})

    return HttpResponseBadRequest("unsupported_method")


@require_http_methods(["GET"])
def InitiationStatusView(request):
    user = _find_user({}, request.GET)
    bot = _find_bot({}, request.GET)
    if not user or not bot:
        return JsonResponse({"ok": False, "error": "user_or_bot_not_found"}, status=404)
    event = (
        InitiationEvent.objects.filter(user=user, bot=bot)
        .order_by("-created_at")
        .values("status", "scheduled_for", "sent_at")
        .first()
    )
    return JsonResponse({"ok": True, "latest": event})


@csrf_exempt
@require_http_methods(["POST"])
def InitiationTriggerView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    bot = _find_bot(data, request.GET)
    if not user or not bot:
        return JsonResponse({"ok": False, "error": "user_or_bot_not_found"}, status=404)
    state, _ = BotUserState.objects.get_or_create(user=user, bot=bot)
    event = InitiationEvent.objects.create(
        state=state,
        bot=bot,
        user=user,
        trigger=InitiationEvent.Trigger.MANUAL,
        status=InitiationEvent.Status.PLANNED,
        scheduled_for=timezone.now(),
        message_text=data.get("message_text", ""),
        idempotency_key=data.get("idempotency_key", ""),
    )
    return JsonResponse({"ok": True, "event_id": str(event.id)})


@require_http_methods(["GET"])
def InitiationEventListView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    events = InitiationEvent.objects.filter(user=user).order_by("-created_at")[:200]
    return JsonResponse(
        {
            "ok": True,
            "events": [
                {
                    "id": str(evt.id),
                    "bot_id": str(evt.bot_id),
                    "status": evt.status,
                    "trigger": evt.trigger,
                    "scheduled_for": evt.scheduled_for.isoformat() if evt.scheduled_for else None,
                    "sent_at": evt.sent_at.isoformat() if evt.sent_at else None,
                    "message_text": evt.message_text,
                }
                for evt in events
            ],
        }
    )


@require_http_methods(["GET"])
def ScheduledJobListView(request):
    job_type = request.GET.get("job_type")
    jobs = ScheduledJob.objects.all().order_by("-created_at")[:200]
    if job_type:
        jobs = jobs.filter(job_type=job_type)
    return JsonResponse(
        {
            "ok": True,
            "jobs": [
                {
                    "id": str(job.id),
                    "job_type": job.job_type,
                    "status": job.status,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                }
                for job in jobs
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def ScheduledJobRunView(request):
    data = _load_json(request)
    job_type = data.get("job_type") or ScheduledJob.JobType.HOUSEKEEPING
    job = ScheduledJob.objects.create(
        job_type=job_type,
        status=data.get("status") or ScheduledJob.Status.RUNNING,
        started_at=timezone.now(),
        meta=data.get("meta", {}),
    )
    return JsonResponse({"ok": True, "job_id": str(job.id)})
