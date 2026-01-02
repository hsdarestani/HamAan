import json
import os
from typing import Any

from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from billing.models import CoinPack, CoinTxn, Purchase, apply_coin_txn, ensure_wallet
from chat.models import Conversation, Message, next_message_seq
from persona.models import Bot, BotUserState
from users.models import User


def _load_json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _get_default_bot() -> Bot | None:
    return Bot.objects.filter(is_active=True).order_by("created_at").first()


def _touch_user(telegram_payload: dict[str, Any]) -> User:
    chat = telegram_payload.get("message", {}).get("chat", {}) or telegram_payload.get("callback_query", {}).get("message", {}).get("chat", {})
    telegram_id = chat.get("id")
    username = chat.get("username", "")
    first_name = chat.get("first_name", "")
    last_name = chat.get("last_name", "")
    if telegram_id is None:
        raise ValueError("missing chat.id")
    defaults = {
        "telegram_username": username or "",
        "first_name": first_name or "",
        "last_name": last_name or "",
        "last_seen_at": timezone.now(),
    }
    user, _ = User.objects.update_or_create(telegram_id=int(telegram_id), defaults=defaults)
    ensure_wallet(user)
    return user


def _ensure_conversation(user: User, bot: Bot | None) -> Conversation | None:
    if not bot:
        return None
    conversation, _ = Conversation.objects.get_or_create(
        user=user,
        bot=bot,
        status=Conversation.Status.ACTIVE,
        defaults={"last_activity_at": timezone.now()},
    )
    BotUserState.objects.get_or_create(user=user, bot=bot)
    return conversation


def _reply_payload(text: str, keyboard: list[list[dict[str, str]]] | None = None, parse_mode: str | None = None):
    payload: dict[str, Any] = {"text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return payload


def _start_replies():
    keyboard = [
        [
            {"text": "شروع کنیم", "callback_data": "start_now"},
            {"text": "یه کم درباره‌اش بگو", "callback_data": "about"},
        ],
        [
            {"text": "سکه‌هام", "callback_data": "balance"},
            {"text": "خرید سکه", "callback_data": "buy_coins"},
        ],
        [{"text": "تنظیمات", "callback_data": "settings"}],
    ]
    return [
        _reply_payload("سلام. من اینجام که حرف بزنی. نه نصیحت می‌کنم، نه سؤال‌پیچت می‌کنم."),
        _reply_payload("اگه دوست داری همین الان هرچی تو ذهنته بنویس.\nاگه هم حوصله نداری، می‌تونیم فقط چند دقیقه سکوت کنیم."),
        _reply_payload("چند پیام اول مهمون من. بعدش برای هر جواب، یه سکه کم می‌شه. هر وقت نخواستی، قطعش کن.", keyboard=keyboard),
    ]


def _about_replies():
    return [
        _reply_payload("اینجا برای حرف زدنه."),
        _reply_payload("جواب‌ها کوتاهه."),
        _reply_payload("سکه‌ایه؛ هر جواب یه سکه."),
    ]


def _manual_bot_reply(conversation: Conversation, text: str) -> Message:
    seq = next_message_seq(conversation.id)
    msg = Message.objects.create(conversation=conversation, role=Message.Role.BOT, text=text, seq=seq)
    now = timezone.now()
    Conversation.objects.filter(id=conversation.id).update(
        last_activity_at=now, last_bot_reply_at=now, has_unread_bot_message=True, updated_at=now
    )
    return msg


def _record_user_message(conversation: Conversation, text: str, telegram_ids: dict[str, Any]) -> Message:
    seq = next_message_seq(conversation.id)
    msg = Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        text=text or "",
        seq=seq,
        telegram_message_id=telegram_ids.get("message_id"),
        telegram_update_id=telegram_ids.get("update_id"),
    )
    now = timezone.now()
    Conversation.objects.filter(id=conversation.id).update(
        last_activity_at=now, last_user_message_at=now, has_unread_bot_message=False, updated_at=now
    )
    return msg


def _balance_reply(user: User):
    wallet = ensure_wallet(user)
    return _reply_payload(f"سکه فعلی: {wallet.balance}")


def _coin_pack_buttons():
    packs = CoinPack.objects.filter(is_active=True).order_by("sort_order", "coins")[:5]
    if not packs:
        return None
    return [[{"text": f"{p.coins} سکه", "callback_data": f"pack:{p.code}"}] for p in packs]


def _create_purchase(user: User, pack: CoinPack) -> Purchase:
    return Purchase.objects.create(
        user=user,
        pack=pack,
        status=Purchase.Status.PENDING,
        gateway=Purchase.Gateway.SANDBOX,
        currency=pack.currency,
        amount=pack.price_amount,
        coins=pack.coins,
        expires_at=timezone.now() + timezone.timedelta(hours=2),
    )


def _paywall_replies():
    keyboard = [
        [{"text": "خرید سکه", "callback_data": "buy_coins"}],
        [{"text": "دیدن بسته‌ها", "callback_data": "packs"}],
        [{"text": "فعلاً نه", "callback_data": "no_pay"}],
    ]
    return [
        _reply_payload("الان سکه‌ات تموم شده.\nاگه دوست داشتی ادامه بدیم، یه بسته بردار. اگر هم نه، اوکیه.", keyboard=keyboard)
    ]


def _settings_reply():
    return _reply_payload("تنظیمات ساده:\n- کم‌حرف‌تر باش\n- یه کم بیشتر بپرس\n- ربات گاهی سر بزنه / نزنه\n- پاک کردن داده‌ها (اختیاری)")


def _handle_message(user: User, bot: Bot | None, text: str, update: dict[str, Any]):
    conversation = _ensure_conversation(user, bot)
    if not conversation:
        return [_reply_payload("هیچ بات فعالی پیدا نشد.")]

    normalized = (text or "").strip()
    if normalized in {"/start", "start"}:
        return _start_replies()
    if normalized in {"شروع کنیم", "start_now"}:
        _record_user_message(conversation, text, update.get("message", {}))
        return [_reply_payload("هرچی هست همین‌جا بگو.")]
    if normalized in {"یه کم درباره‌اش بگو", "about"}:
        return _about_replies()
    if normalized in {"سکه‌هام", "balance"}:
        return [_balance_reply(user)]
    if normalized in {"تنظیمات", "settings"}:
        return [_settings_reply()]
    if normalized in {"خرید سکه", "buy_coins", "packs"}:
        pack_buttons = _coin_pack_buttons()
        if pack_buttons:
            return [_reply_payload("یک بسته انتخاب کن:", keyboard=pack_buttons)]
        return [_reply_payload("بسته‌ای تعریف نشده.")]
    if normalized.startswith("pack:"):
        code = normalized.split(":", 1)[1]
        try:
            pack = CoinPack.objects.get(code=code, is_active=True)
        except CoinPack.DoesNotExist:
            return [_reply_payload("این بسته وجود ندارد.")]
        purchase = _create_purchase(user, pack)
        pay_link = f"https://pay.example.com/{purchase.id}"
        return [
            _reply_payload(f"برای {pack.coins} سکه، این لینکه:\n{pay_link}"),
            _reply_payload("بعد از پرداخت، سکه‌ها اضافه می‌شن. هر وقت خواستی ادامه بده."),
        ]

    # Regular chat flow
    _record_user_message(conversation, text, update.get("message", {}))
    wallet = ensure_wallet(user)
    if wallet.balance <= 0:
        return _paywall_replies()

    bot_reply = _manual_bot_reply(conversation, "شنیدم. هرچی تو ذهنته بگو.")
    try:
        apply_coin_txn(
            user=user,
            delta=-1,
            reason=CoinTxn.Reason.CHAT_REPLY_DEBIT,
            ref_type="message",
            ref_id=str(bot_reply.id),
        )
    except Exception:  # noqa: BLE001
        return _paywall_replies()
    return [_reply_payload(bot_reply.text)]


def _extract_text(payload: dict[str, Any]) -> str:
    if "message" in payload:
        return payload["message"].get("text", "") or ""
    if "callback_query" in payload:
        return payload["callback_query"].get("data", "") or ""
    return ""


@require_http_methods(["GET"])
def HealthCheckView(request):
    """
    Lightweight health endpoint for uptime checks.
    """
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_http_methods(["POST"])
def TelegramWebhookView(request):
    """
    Telegram webhook receiver that drives the conversational onboarding flow.
    Returns a JSON payload with `replies` for the upstream dispatcher to send.
    """
    secret = os.getenv("TELEGRAM_SECRET_TOKEN", "")
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    if secret and got != secret:
        return HttpResponseForbidden("forbidden")

    payload = _load_json(request)
    text = _extract_text(payload)
    bot = _get_default_bot()
    try:
        user = _touch_user(payload)
    except ValueError:
        return HttpResponseBadRequest("missing chat.id")

    replies = _handle_message(user, bot, text, payload)
    return JsonResponse({"ok": True, "replies": replies})


@csrf_exempt
@require_http_methods(["POST"])
def TelegramSetWebhookView(request):
    """
    Stub endpoint to mimic setting Telegram webhook details.
    Accepts a `url` in JSON body and echoes it back.
    """
    data = _load_json(request)
    url = data.get("url")
    if not url:
        return HttpResponseBadRequest("url is required")
    secret = os.getenv("TELEGRAM_SECRET_TOKEN", "")
    return JsonResponse({"ok": True, "url": url, "secret_token_set": bool(secret)})


@require_http_methods(["GET"])
def TelegramWebhookDiagnosticsView(request):
    """
    Returns basic diagnostics about webhook configuration (non-sensitive).
    """
    return JsonResponse(
        {
            "has_secret": bool(os.getenv("TELEGRAM_SECRET_TOKEN", "")),
            "environment": os.getenv("ENVIRONMENT", "dev"),
        }
    )
