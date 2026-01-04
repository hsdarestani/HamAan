from django.contrib import admin

from .models import User, UserPrefs


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "telegram_id",
        "telegram_username",
        "first_name",
        "last_name",
        "is_active",
        "is_staff",
        "is_blocked",
        "last_seen_at",
        "last_message_at",
    )
    list_filter = (
        "is_active",
        "is_staff",
        "is_blocked",
        "language_code",
        "timezone",
        "marketing_opt_in",
        "initiation_opt_in",
    )
    search_fields = ("telegram_id", "telegram_username", "first_name", "last_name")
    readonly_fields = ("created_at", "updated_at", "first_seen_at", "last_seen_at", "last_message_at")
    ordering = ("-created_at",)


@admin.register(UserPrefs)
class UserPrefsAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "reply_length",
        "question_tolerance",
        "tone",
        "prefers_initiation",
        "quiet_hours_enabled",
        "last_profile_refresh_at",
    )
    list_filter = (
        "reply_length",
        "question_tolerance",
        "tone",
        "prefers_initiation",
        "quiet_hours_enabled",
    )
    search_fields = ("user__telegram_id", "user__telegram_username")
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at", "last_profile_refresh_at")
