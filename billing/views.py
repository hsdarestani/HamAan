import json
from uuid import UUID

from django.http import HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from users.models import User
from .models import CoinPack, CoinTxn, Purchase, Wallet, credit_purchase_once, ensure_wallet


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


@require_http_methods(["GET"])
def WalletView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    wallet = ensure_wallet(user)
    payload = {
        "user_id": str(wallet.user_id),
        "balance": wallet.balance,
        "is_frozen": wallet.is_frozen,
        "updated_at": wallet.updated_at.isoformat(),
    }
    return JsonResponse({"ok": True, "wallet": payload})


@require_http_methods(["GET"])
def WalletBalanceView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    wallet = ensure_wallet(user)
    return JsonResponse({"ok": True, "balance": wallet.balance})


@require_http_methods(["GET"])
def CoinPackListView(request):
    packs = CoinPack.objects.filter(is_active=True).order_by("sort_order", "coins")
    return JsonResponse(
        {
            "ok": True,
            "packs": [
                {
                    "id": pack.id,
                    "code": pack.code,
                    "title": pack.title,
                    "coins": pack.coins,
                    "currency": pack.currency,
                    "price_amount": pack.price_amount,
                    "tag": pack.tag,
                }
                for pack in packs
            ],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def PurchaseCreateView(request):
    data = _load_json(request)
    user = _find_user(data, request.GET)
    pack_code = data.get("pack_code")
    if not user or not pack_code:
        return JsonResponse({"ok": False, "error": "user_or_pack_missing"}, status=400)
    try:
        pack = CoinPack.objects.get(code=pack_code, is_active=True)
    except CoinPack.DoesNotExist:
        return JsonResponse({"ok": False, "error": "pack_not_found"}, status=404)

    purchase = Purchase.objects.create(
        user=user,
        pack=pack,
        status=Purchase.Status.PENDING,
        gateway=data.get("gateway") or Purchase.Gateway.SANDBOX,
        currency=pack.currency,
        amount=pack.price_amount,
        coins=pack.coins,
        expires_at=timezone.now() + timezone.timedelta(hours=2),
        client_ip=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
    return JsonResponse({"ok": True, "purchase_id": str(purchase.id), "status": purchase.status})


@require_http_methods(["GET"])
def PurchaseStatusView(request):
    purchase_id = request.GET.get("purchase_id")
    if not purchase_id:
        return HttpResponseBadRequest("purchase_id is required")
    try:
        purchase = Purchase.objects.get(id=UUID(str(purchase_id)))
    except (Purchase.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "purchase_not_found"}, status=404)
    payload = {
        "id": str(purchase.id),
        "status": purchase.status,
        "gateway": purchase.gateway,
        "amount": purchase.amount,
        "coins": purchase.coins,
        "credit_txn_id": str(purchase.credit_txn_id) if purchase.credit_txn_id else None,
    }
    return JsonResponse({"ok": True, "purchase": payload})


@csrf_exempt
@require_http_methods(["POST"])
def PaymentCallbackView(request):
    data = _load_json(request)
    purchase_id = data.get("purchase_id")
    status_flag = (data.get("status") or "").upper()
    if not purchase_id:
        return HttpResponseBadRequest("purchase_id is required")
    try:
        purchase = Purchase.objects.get(id=UUID(str(purchase_id)))
    except (Purchase.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "purchase_not_found"}, status=404)

    if status_flag in {"PAID", "OK", "SUCCESS"}:
        purchase.status = Purchase.Status.PAID
        purchase.gateway_ref_id = data.get("gateway_ref_id", purchase.gateway_ref_id)
        purchase.save(update_fields=["status", "gateway_ref_id", "updated_at"])
        try:
            txn = credit_purchase_once(purchase=purchase)
        except Exception as exc:  # noqa: BLE001
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "status": purchase.status, "txn_id": str(txn.id)})

    # For any other status, mark as failed
    purchase.status = Purchase.Status.FAILED
    purchase.gateway_raw = data
    purchase.save(update_fields=["status", "gateway_raw", "updated_at"])
    return JsonResponse({"ok": True, "status": purchase.status})


@require_http_methods(["GET"])
def CoinTxnListView(request):
    user = _find_user({}, request.GET)
    if not user:
        return JsonResponse({"ok": False, "error": "user_not_found"}, status=404)
    txns = CoinTxn.objects.filter(user=user).order_by("-created_at")[:200]
    return JsonResponse(
        {
            "ok": True,
            "txns": [
                {
                    "id": str(txn.id),
                    "delta": txn.delta,
                    "reason": txn.reason,
                    "ref_type": txn.ref_type,
                    "ref_id": txn.ref_id,
                    "balance_after": txn.balance_after,
                    "created_at": txn.created_at.isoformat(),
                }
                for txn in txns
            ],
        }
    )
