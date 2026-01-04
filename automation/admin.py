from django.contrib import admin

from .models import InitiationEvent, InitiationRule, ScheduledJob


@admin.register(InitiationRule)
class InitiationRuleAdmin(admin.ModelAdmin):
    list_display = (
        "bot",
        "enabled",
        "cooldown_hours",
        "max_per_day",
        "max_per_week",
        "allowed_start_hour",
        "allowed_end_hour",
    )
    list_filter = ("enabled",)
    search_fields = ("bot__code", "bot__display_name")
    raw_id_fields = ("bot",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(InitiationEvent)
class InitiationEventAdmin(admin.ModelAdmin):
    list_display = (
        "state",
        "bot",
        "user",
        "trigger",
        "status",
        "scheduled_for",
        "sent_at",
        "user_replied",
    )
    list_filter = ("trigger", "status", "user_replied")
    search_fields = ("bot__code", "user__telegram_id", "user__telegram_username", "idempotency_key")
    raw_id_fields = ("state", "bot", "user")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ScheduledJob)
class ScheduledJobAdmin(admin.ModelAdmin):
    list_display = ("job_type", "status", "started_at", "finished_at", "created_at")
    list_filter = ("job_type", "status")
    search_fields = ("job_type",)
    readonly_fields = ("created_at", "updated_at")
