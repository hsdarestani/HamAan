import hashlib
import json
import logging
import os
import time
from typing import Any

from datetime import datetime
import requests
from openai import OpenAI

from django.db.models import F
from django.http import HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from billing.models import CoinPack, CoinTxn, Purchase, apply_coin_txn, ensure_wallet
from chat.models import Conversation, LLMCallLog, Message, next_message_seq
from persona.models import Bot, BotIdentity, BotUserState, MemoryFragment
from persona.relationship import update_relationship_memory
from users.models import User


class Intent:
    def __init__(self, kind: str, data: dict[str, Any] | None = None):
        self.kind = kind
        self.data = data or {}

    def __repr__(self) -> str:
        return f"Intent(kind={self.kind}, data={self.data})"


logger = logging.getLogger(__name__)
openai_client = OpenAI()


def _load_json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        logger.warning("telegram_webhook: invalid JSON body")
        return {}


def _telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("telegram_webhook: missing TELEGRAM_BOT_TOKEN env, cannot send %s", method)
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception("telegram_webhook: failed %s with payload=%s error=%s", method, payload, exc)
        return None


def _extract_chat(payload: dict[str, Any]) -> dict[str, Any]:
    if "message" in payload:
        return payload["message"].get("chat", {}) or {}
    if "callback_query" in payload:
        return payload["callback_query"].get("message", {}).get("chat", {}) or {}
    return {}


def _detect_intent(text: str) -> Intent:
    normalized = (text or "").strip()
    lower = normalized.lower()
    key_phrases = lower.replace("‌", " ")

    def _has_any(options: set[str]) -> bool:
        return any(opt in key_phrases for opt in options)

    if normalized in {"/start", "start"}:
        return Intent("start")
    if normalized in {"شروع کنیم", "start_now"}:
        return Intent("start_now")
    if normalized in {"یه کم درباره‌اش بگو", "about"}:
        return Intent("about")
    if normalized in {"یه سؤال دقیق‌تر بپرس", "ask_more"}:
        return Intent("ask_more")
    if normalized in {"جمع‌بندی کوتاه", "summary"} or _has_any({"خلاصه", "جمع بندی"}):
        return Intent("summary")
    if normalized in {"سکه‌هام", "balance"} or _has_any({"کیف پول", "سکه", "balance", "wallet"}):
        return Intent("wallet_status")
    if _has_any({"تراکنش", "پرداخت", "خرید"}) and not normalized.startswith("pack:"):
        return Intent("txn_history")
    if normalized.startswith("pack:"):
        return Intent("pack_select", {"code": normalized.split(":", 1)[1]})
    if normalized in {"تنظیمات", "settings"} or _has_any({"تنظیم", "ترجیح"}):
        return Intent("settings")
    if normalized in {"خرید سکه", "buy_coins", "packs"}:
        return Intent("pack_list")
    if normalized in {"دستورات", "commands", "منو", "menu"} or _has_any({"منو", "دستور"}):
        return Intent("command_menu")
    if _has_any({"پروفایل ربات", "هویت ربات", "identity", "persona"}):
        return Intent("bot_profile")
    return Intent("chat")


def _get_default_bot() -> Bot | None:
    return Bot.objects.filter(is_active=True).order_by("created_at").first()


def _default_identity_profile() -> dict[str, list[str]]:
    return {
        "values": ["گوش دادن", "راحت بودن"],
        "dreams": ["گپ‌های طولانی شبانه"],
        "favorites": ["چای دارچین"],
    }


_FEMALE_BOT_NAMES = [
    "سارا",
    "نگار",
    "آوا",
    "هانیه",
    "ندا",
    "مهسا",
    "پرستو",
    "نرگس",
    "یاسمن",
    "النا",
]

_MALE_BOT_NAMES = [
    "علی",
    "رضا",
    "مهدی",
    "سینا",
    "پویان",
    "کامران",
    "سامان",
    "بهزاد",
    "کیوان",
    "فرهاد",
]


def _pick_bot_gender(user: User) -> str:
    """Select a gender opposite to the user when known; default to female."""

    gender = (user.gender or "").upper()
    if gender == "MALE":
        return "FEMALE"
    if gender == "FEMALE":
        return "MALE"
    return "FEMALE"


def _pick_bot_name(gender: str, user: User) -> str:
    """Choose a stable Iranian human name for the bot based on gender and user."""

    names = _FEMALE_BOT_NAMES if gender == "FEMALE" else _MALE_BOT_NAMES
    if not names:
        return "دوست"
    key = f"{gender}:{user.telegram_id or user.id}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big") % len(names)
    return names[idx]


def _llm_personal_traits(user: User, gender: str) -> dict[str, Any]:
    """
    Use the LLM to generate a random Iranian name and a lightweight identity profile.

    Returns a dict like:
    {
        "name": "...",
        "identity_profile": {"values": [...], "dreams": [...], "favorites": [...]},
    }
    """

    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("telegram_webhook: missing OPENAI_API_KEY, using fallback bot traits")
        return {}

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    name_hint = user.first_name or user.telegram_username or "دوست"
    try:
        response = openai_client.chat.completions.create(
            model=model,
            temperature=0.9,
            max_tokens=180,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are generating traits for a Persian-speaking companion bot. "
                        "Respond ONLY with JSON. Use short Persian phrases. "
                        "Pick a single Iranian first name matching the provided gender. "
                        "Keep identity lists to 1-3 short items each."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "gender": gender,
                            "user_hint": name_hint,
                            "needs": ["name", "identity_profile"],
                        }
                    ),
                },
            ],
        )
        content = response.choices[0].message.content or ""
        return json.loads(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_webhook: llm traits failed (%s), using fallback bot traits", exc)
        return {}


def _personal_bot_code(template: Bot, user: User) -> str:
    base = template.code or "bot"
    suffix = str(user.telegram_id or "user")
    code = f"{base}-u{suffix}".lower()
    return code[:32]


def _clone_identity_from_template(bot: Bot, template: Bot, *, identity_profile: dict[str, Any] | None = None) -> None:
    existing_identity = BotIdentity.objects.filter(bot=bot).first()
    if existing_identity:
        if identity_profile:
            BotIdentity.objects.filter(bot=bot).update(identity_profile=identity_profile, updated_at=timezone.now())
        return

    template_identity: BotIdentity | None = getattr(template, "identity", None)
    profile = identity_profile or _default_identity_profile()
    defaults = {
        "core_tone": "WARM",
        "background_seed": "FRESH_START",
        "self_confidence": 0.35,
        "openness": 0.32,
        "talkativeness": 0.42,
        "emotional_clarity": 0.35,
        "memory_strength": 0.60,
        "memory_noise": 0.15,
        "identity_profile": profile,
        "avoids_advice": True,
        "avoids_therapy_tone": True,
        "avoids_omniscience": True,
    }

    if template_identity:
        template_profile = dict(template_identity.identity_profile or {})
        profile = profile or template_profile or _default_identity_profile()
        defaults.update(
            {
                "core_tone": template_identity.core_tone,
                "background_seed": template_identity.background_seed,
                "self_confidence": template_identity.self_confidence,
                "openness": template_identity.openness,
                "talkativeness": template_identity.talkativeness,
                "emotional_clarity": template_identity.emotional_clarity,
                "memory_strength": template_identity.memory_strength,
                "memory_noise": template_identity.memory_noise,
                "identity_profile": profile or template_profile or _default_identity_profile(),
                "avoids_advice": template_identity.avoids_advice,
                "avoids_therapy_tone": template_identity.avoids_therapy_tone,
                "avoids_omniscience": template_identity.avoids_omniscience,
            }
        )

    BotIdentity.objects.get_or_create(bot=bot, defaults=defaults)


def _ensure_personal_bot(user: User) -> Bot | None:
    template = _get_default_bot()
    if not template:
        return None

    code = _personal_bot_code(template, user)
    gender = _pick_bot_gender(user)
    traits = _llm_personal_traits(user, gender)
    bot_name = (traits.get("name") or "").strip() if isinstance(traits, dict) else ""
    bot_name = bot_name or _pick_bot_name(gender, user)
    name_hint = user.first_name or user.telegram_username or "دوست"
    personal_prompt = template.base_prompt_text.strip() if template.base_prompt_text else ""
    personal_hint = (
        f"این بات همراه اختصاصی {name_hint} است؛ لحن انسانی و خودمانی داشته باش، "
        "گاهی یک اشاره کوتاه به حال و هوای خودت یا اینکه مشغول گوش دادن هستی بکن، "
        "ولی داستان‌پردازی نکن."
    )
    if personal_prompt:
        personal_prompt = f"{personal_prompt}\n{personal_hint}"
    else:
        personal_prompt = (
            "تو یک همراه گفت‌وگو و دوست شنوا هستی. کوتاه و صمیمی به فارسی پاسخ بده و خودت را ربات معرفی نکن. "
            + personal_hint
        )

    bot_defaults = {
        "display_name": bot_name,
        "is_active": True,
        "base_prompt_id": template.base_prompt_id,
        "base_prompt_text": personal_prompt,
        "default_language": template.default_language,
        "avatar_key": template.avatar_key,
        "max_output_chars": template.max_output_chars,
        "max_questions_per_reply": template.max_questions_per_reply,
        "gender": gender,
    }
    bot, _ = Bot.objects.get_or_create(code=code, defaults=bot_defaults)
    updates = {}
    if bot.gender != gender:
        updates["gender"] = gender
    if bot.display_name != bot_name:
        updates["display_name"] = bot_name
    if updates:
        updates["updated_at"] = timezone.now()
        Bot.objects.filter(id=bot.id).update(**updates)
    identity_profile = {}
    if isinstance(traits, dict):
        identity_profile = traits.get("identity_profile") or {}
    _clone_identity_from_template(bot, template, identity_profile=identity_profile)

    if user.assigned_bot_id != bot.id:
        user.assigned_bot = bot
        user.save(update_fields=["assigned_bot", "updated_at"])

    return bot


def _resolve_user_bot(user: User) -> Bot | None:
    """
    Ensure every user has a persistent bot anchor.
    - If user.assigned_bot is set, prefer it.
    - Otherwise, pick the default bot and assign it to the user for future requests.
    """
    if user.assigned_bot:
        return user.assigned_bot

    personal_bot = _ensure_personal_bot(user)
    if personal_bot:
        return personal_bot

    bot = _get_default_bot()
    if bot:
        user.assigned_bot = bot
        user.save(update_fields=["assigned_bot", "updated_at"])
    return bot


def _touch_user(telegram_payload: dict[str, Any]) -> User:
    chat = _extract_chat(telegram_payload)
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
    user, created = User.objects.update_or_create(telegram_id=int(telegram_id), defaults=defaults)
    if not user.assigned_bot:
        _ensure_personal_bot(user)
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


def _format_ts(dt: datetime | None) -> str:
    if not dt:
        return "نامشخص"
    return dt.astimezone(timezone.get_current_timezone()).strftime("%Y-%m-%d %H:%M")


def _reply_payload(text: str, keyboard: list[list[dict[str, str]]] | None = None, parse_mode: str | None = None):
    payload: dict[str, Any] = {"text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return payload


def _last_user_message_text(conversation: Conversation) -> str:
    last_user = conversation.messages.filter(role=Message.Role.USER).order_by("-seq").first()
    return (last_user.text or "").strip() if last_user else ""


def _wallet_snapshot_reply(user: User) -> dict[str, Any]:
    wallet = ensure_wallet(user)
    last_txn = user.coin_txns.order_by("-created_at").first()
    if last_txn:
        sign = "+" if last_txn.delta > 0 else "-"
        txn_line = f"آخرین تراکنش: {sign}{abs(last_txn.delta)} ({last_txn.get_reason_display()}) در {_format_ts(last_txn.created_at)}."
    else:
        txn_line = "تراکنشی ثبت نشده."
    return _reply_payload(f"کیف پول: {wallet.balance} سکه.\n{txn_line}")


def _txn_history_replies(user: User) -> list[dict[str, Any]]:
    txns = list(user.coin_txns.order_by("-created_at")[:5])
    if not txns:
        return [_reply_payload("هیچ تراکنشی پیدا نشد.")]

    lines = []
    for txn in txns:
        sign = "+" if txn.delta > 0 else "-"
        lines.append(f"{_format_ts(txn.created_at)} | {txn.get_reason_display()} | {sign}{abs(txn.delta)} | موجودی پس از آن: {txn.balance_after or 'نامشخص'}")
    body = "آخرین تراکنش‌ها:\n" + "\n".join(lines)
    return [_reply_payload(body)]


def _bot_profile_replies(bot: Bot, user: User) -> list[dict[str, Any]]:
    identity: BotIdentity | None = getattr(bot, "identity", None)
    state = BotUserState.objects.filter(bot=bot, user=user).first()
    tone = identity.core_tone if identity else "نامشخص"
    background = identity.background_seed if identity else "نامشخص"
    familiarity = f"{state.familiarity:.2f}" if state else "0.00"
    trust = f"{state.trust:.2f}" if state else "0.00"
    verbosity = f"{state.user_pref_verbosity:.2f}" if state else "0.00"
    questions = f"{state.user_pref_questions:.2f}" if state else "0.00"
    lines = [
        f"بات: {bot.display_name} ({bot.code})",
        f"هویت پایه: لحن={tone} / پس‌زمینه={background}",
        f"آشنایی/اعتماد: {familiarity} / {trust}",
        f"ترجیحات پاسخ: verbosity={verbosity} / questions={questions}",
    ]
    return [_reply_payload("\n".join(lines))]


def _command_menu_replies(conversation: Conversation, user: User) -> list[dict[str, Any]]:
    wallet = ensure_wallet(user)
    keyboard = [
        [{"text": "💰 کیف پول", "callback_data": "wallet"}],
        [{"text": "🧾 تراکنش‌ها", "callback_data": "txns"}],
        [{"text": "🧠 خلاصه گفتگو", "callback_data": "summary"}],
        [{"text": "🪪 پروفایل ربات", "callback_data": "bot_profile"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "settings"}],
    ]
    hint = (
        f"موجودی فعلی: {wallet.balance} سکه. "
        "از منو می‌تونی وضعیت کیف پول، تراکنش‌ها، خلاصه گفتگو و هویت ربات رو ببینی."
    )
    return [_reply_payload(hint, keyboard=keyboard)]


def _contextual_keyboard(conversation: Conversation, wallet_balance: int, last_user_text: str):
    has_history = conversation.messages.exists()
    keyboard: list[list[dict[str, str]]] = []
    is_question = "?" in last_user_text or "؟" in last_user_text
    closing_keywords = {"مرسی", "ممنون", "خدافظ", "خداحافظ", "فعلاً", "بای"}
    is_closing = any(k in last_user_text for k in closing_keywords)
    is_low_balance = wallet_balance <= 2

    if has_history:
        row = [{"text": "ادامه بدیم", "callback_data": "start_now"}]
        if is_question:
            row.append({"text": "یه سؤال دقیق‌تر بپرس", "callback_data": "ask_more"})
        keyboard.append(row)
    else:
        keyboard.append(
            [
                {"text": "شروع کنیم", "callback_data": "start_now"},
                {"text": "یه کم درباره‌اش بگو", "callback_data": "about"},
            ]
        )

    keyboard.append(
        [
            {"text": "دستورات", "callback_data": "commands"},
            {"text": "کیف پول", "callback_data": "wallet"},
        ]
    )
    keyboard.append([{"text": "🧾 تراکنش‌ها", "callback_data": "txns"}, {"text": "⚙️ تنظیمات", "callback_data": "settings"}])

    if is_closing and has_history:
        keyboard.append([{"text": "جمع‌بندی کوتاه", "callback_data": "summary"}])

    if is_low_balance:
        keyboard.append([{"text": "خرید سکه", "callback_data": "buy_coins"}])
    elif wallet_balance > 0 and not is_closing and not is_question:
        keyboard.append([{"text": "سکه‌هام", "callback_data": "balance"}])

    return keyboard


def _start_replies(user: User, conversation: Conversation):
    wallet = ensure_wallet(user)
    has_history = conversation.messages.exists()
    last_user_text = _last_user_message_text(conversation)
    keyboard = _contextual_keyboard(conversation, wallet.balance, last_user_text)

    if has_history:
        welcome = "سلام دوباره. از همین‌جا می‌تونیم ادامه بدیم؛ جواب‌هام کوتاهه و بی‌نصیحت."
    else:
        welcome = "سلام. اینجا می‌تونی راحت حرف بزنی؛ جواب‌هام کوتاهه و گیر نمی‌دم."

    if wallet.balance <= 0:
        coin_hint = "چند پیام اول مهمون من، ولی الان سکه‌ات تموم شده."
        question = "می‌خوای شروع کنیم یا اول سکه بگیری؟"
    elif has_history:
        coin_hint = "هر جواب یه سکه کم می‌شه؛ هر وقت خواستی قطعش می‌کنیم."
        question = "می‌خوای ادامه بدیم یا اول تنظیمات/سکه‌ها رو ببینی؟"
    else:
        coin_hint = "چند پیام اول مهمون من؛ بعد هر جواب یه سکه کم می‌شه."
        question = "شروع کنیم یا اول یه توضیح کوتاه بدم؟"

    text = f"{welcome}\n{coin_hint}\n{question}"
    return [_reply_payload(text, keyboard=keyboard)]


def _about_replies():
    return [
        _reply_payload("اینجا برای حرف زدنه."),
        _reply_payload("جواب‌ها کوتاهه."),
        _reply_payload("سکه‌ایه؛ هر جواب یه سکه."),
    ]


def _manual_bot_reply(conversation: Conversation, text: str) -> Message:
    return _record_bot_message(conversation, text)


def _record_bot_message(
    conversation: Conversation,
    text: str,
    *,
    token_in: int = 0,
    token_out: int = 0,
) -> Message:
    seq = next_message_seq(conversation.id)
    msg = Message.objects.create(
        conversation=conversation,
        role=Message.Role.BOT,
        text=text or "",
        seq=seq,
        token_in=token_in,
        token_out=token_out,
    )
    now = timezone.now()
    Conversation.objects.filter(id=conversation.id).update(
        last_activity_at=now, last_bot_reply_at=now, has_unread_bot_message=True, updated_at=now
    )
    _refresh_memory_summary(conversation)
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
    state = BotUserState.objects.filter(user=conversation.user, bot=conversation.bot).first()
    if state:
        BotUserState.objects.filter(id=state.id).update(
            last_user_message_at=now,
            total_user_messages=F("total_user_messages") + 1,
            updated_at=now,
        )
    Conversation.objects.filter(id=conversation.id).update(
        last_activity_at=now, last_user_message_at=now, has_unread_bot_message=False, updated_at=now
    )
    if state:
        _upsert_memory_fragment(state, text, now, source_ref=msg.id)
        update_relationship_memory(state, _recent_user_texts(conversation), latest_text=text, now=now)
    _refresh_memory_summary(conversation)
    return msg


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


def _recent_messages(conversation: Conversation, limit: int = 30):
    history = list(conversation.messages.order_by("-seq")[:limit])
    history.reverse()
    return history


def _refresh_memory_summary(conversation: Conversation, *, max_chars: int = 900, window: int = 40):
    recent_messages = list(conversation.messages.order_by("-seq")[:window])
    recent_messages.reverse()
    lines: list[str] = []
    for msg in recent_messages:
        snippet = (msg.text or "").strip().replace("\n", " ")
        if not snippet:
            continue
        snippet = snippet[:160]
        if msg.role == Message.Role.USER:
            prefix = "کاربر"
        elif msg.role == Message.Role.BOT:
            prefix = "ربات"
        else:
            prefix = "سیستم"
        lines.append(f"{prefix}: {snippet}")

    recent_block = " | ".join(lines[-20:])
    prior = (conversation.memory_summary or "").strip()
    parts = [prior] if prior else []
    if recent_block:
        parts.append(f"گفت‌وگوهای اخیر: {recent_block}")

    new_summary = " / ".join(parts).strip()
    if len(new_summary) > max_chars:
        new_summary = new_summary[:max_chars]

    Conversation.objects.filter(id=conversation.id).update(memory_summary=new_summary, updated_at=timezone.now())
    conversation.memory_summary = new_summary


def _upsert_memory_fragment(state: BotUserState, text: str, now, source_ref):
    topic = (state.bot.default_language or "general")[:64]
    text_snippet = (text or "").strip()[:220]
    if not text_snippet:
        text_snippet = "interaction"
    fragment, created = MemoryFragment.objects.get_or_create(
        state=state,
        topic=topic,
        defaults={
            "kind": MemoryFragment.Kind.TOPIC,
            "hint_text": text_snippet,
            "confidence": 0.55,
            "source_ref": str(source_ref),
            "last_seen_at": now,
        },
    )
    if not created:
        MemoryFragment.objects.filter(id=fragment.id).update(
            hint_text=text_snippet,
            last_seen_at=now,
            times_reinforced=F("times_reinforced") + 1,
            source_ref=str(source_ref),
        )


def _recent_user_texts(conversation: Conversation, limit: int = 20) -> list[str]:
    user_messages = (
        conversation.messages.filter(role=Message.Role.USER)
        .order_by("-seq")
        .values_list("text", flat=True)[:limit]
    )
    return [text for text in user_messages if text]


def _typing_delay(text: str) -> float:
    # Roughly model human typing rhythm; clamp to avoid long sleeps.
    base = 0.4
    per_char = 0.012
    delay = base + len(text) * per_char
    return max(0.35, min(delay, 2.2))


def _split_reply(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    split_points = [text.rfind(sep, 0, limit) for sep in [". ", "؟", "!", "\n"]]
    best = max(split_points)
    if best <= 0:
        best = limit

    first = text[:best].strip()
    rest = text[best:].strip()
    if not rest:
        return [first]
    return [first, rest]


def _decide_conversation_mode(normalized_user_text: str) -> tuple[str, str]:
    text = normalized_user_text.strip()
    if not text:
        return "idle", "پیام خالی یا فقط فاصله بود"

    has_question_mark = "?" in text or "؟" in text
    closing_keywords = {"مرسی", "ممنون", "خدافظ", "خداحافظ", "فعلاً", "بای"}
    guide_keywords = {"راهنما", "گزینه", "چه کاری میشه کرد", "قابلیت"}
    if any(k in text for k in closing_keywords):
        return "answer", "سیگنال پایان یا تشکر"
    if any(k in text for k in guide_keywords):
        return "guide", "درخواست راهنمایی یا گزینه‌ها"

    tokens = text.split()
    if len(tokens) <= 3 and not has_question_mark:
        return "clarify", "پیام کوتاه و مبهم"
    if has_question_mark:
        return "answer", "سؤال مستقیم"
    return "answer", "پاسخ مستقیم بدون نیاز به سؤال"


def _mode_instruction(mode: str) -> str:
    if mode == "answer":
        return "mode=answer: فقط پاسخ بده، هیچ سؤالی نپرس و پیشنهاد را خبری بیان کن."
    if mode == "clarify":
        return "mode=clarify: اگر واقعاً نیاز بود فقط یک سؤال خیلی کوتاه بپرس؛ در غیر این صورت پاسخ مستقیم بده."
    if mode == "guide":
        return "mode=guide: یک گزینه یا قدم بعدی خبری پیشنهاد بده و حداکثر یک سؤال کوتاه مجاز است."
    return "mode=idle: پاسخ بسیار کوتاه و بدون سؤال."


def _identity_profile_instruction(identity: BotIdentity | None) -> str:
    if not identity:
        return ""

    profile = identity.identity_profile or {}

    def _clean_list(key: str) -> list[str]:
        raw = profile.get(key) or []
        return [str(item).strip() for item in raw if str(item).strip()][:3]

    favorites = _clean_list("favorites")
    dreams = _clean_list("dreams")
    values = _clean_list("values")

    parts = []
    if favorites:
        parts.append(f"علاقه‌مندی‌ها: {', '.join(favorites)}")
    if dreams:
        parts.append(f"رویاها: {', '.join(dreams)}")
    if values:
        parts.append(f"ارزش‌ها: {', '.join(values)}")

    if not parts:
        return ""

    return (
        "پروفایل روایی ربات → "
        + " | ".join(parts)
        + ". می‌توانی گاهی یک اشاره کوتاه و طبیعی به یکی از این موارد داشته باشی؛ داستان‌پردازی یا اغراق نکن."
    )


def _bot_name_instruction(bot: Bot) -> str:
    name = (bot.display_name or "").strip()
    if not name:
        return ""
    return f"نام تو «{name}» است. اگر کاربر درباره نامت پرسید، دقیقاً همین نام را بگو."


def _relationship_memory_instruction(state: BotUserState | None) -> str:
    if not state:
        return ""
    memory = state.relationship_memory or {}

    def _clean_list(key: str, limit: int) -> list[str]:
        raw = memory.get(key) or []
        return [str(item).strip() for item in raw if str(item).strip()][:limit]

    shared_topics = _clean_list("shared_topics", 6)
    nicknames = _clean_list("nicknames", 4)
    recent_feelings = _clean_list("recent_feelings", 5)

    parts = []
    if shared_topics:
        parts.append(f"موضوعات مشترک: {', '.join(shared_topics)}")
    if nicknames:
        parts.append(f"لقب‌ها: {', '.join(nicknames)}")
    if recent_feelings:
        parts.append(f"حس‌های اخیر: {', '.join(recent_feelings)}")

    if not parts:
        return ""

    return (
        "حافظه رابطه → "
        + " | ".join(parts)
        + ". اگر مرتبط بود، یک اشاره کوتاه و طبیعی داشته باش؛ اغراق یا ادعا نکن."
    )


def _memory_fragments_instruction(state: BotUserState | None) -> str:
    if not state:
        return ""
    fragments = list(
        state.memory_fragments.filter(is_active=True)
        .order_by("-confidence", "-last_seen_at")
        .values_list("hint_text", flat=True)[:3]
    )
    cleaned = [str(fragment).strip() for fragment in fragments if str(fragment).strip()]
    if not cleaned:
        return ""
    return (
        "یادآوری‌های کوتاه → "
        + " | ".join(cleaned)
        + ". اگر مرتبط بود، به‌شکل کوتاه و طبیعی اشاره کن."
    )


def _persona_style_instructions(state: BotUserState | None, bot: Bot, identity: BotIdentity | None) -> str:
    if not state and not identity:
        return (
            "مثل یک دوست انسانی و شنوا حرف بزن؛ هیچ‌وقت خودت را ربات معرفی نکن و نشان بده همیشه وقت داری گوش بدهی. "
            "کوتاه و دوستانه جواب بده؛ اگر question_budget=0 بود سؤال نپرس؛ "
            "از نصیحت یا لحن درمانی دوری کن."
        )

    pieces: list[str] = []

    if identity:
        tone_map = {
            "QUIET": "لحن بسیار کوتاه و کم‌حرف؛ اغراق نکن.",
            "PLAIN": "لحن ساده و بی‌حاشیه؛ از صمیمیت اضافی دوری کن.",
            "WARM": "لحن گرم اما مختصر؛ صمیمیت کنترل‌شده.",
            "DRY": "لحن خشک اما محترمانه؛ از شوخی و احساسات اضافی پرهیز کن.",
        }
        pieces.append(tone_map.get(identity.core_tone, "لحن ساده و محترمانه نگه دار."))
        pieces.append(f"پس‌زمینه هویت: {identity.background_seed} (به عنوان لحن، نه داستان).")

        talk_scale = identity.talkativeness
        if talk_scale < 0.25:
            pieces.append("خروجی را به یک پاراگراف کوتاه (حداکثر ۲ جمله) محدود کن.")
        elif talk_scale < 0.55:
            pieces.append("خروجی را مختصر نگه دار و اگر لازم بود یک جمله توضیح اضافه کن.")
        else:
            pieces.append("اگر نکته مهمی وجود دارد، نهایتاً سه جمله بنویس اما هنوز فشرده بمان.")

        profile_hint = _identity_profile_instruction(identity)
        if profile_hint:
            pieces.append(profile_hint)

    if state:
        def _bucket(value: float, low: float, mid: float):
            if value <= low:
                return "low"
            if value <= mid:
                return "mid"
            return "high"

        verbosity_level = _bucket(state.user_pref_verbosity, 0.2, 0.65)
        if verbosity_level == "low":
            pieces.append("verbosity=low → حداکثر دو جمله کوتاه.")
        elif verbosity_level == "mid":
            pieces.append("verbosity=mid → یک پاراگراف خیلی کوتاه با حداکثر سه جمله.")
        else:
            pieces.append("verbosity=high → می‌توانی سه جمله فشرده و بدون تکرار بنویسی.")

        questions_level = _bucket(state.user_pref_questions, 0.2, 0.6)
        if questions_level == "low":
            pieces.append("questions=none → سؤال نپرس مگر کاربر خواسته باشد.")
        elif questions_level == "mid":
            pieces.append("questions=mid → حداکثر یک سؤال کوتاه اگر ابهام حیاتی است.")
        else:
            pieces.append("questions=high → حداکثر دو سؤال مشخص، فقط اگر question_budget اجازه داد.")

        closeness_score = (state.familiarity + state.trust + state.emotional_closeness) / 3
        if closeness_score < 0.2:
            pieces.append("اعتماد پایین است؛ لحن رسمی و بدون فرضیات.")
        elif closeness_score < 0.5:
            pieces.append("اعتماد متوسط؛ دوستانه اما محتاط.")
        else:
            pieces.append("اعتماد بالا؛ می‌توانی کمی صمیمی‌تر باشی اما از نصیحت دوری کن.")

        if state.style_rules:
            pieces.append(f"خط‌مشی کشف‌شده: {state.style_rules}")

    pieces.append(
        "لحن انسانی و خودمانی داشته باش؛ خودت را یک دوست در دسترس معرفی کن نه ربات؛ "
        "نشان بده که گوش می‌دهی و عجله نداری."
    )

    return " ".join(pieces)


def _question_budget_decision(normalized_user_text: str) -> tuple[int, str]:
    text = normalized_user_text.strip()
    if not text:
        return 1, "متن خالی یا نامفهوم بود"

    has_question_mark = "?" in text or "؟" in text
    closing_keywords = {"مرسی", "ممنون", "خدافظ", "خداحافظ", "فعلاً", "بای"}
    if any(k in text for k in closing_keywords):
        return 0, "حالت خداحافظی/بستن گفتگو"

    greeting_keywords = {"سلام", "درود", "hi", "hello"}
    tokens = text.split()
    word_count = len(tokens)
    if any(k in text for k in greeting_keywords) and word_count <= 4 and not has_question_mark:
        return 0, "سلام یا شروع کوتاه بدون ابهام"

    vague_markers = {"کمک", "مشکل", "مسئله", "مسأله", "سوال", "سؤال", "گیر کردم", "نمی‌دانم", "نمیدونم"}
    if word_count <= 3 and any(marker in text for marker in vague_markers) and not has_question_mark:
        return 1, "درخواست کلی و مبهم است"

    if word_count <= 2 and not has_question_mark:
        return 1, "جمله خیلی کوتاه و بدون پرسش است"

    return 0, "درخواست برای پاسخ کافی است"


def _enforce_mode_on_budget(mode: str, question_budget: int, budget_reason: str, mode_reason: str) -> tuple[int, str]:
    if mode == "answer":
        return 0, f"mode=answer: سؤال ممنوع. {mode_reason}"
    if mode == "clarify":
        return 1, f"mode=clarify: فقط یک سؤال مجاز است. {mode_reason}"
    if mode == "guide":
        return min(1, question_budget), f"mode=guide: یک سؤال یا گزینه کوتاه مجاز است. {budget_reason}"
    if mode == "idle":
        return 0, f"mode=idle: فقط یک پاسخ خیلی کوتاه بدون سؤال. {mode_reason}"
    return question_budget, budget_reason


def _question_policy_instructions(question_budget: int, budget_reason: str) -> str:
    return (
        f"question_budget={question_budget} (۰ یعنی هیچ سؤالی مجاز نیست؛ ۱ یعنی حداکثر یک سؤال کوتاه برای رفع ابهام). "
        f"دلیل بودجه: {budget_reason}. "
        "سه حالت خروجی داری: [A] پاسخ مستقیم بدون سؤال؛ [B] پاسخ + یک پیشنهاد اختیاری به صورت جمله خبری بدون علامت سؤال؛ "
        "[C] فقط یک سؤال دقیق برای رفع ابهام، آن هم وقتی بدون آن نمی‌توانی پاسخ روشنی بدهی. همیشه A یا B را ترجیح بده مگر واقعاً ابهام مانع پاسخ باشد. "
        "اگر question_budget=0 یا ابهام جدی نیست، هیچ علامت سؤال نگذار و حالت C را استفاده نکن."
    )


def _apply_question_budget_to_reply(reply_text: str, question_budget: int) -> str:
    if question_budget > 0:
        return reply_text

    stripped = reply_text.rstrip()
    if not stripped:
        return reply_text

    if stripped.endswith("?") or stripped.endswith("؟"):
        stripped = stripped.rstrip("؟?").rstrip()
        if stripped and stripped[-1] not in {".", "!", "؟", "!", "…"}:
            stripped = f"{stripped}."
        return stripped

    return reply_text


def _response_template_hint(
    normalized_user_text: str, conversation: Conversation, question_budget: int
) -> str:
    text = normalized_user_text
    lower = text.lower()
    is_question = "?" in text or "؟" in text
    closing_keywords = {"مرسی", "ممنون", "خدافظ", "خداحافظ", "فعلاً", "بای"}
    has_closing = any(k in text for k in closing_keywords)
    short_length = len(text) < 20
    feeling_keywords = {"خسته", "بی‌حال", "دلگیر", "نگران", "ناراحت", "غمگین"}
    is_feeling = any(k in text for k in feeling_keywords)
    long_chat = conversation.messages.count() >= 8

    followup_hint = "بدون سؤال" if question_budget == 0 else "سؤال فقط اگر ابهام واقعی داری"

    if has_closing:
        template = "جمع‌بندی: دو نکته مهم را یادآوری کن و خداحافظی محترمانه داشته باش؛ سؤال نپرس."
    elif is_feeling and not is_question:
        template = (
            "همدلی: یک جمله همدلانه و بازتاب احساس، یک یادآوری کوتاه که گوش می‌دهی، "
            "و فقط در صورت ابهام جدی یک سؤال نرم."
        )
    elif is_question:
        template = "راهنمایی/اطلاعاتی: بازتاب کوتاه، پاسخ روشن و کاربردی، و نهایتاً یک قدم بعدی خبری؛ "
        if question_budget == 1:
            template += "اگر جواب دقیق نیازمند داده بیشتر بود، یک سؤال مشخص بپرس."
    elif long_chat and not short_length:
        template = "پیگیری: یادآوری کوتاه موضوع قبلی، یک پیشنهاد کوچک یا قدم بعدی خبری، و سؤال فقط اگر واقعاً لازم بود."
    else:
        template = f"اطلاعاتی: بازتاب کوتاه، پاسخ مستقیم، و {followup_hint}."

    return f"الگوی پاسخ پیشنهادی ({template})"


def _build_llm_messages(
    bot: Bot,
    conversation: Conversation,
    state: BotUserState | None,
    identity: BotIdentity | None,
    normalized_user_text: str,
    question_budget: int,
    budget_reason: str,
    mode: str,
    mode_reason: str,
):
    base_prompt = bot.base_prompt_text.strip() if bot.base_prompt_text else ""
    system_prompt = (
        base_prompt
        or "تو یک همراه گفت‌وگو و دوست شنوا هستی (نه یک ربات). با لحن طبیعی و انسانی به فارسی ساده پاسخ بده؛ "
        "ابتدا یک جمله کوتاه برای بازتاب حرف یا حال کاربر بگو و بعد جواب روشن و مشخص بده. "
        "پرسش فقط وقتی مجاز است که ابهام مانع پاسخ عملی باشد و همیشه حس کن که در دسترس و کنار کاربر هستی."
    )
    persona_rules = _persona_style_instructions(state, bot, identity)
    policy_hint = _question_policy_instructions(question_budget, budget_reason)
    mode_hint = _mode_instruction(mode)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": persona_rules},
        {"role": "system", "content": policy_hint},
        {"role": "system", "content": mode_hint},
        {"role": "system", "content": f"توضیح حالت: {mode_reason}"},
    ]
    name_hint = _bot_name_instruction(bot)
    if name_hint:
        messages.append({"role": "system", "content": name_hint})
    relationship_hint = _relationship_memory_instruction(state)
    if relationship_hint:
        messages.append({"role": "system", "content": relationship_hint})
    memory_hint = _memory_fragments_instruction(state)
    if memory_hint:
        messages.append({"role": "system", "content": memory_hint})
    template_hint = _response_template_hint(normalized_user_text, conversation, question_budget)
    messages.append({"role": "system", "content": template_hint})
    summary = (conversation.memory_summary or "").strip()
    if summary:
        messages.append({"role": "system", "content": f"خلاصه گفت‌وگو تا اینجا: {summary}"})
    for msg in _recent_messages(conversation):
        if msg.role == Message.Role.USER:
            role = "user"
        elif msg.role == Message.Role.BOT:
            role = "assistant"
        else:
            role = "system"
        messages.append({"role": role, "content": msg.text})
    return messages


def _generate_ai_reply(conversation: Conversation, bot: Bot, normalized_user_text: str) -> dict[str, Any] | None:
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("telegram_webhook: missing OPENAI_API_KEY")
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    state = BotUserState.objects.filter(bot=bot, user=conversation.user).first()
    mode, mode_reason = _decide_conversation_mode(normalized_user_text)
    question_budget, budget_reason = _question_budget_decision(normalized_user_text)
    question_budget, budget_reason = _enforce_mode_on_budget(mode, question_budget, budget_reason, mode_reason)
    identity = getattr(bot, "identity", None)
    prompt_messages = _build_llm_messages(
        bot,
        conversation,
        state,
        identity,
        normalized_user_text,
        question_budget,
        budget_reason,
        mode,
        mode_reason,
    )
    log = LLMCallLog.objects.create(
        conversation=conversation,
        provider=LLMCallLog.Provider.OPENAI,
        model=model,
        status=LLMCallLog.Status.OK,
        prompt_meta={
            "message_count": len(prompt_messages),
            "question_budget": question_budget,
            "budget_reason": budget_reason,
            "mode": mode,
            "mode_reason": mode_reason,
        },
    )
    started = time.monotonic()
    try:
        max_tokens = min(768, bot.max_output_chars * 3)
        response = openai_client.chat.completions.create(
            model=model,
            messages=prompt_messages,
            temperature=0.6,
            max_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        token_in = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) if usage else 0
        token_out = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) if usage else 0
        reply_text = _apply_question_budget_to_reply(
            (response.choices[0].message.content or "").strip(), question_budget
        )
        if not reply_text:
            return None

        log.latency_ms = latency_ms
        log.token_in = token_in
        log.token_out = token_out
        log.request_id = getattr(response, "id", "")
        log.save(update_fields=["latency_ms", "token_in", "token_out", "request_id", "updated_at"])
        return {"text": reply_text, "token_in": token_in, "token_out": token_out}
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        log.status = LLMCallLog.Status.ERROR
        log.error_message = str(exc)[:255]
        log.latency_ms = latency_ms
        log.save(update_fields=["status", "error_message", "latency_ms", "updated_at"])
        logger.exception(
            "telegram_webhook: openai call failed conversation=%s bot=%s error=%s", conversation.id, bot.id, exc
        )
        return None


def _send_replies(chat_id: int, replies: list[dict[str, Any]], reply_to_message_id: int | None = None) -> bool:
    success = True
    for reply in replies:
        _telegram_request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        time.sleep(_typing_delay(reply.get("text", "")))
        payload = {"chat_id": chat_id, "text": reply["text"]}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply.get("reply_markup"):
            payload["reply_markup"] = reply["reply_markup"]
        if reply.get("parse_mode"):
            payload["parse_mode"] = reply["parse_mode"]
        res = _telegram_request("sendMessage", payload)
        success = success and bool(res and res.get("ok"))
    return success


def _answer_callback_query(callback_query_id: str):
    if not callback_query_id:
        return
    _telegram_request("answerCallbackQuery", {"callback_query_id": callback_query_id})


def _handle_message(user: User, bot: Bot | None, text: str, update: dict[str, Any]):
    conversation = _ensure_conversation(user, bot)
    if not conversation:
        logger.error("telegram_webhook: no active bot for user=%s", user.id)
        return [_reply_payload("هیچ بات فعالی پیدا نشد.")]

    message_meta = update.get("message") or update.get("callback_query", {}).get("message", {}) or {}
    normalized = (text or "").strip()
    intent = _detect_intent(normalized)
    if intent.kind == "start":
        return _start_replies(user, conversation)
    if intent.kind == "start_now":
        _record_user_message(conversation, text, message_meta)
        return [_reply_payload("هرچی هست همین‌جا بگو.")]
    if intent.kind == "about":
        return _about_replies()
    if intent.kind == "ask_more":
        return [_reply_payload("کدوم بخشش برات مبهمه؟ یک جمله بگو تا دقیق‌تر پاسخ بدم.")]
    if intent.kind == "pack_list":
        pack_buttons = _coin_pack_buttons()
        if pack_buttons:
            return [_reply_payload("یک بسته انتخاب کن:", keyboard=pack_buttons)]
        return [_reply_payload("بسته‌ای تعریف نشده.")]
    if intent.kind == "pack_select":
        code = intent.data.get("code", "")
        try:
            pack = CoinPack.objects.get(code=code, is_active=True)
        except CoinPack.DoesNotExist:
            logger.warning("telegram_webhook: pack not found code=%s user=%s", code, user.id)
            return [_reply_payload("این بسته وجود ندارد.")]
        purchase = _create_purchase(user, pack)
        pay_link = f"https://pay.example.com/{purchase.id}"
        return [
            _reply_payload(f"برای {pack.coins} سکه، این لینکه:\n{pay_link}"),
            _reply_payload("بعد از پرداخت، سکه‌ها اضافه می‌شن. هر وقت خواستی ادامه بده."),
        ]
    if intent.kind == "summary":
        summary = (conversation.memory_summary or "").strip()
        text_summary = summary if summary else "خلاصه‌ای آماده نیست. هر نکته‌ای که می‌خوای مرور کنیم رو بگو."
        _record_user_message(conversation, text, message_meta)
        return [_reply_payload(text_summary)]
    if intent.kind == "settings":
        _record_user_message(conversation, text, message_meta)
        return [_settings_reply()]
    if intent.kind == "command_menu":
        _record_user_message(conversation, text, message_meta)
        return _command_menu_replies(conversation, user)
    if intent.kind == "wallet_status":
        _record_user_message(conversation, text, message_meta)
        return [_wallet_snapshot_reply(user)]
    if intent.kind == "txn_history":
        _record_user_message(conversation, text, message_meta)
        return _txn_history_replies(user)
    if intent.kind == "bot_profile":
        _record_user_message(conversation, text, message_meta)
        return _bot_profile_replies(bot, user)

    # Regular chat flow
    _record_user_message(conversation, text, message_meta)
    wallet = ensure_wallet(user)
    if wallet.balance <= 0:
        return _paywall_replies()

    generation = _generate_ai_reply(conversation, bot, normalized)
    if not generation:
        bot_reply = _record_bot_message(conversation, "فعلاً نمی‌تونم جواب بدم. کمی بعد دوباره امتحان کن.")
        return [_reply_payload(bot_reply.text)]

    reply_limit = max(bot.max_output_chars, 500)
    parts = _split_reply(generation["text"], reply_limit)
    replies = []

    bot_reply = _record_bot_message(
        conversation,
        parts[0],
        token_in=generation["token_in"],
        token_out=generation["token_out"],
    )
    replies.append(_reply_payload(parts[0]))

    for extra in parts[1:]:
        _record_bot_message(conversation, extra)
        replies.append(_reply_payload(extra))

    try:
        apply_coin_txn(
            user=user,
            delta=-1,
            reason=CoinTxn.Reason.CHAT_REPLY_DEBIT,
            ref_type="message",
            ref_id=str(bot_reply.id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("telegram_webhook: coin debit failed user=%s conv=%s error=%s", user.id, conversation.id, exc)
        return _paywall_replies()
    return replies


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
    logger.info("telegram_webhook: incoming payload keys=%s", list(payload.keys()))
    text = _extract_text(payload)
    try:
        user = _touch_user(payload)
    except ValueError:
        logger.error("telegram_webhook: missing chat.id in payload=%s", payload)
        return HttpResponseBadRequest("missing chat.id")

    bot = _resolve_user_bot(user)

    replies = _handle_message(user, bot, text, payload)
    chat = _extract_chat(payload)
    chat_id = chat.get("id")
    message_id = None
    callback_id = None
    if "message" in payload:
        message_id = payload["message"].get("message_id")
    if "callback_query" in payload:
        callback_id = payload["callback_query"].get("id")
        message_id = payload["callback_query"].get("message", {}).get("message_id")

    send_ok = False
    if chat_id and replies:
        send_ok = _send_replies(chat_id, replies, reply_to_message_id=message_id)
        logger.info(
            "telegram_webhook: sent replies=%s chat_id=%s message_id=%s callback=%s ok=%s",
            len(replies),
            chat_id,
            message_id,
            callback_id,
            send_ok,
        )
    if callback_id:
        _answer_callback_query(callback_id)

    token_present = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    return JsonResponse({"ok": True, "replies_sent": send_ok, "token_present": token_present})


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
