from django.contrib import admin

from .models import Conversation, LLMCallLog, Message


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "bot",
        "status",
        "last_activity_at",
        "last_user_message_at",
        "last_bot_reply_at",
        "has_unread_bot_message",
    )
    list_filter = ("status", "has_unread_bot_message")
    search_fields = ("id", "user__telegram_id", "user__telegram_username", "bot__code", "bot__display_name")
    raw_id_fields = ("user", "bot")
    readonly_fields = ("created_at", "updated_at", "last_activity_at", "last_user_message_at", "last_bot_reply_at")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "role", "seq", "is_flagged", "short_text", "created_at")
    list_filter = ("role", "is_flagged")
    search_fields = ("id", "conversation__id", "text")
    raw_id_fields = ("conversation",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"

    @admin.display(description="Text")
    def short_text(self, obj):
        return (obj.text[:75] + "...") if obj.text and len(obj.text) > 75 else obj.text


@admin.register(LLMCallLog)
class LLMCallLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "conversation",
        "provider",
        "model",
        "status",
        "attempt",
        "request_id",
        "created_at",
    )
    list_filter = ("provider", "status")
    search_fields = ("id", "conversation__id", "request_id", "model")
    raw_id_fields = ("conversation", "trigger_message")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
