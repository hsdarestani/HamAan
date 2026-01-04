from django.contrib import admin

from .models import CoinPack, CoinTxn, Purchase, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance", "is_frozen", "last_txn_at", "created_at")
    list_filter = ("is_frozen",)
    search_fields = ("user__telegram_id", "user__telegram_username")
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at", "last_txn_at")


@admin.register(CoinTxn)
class CoinTxnAdmin(admin.ModelAdmin):
    list_display = ("user", "delta", "reason", "idempotency_key", "balance_after", "created_at")
    list_filter = ("reason",)
    search_fields = ("user__telegram_id", "user__telegram_username", "idempotency_key", "ref_id")
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"


@admin.register(CoinPack)
class CoinPackAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "coins", "price_amount", "currency", "is_active", "sort_order")
    list_filter = ("is_active", "currency")
    search_fields = ("code", "title")
    ordering = ("sort_order",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "pack",
        "status",
        "gateway",
        "amount",
        "coins",
        "credit_txn_id",
        "created_at",
    )
    list_filter = ("status", "gateway")
    search_fields = (
        "id",
        "user__telegram_id",
        "user__telegram_username",
        "gateway_authority",
        "gateway_ref_id",
    )
    raw_id_fields = ("user", "pack")
    readonly_fields = ("created_at", "updated_at", "credit_txn_id")
