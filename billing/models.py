# billing/models.py
from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True


class Wallet(TimeStampedModel):
    """
    Wallet is a cached balance. Source of truth is CoinTxn ledger.
    Keep balance as integer "coins" for MVP simplicity.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet",
        primary_key=True,
    )

    # Cached balance (coins). Always keep >= 0.
    balance = models.BigIntegerField(default=0, validators=[MinValueValidator(0)], db_index=True)

    # Soft flags
    is_frozen = models.BooleanField(default=False, db_index=True)
    freeze_reason = models.CharField(max_length=255, blank=True, default="")

    # Internal bookkeeping
    last_txn_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "billing_wallet"
        indexes = [
            models.Index(fields=["balance"]),
            models.Index(fields=["is_frozen"]),
        ]

    def __str__(self) -> str:
        return f"Wallet(user={self.user_id}, balance={self.balance})"


class CoinTxn(TimeStampedModel):
    """
    Ledger entry (source of truth). Every change to wallet must be recorded here.

    delta:
      +N => credit
      -N => debit

    Idempotency:
      - Use idempotency_key for external events (payment callbacks, webhook retries, etc.)
      - Enforce uniqueness per user to avoid double credit/debit.
    """

    class Reason(models.TextChoices):
        # Credits
        PURCHASE_CREDIT = "PURCHASE_CREDIT", "Purchase credit"
        ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT", "Admin adjustment"
        PROMO_CREDIT = "PROMO_CREDIT", "Promo credit"
        REFUND_CREDIT = "REFUND_CREDIT", "Refund credit"

        # Debits
        CHAT_REPLY_DEBIT = "CHAT_REPLY_DEBIT", "Chat reply debit"
        TOOLING_DEBIT = "TOOLING_DEBIT", "Tooling debit"
        REVERSAL_DEBIT = "REVERSAL_DEBIT", "Reversal debit"

        # Neutral / audit
        NOTE = "NOTE", "Note"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coin_txns",
        db_index=True,
    )

    # Signed integer delta
    delta = models.BigIntegerField(help_text="Signed coin delta. +credit / -debit")
    reason = models.CharField(max_length=32, choices=Reason.choices, db_index=True)

    # Optional linkage to conversation/message/purchase etc.
    ref_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    ref_id = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # Optional: idempotency for retried events (purchase callbacks, etc.)
    idempotency_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # Audit / metadata (small)
    meta = models.JSONField(default=dict, blank=True)

    # Denormalized resulting balance snapshot (helps audits; not used as source-of-truth)
    balance_after = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "billing_cointxn"
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["reason", "created_at"]),
            models.Index(fields=["ref_type", "ref_id"]),
        ]
        constraints = [
            # Do not allow zero-delta transactions
            models.CheckConstraint(check=~Q(delta=0), name="billing_cointxn_delta_nonzero"),

            # If idempotency_key is present, it must be unique per user
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="billing_cointxn_unique_user_idempotency_key",
            ),
        ]

    def __str__(self) -> str:
        return f"CoinTxn(user={self.user_id}, delta={self.delta}, reason={self.reason})"


class CoinPack(TimeStampedModel):
    """
    Sellable coin packs. Stored in DB so you can manage it via admin.
    """

    code = models.SlugField(max_length=32, unique=True)
    title = models.CharField(max_length=64)
    coins = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    is_active = models.BooleanField(default=True, db_index=True)

    # Money fields:
    # - If you're charging in IRR, store integer rials to avoid floats.
    # - If you're charging in USD for some gateways, you can use Decimal.
    currency = models.CharField(max_length=8, default="IRR", db_index=True)
    price_amount = models.BigIntegerField(validators=[MinValueValidator(0)], help_text="Price in minor units (e.g., Rials)")

    # Optional: marketing
    sort_order = models.PositiveSmallIntegerField(default=100, db_index=True)
    tag = models.CharField(max_length=32, blank=True, default="")  # e.g. "best_value"

    class Meta:
        db_table = "billing_coinpack"
        indexes = [
            models.Index(fields=["is_active", "sort_order"]),
            models.Index(fields=["currency"]),
        ]

    def __str__(self) -> str:
        return f"{self.code}({self.coins} coins)"


class Purchase(TimeStampedModel):
    """
    Purchase intent + gateway lifecycle.

    Flow:
      - PENDING: created, waiting for gateway/payment
      - PAID: confirmed by gateway
      - CREDITED: coins credited into wallet (idempotent)
      - FAILED/CANCELED/EXPIRED: terminal states
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PAID = "PAID", "Paid"
        CREDITED = "CREDITED", "Credited"
        FAILED = "FAILED", "Failed"
        CANCELED = "CANCELED", "Canceled"
        EXPIRED = "EXPIRED", "Expired"

    class Gateway(models.TextChoices):
        ZARINPAL = "ZARINPAL", "Zarinpal"
        ZIBAL = "ZIBAL", "Zibal"
        PAYIR = "PAYIR", "Pay.ir"
        MANUAL = "MANUAL", "Manual"
        SANDBOX = "SANDBOX", "Sandbox"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="purchases",
        db_index=True,
    )

    pack = models.ForeignKey(
        CoinPack,
        on_delete=models.PROTECT,
        related_name="purchases",
        db_index=True,
    )

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    gateway = models.CharField(max_length=16, choices=Gateway.choices, default=Gateway.SANDBOX, db_index=True)

    # Pricing snapshot (to avoid later changes affecting old purchases)
    currency = models.CharField(max_length=8, default="IRR", db_index=True)
    amount = models.BigIntegerField(validators=[MinValueValidator(0)], help_text="Charged amount in minor units (e.g., Rials)")
    coins = models.PositiveIntegerField(validators=[MinValueValidator(1)], help_text="Coins to credit for this purchase")

    # Gateway references
    gateway_authority = models.CharField(max_length=128, blank=True, default="", db_index=True)  # e.g., authority/token
    gateway_ref_id = models.CharField(max_length=128, blank=True, default="", db_index=True)     # e.g., refId/trackId
    gateway_card_pan = models.CharField(max_length=32, blank=True, default="")                   # best-effort
    gateway_raw = models.JSONField(default=dict, blank=True)                                     # full callback payload

    # Idempotency for credit step
    credit_txn_id = models.UUIDField(null=True, blank=True, db_index=True)

    # Expiration (optional)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Client metadata (optional; keep small)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        db_table = "billing_purchase"
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["gateway", "gateway_authority"]),
            models.Index(fields=["gateway_ref_id"]),
        ]
        constraints = [
            # gateway_ref_id (when present) should be unique to avoid double-credit
            models.UniqueConstraint(
                fields=["gateway", "gateway_ref_id"],
                condition=~Q(gateway_ref_id=""),
                name="billing_purchase_unique_gateway_ref",
            ),
        ]

    def __str__(self) -> str:
        return f"Purchase({self.id}, user={self.user_id}, status={self.status}, amount={self.amount})"


# --------- Service-like helpers (kept inside models.py for MVP simplicity) ---------

def ensure_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


@transaction.atomic
def apply_coin_txn(
    *,
    user,
    delta: int,
    reason: str,
    ref_type: str = "",
    ref_id: str = "",
    idempotency_key: str = "",
    meta: dict | None = None,
) -> CoinTxn:
    """
    Atomic ledger + wallet update with idempotency support.

    - Creates a CoinTxn
    - Updates Wallet.balance using F() to avoid race conditions
    - Stores balance_after on the ledger row

    Raises:
      - ValueError for invalid delta or insufficient balance
      - IntegrityError if idempotency_key already used for this user
    """
    if delta == 0:
        raise ValueError("delta must be non-zero")

    wallet = ensure_wallet(user)

    if wallet.is_frozen:
        raise ValueError("wallet is frozen")

    # For debits, ensure sufficient balance
    if delta < 0:
        # Lock wallet row for safe balance check
        wallet = Wallet.objects.select_for_update().get(user=user)
        if wallet.balance + delta < 0:
            raise ValueError("insufficient balance")

    txn = CoinTxn.objects.create(
        user=user,
        delta=delta,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
        meta=meta or {},
    )

    # Update wallet (atomic)
    Wallet.objects.filter(user=user).update(
        balance=F("balance") + delta,
        last_txn_at=timezone.now(),
        updated_at=timezone.now(),
    )

    # Snapshot resulting balance for audits
    wallet.refresh_from_db(fields=["balance"])
    txn.balance_after = wallet.balance
    txn.save(update_fields=["balance_after"])

    return txn


@transaction.atomic
def credit_purchase_once(*, purchase: Purchase) -> CoinTxn:
    """
    Credits purchase coins into wallet exactly once.
    Uses Purchase.credit_txn_id + ledger idempotency to ensure idempotence.

    Expected status transitions:
      PENDING -> PAID -> CREDITED

    You can call this multiple times safely.
    """
    purchase = Purchase.objects.select_for_update().get(id=purchase.id)

    if purchase.status not in (Purchase.Status.PAID, Purchase.Status.CREDITED):
        raise ValueError("purchase must be PAID (or already CREDITED)")

    if purchase.status == Purchase.Status.CREDITED and purchase.credit_txn_id:
        # Already credited
        return CoinTxn.objects.get(id=purchase.credit_txn_id)

    # Idempotency key: gateway + ref id (or purchase id fallback)
    idem = f"purchase:{purchase.gateway}:{purchase.gateway_ref_id or purchase.id}"

    txn = apply_coin_txn(
        user=purchase.user,
        delta=int(purchase.coins),
        reason=CoinTxn.Reason.PURCHASE_CREDIT,
        ref_type="purchase",
        ref_id=str(purchase.id),
        idempotency_key=idem,
        meta={"gateway": purchase.gateway, "gateway_ref_id": purchase.gateway_ref_id},
    )

    purchase.status = Purchase.Status.CREDITED
    purchase.credit_txn_id = txn.id
    purchase.save(update_fields=["status", "credit_txn_id", "updated_at"])

    return txn

