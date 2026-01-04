from django.contrib import admin

from .models import BlockedPhrase, SafetyEvent, UserRestriction


@admin.register(UserRestriction)
class UserRestrictionAdmin(admin.ModelAdmin):
    list_display = ("user", "level", "expires_at", "block_initiation", "block_media", "block_purchases", "created_at")
    list_filter = ("level", "block_initiation", "block_media", "block_purchases")
    search_fields = ("user__telegram_id", "user__telegram_username")
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(SafetyEvent)
class SafetyEventAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "event_type",
        "severity",
        "rule_key",
        "action_taken",
        "conversation_id",
        "message_id",
        "created_at",
    )
    list_filter = ("event_type", "severity", "action_taken")
    search_fields = (
        "user__telegram_id",
        "user__telegram_username",
        "rule_key",
        "conversation_id",
        "message_id",
        "summary",
    )
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


@admin.register(BlockedPhrase)
class BlockedPhraseAdmin(admin.ModelAdmin):
    list_display = ("phrase", "event_type", "severity", "is_active", "created_at")
    list_filter = ("is_active", "event_type", "severity")
    search_fields = ("phrase",)
    readonly_fields = ("created_at", "updated_at")
