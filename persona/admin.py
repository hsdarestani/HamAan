from django.contrib import admin

from .models import Bot, BotIdentity, BotUserState, MemoryFragment, PromptSnippet


@admin.register(Bot)
class BotAdmin(admin.ModelAdmin):
    list_display = ("code", "display_name", "gender", "is_active", "default_language", "max_output_chars")
    list_filter = ("is_active", "default_language", "gender")
    search_fields = ("code", "display_name", "gender")
    readonly_fields = ("created_at", "updated_at")


@admin.register(BotIdentity)
class BotIdentityAdmin(admin.ModelAdmin):
    list_display = ("bot", "core_tone", "background_seed", "memory_strength", "memory_noise")
    list_filter = ("core_tone", "background_seed")
    search_fields = ("bot__code", "bot__display_name")
    raw_id_fields = ("bot",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(BotUserState)
class BotUserStateAdmin(admin.ModelAdmin):
    list_display = (
        "bot",
        "user",
        "familiarity",
        "trust",
        "emotional_closeness",
        "initiation_opt_in",
        "last_user_message_at",
        "last_bot_reply_at",
    )
    list_filter = ("initiation_opt_in",)
    search_fields = ("bot__code", "bot__display_name", "user__telegram_id", "user__telegram_username")
    raw_id_fields = ("bot", "user")
    readonly_fields = ("created_at", "updated_at", "last_user_message_at", "last_bot_reply_at", "last_initiation_at")


@admin.register(MemoryFragment)
class MemoryFragmentAdmin(admin.ModelAdmin):
    list_display = (
        "state",
        "kind",
        "topic",
        "confidence",
        "is_active",
        "times_reinforced",
        "last_seen_at",
        "created_at",
    )
    list_filter = ("kind", "is_active")
    search_fields = ("topic", "hint_text")
    raw_id_fields = ("state",)
    readonly_fields = ("created_at", "updated_at", "last_seen_at")


@admin.register(PromptSnippet)
class PromptSnippetAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "is_active", "version", "created_at")
    list_filter = ("is_active",)
    search_fields = ("key", "title")
    readonly_fields = ("created_at", "updated_at")
