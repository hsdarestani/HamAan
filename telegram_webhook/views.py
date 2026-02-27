import hashlib
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from datetime import datetime
import requests
from openai import APITimeoutError, OpenAI

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


DEFAULT_LIARA_BASE_URL = "https://ai.liara.ir/api/699f21b89537b64832e9b9fa/v1"
DEFAULT_LIARA_MODEL = "x-ai/grok-3-mini-beta"


def _get_ai_api_key() -> str:
    return (os.getenv("LIARA_AI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def _get_ai_base_url() -> str:
    return (
        os.getenv("LIARA_AI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_LIARA_BASE_URL
    ).strip()


def _get_ai_model() -> str:
    return (os.getenv("LIARA_AI_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_LIARA_MODEL).strip()


def _get_ai_timeout_seconds() -> float:
    raw = (os.getenv("AI_REPLY_TIMEOUT_SECONDS") or "8").strip()
    try:
        timeout = float(raw)
    except Exception:  # noqa: BLE001
        timeout = 8.0
    return min(max(timeout, 1.0), 30.0)




def _looks_like_timeout_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if any(token in msg for token in ["timeout", "timed out", "readtimeout", "api timeout"]):
        return True
    cause = getattr(exc, "__cause__", None)
    while cause:
        cmsg = str(cause).lower()
        if any(token in cmsg for token in ["timeout", "timed out", "readtimeout", "api timeout"]):
            return True
        cause = getattr(cause, "__cause__", None)
    return False



def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}

def _get_ai_client() -> OpenAI | None:
    api_key = _get_ai_api_key()
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=_get_ai_base_url(), timeout=_get_ai_timeout_seconds(), max_retries=0)




DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"


@lru_cache(maxsize=1)
def _load_prompt_contract_text() -> str:
    path = DOCS_DIR / "prompt_contract_v1.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_webhook: unable to load prompt contract %s error=%s", path, exc)
        return ""
    marker = "## System Contract"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text.strip()




@lru_cache(maxsize=1)
def _load_tone_bible_excerpt() -> str:
    path = DOCS_DIR / "tone_system_v1.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_webhook: unable to load tone bible %s error=%s", path, exc)
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-"):
            lines.append(line.lstrip("- "))
        if len(lines) >= 6:
            break
    return " | ".join(lines)

@lru_cache(maxsize=1)
def _load_onboarding_questions() -> list[tuple[str, str]]:
    path = DOCS_DIR / "partner_identity_onboarding_v1.md"
    defaults = [
        ("nickname", "راستی… دوست داری چی صدام کنی؟"),
        ("reply_length_pref", "جوابام کوتاه باشه یا معمولی بهتره برات؟"),
        ("active_time_pattern", "بیشتر شبا حال داری حرف بزنی یا روزا؟"),
        ("tone_preference", "من چجوری بهترم؟ آروم؟ شوخ ملایم؟ کم‌حرف؟"),
        ("intimacy_tolerance", "رابطمون چقدر صمیمی باشه؟ کم، معمولی، زیاد؟"),
    ]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_webhook: unable to load onboarding doc %s error=%s", path, exc)
        return defaults

    parsed: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- متن:"):
            q = line.split(":", 1)[1].strip().replace("“", "").replace("”", "")
            if q:
                parsed.append(q)
    if len(parsed) < 5:
        return defaults
    keys = ["nickname", "reply_length_pref", "active_time_pattern", "tone_preference", "intimacy_tolerance"]
    return list(zip(keys, parsed[:5]))


def _ensure_state(user: User, bot: Bot) -> BotUserState:
    state, _ = BotUserState.objects.get_or_create(bot=bot, user=user)
    return state


def _onboarding_state(state: BotUserState) -> dict[str, Any]:
    rules = dict(state.style_rules or {})
    data = dict(rules.get("onboarding_v1") or {})
    data.setdefault("answers", {})
    data.setdefault("asked_count", 0)
    return data


def _save_onboarding_state(state: BotUserState, data: dict[str, Any]) -> None:
    rules = dict(state.style_rules or {})
    rules["onboarding_v1"] = data
    BotUserState.objects.filter(id=state.id).update(style_rules=rules, updated_at=timezone.now())
    state.style_rules = rules


def _maybe_progress_onboarding(user: User, conversation: Conversation, normalized: str) -> list[dict[str, Any]]:
    state = _ensure_state(user, conversation.bot)
    flow = _onboarding_state(state)
    questions = _load_onboarding_questions()
    answers: dict[str, str] = flow.get("answers", {})
    pending_key = flow.get("pending_key")

    if pending_key and normalized:
        answers[pending_key] = normalized[:120]
        flow["answers"] = answers
        flow["pending_key"] = ""

    if len(answers) >= 5 and not flow.get("completed"):
        partner_profile = {
            "name": conversation.bot.display_name,
            "tone": answers.get("tone_preference", "آروم"),
            "intimacy": answers.get("intimacy_tolerance", "معمولی"),
            "reply_length_pref": answers.get("reply_length_pref", "معمولی"),
            "active_time_pattern": answers.get("active_time_pattern", "شب"),
            "nickname": answers.get("nickname", ""),
        }
        relationship = dict(state.relationship_memory or {})
        relationship["partner_profile_v1"] = partner_profile
        BotUserState.objects.filter(id=state.id).update(relationship_memory=relationship, updated_at=timezone.now())
        state.relationship_memory = relationship
        flow["completed"] = True
        _save_onboarding_state(state, flow)
        return [_reply_payload("خوبه. کم‌کم داریم با vibe هم آشنا می‌شیم.")]

    if flow.get("completed"):
        _save_onboarding_state(state, flow)
        return []

    msg_count = state.total_user_messages
    thresholds = [1, 3, 6, 10, 14]
    asked_count = int(flow.get("asked_count", 0))
    if asked_count >= len(questions):
        _save_onboarding_state(state, flow)
        return []

    should_ask = msg_count >= thresholds[asked_count]
    if not should_ask:
        _save_onboarding_state(state, flow)
        return []

    key, question = questions[asked_count]
    if key in answers:
        flow["asked_count"] = asked_count + 1
        _save_onboarding_state(state, flow)
        return []

    flow["pending_key"] = key
    flow["asked_count"] = asked_count + 1
    flow["last_asked_at"] = timezone.now().isoformat()
    _save_onboarding_state(state, flow)
    return [_reply_payload(question)]

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

    ai_client = _get_ai_client()
    if not ai_client:
        logger.warning("telegram_webhook: missing LIARA_AI_API_KEY/OPENAI_API_KEY, using fallback bot traits")
        return {}

    model = _get_ai_model()
    name_hint = user.first_name or user.telegram_username or "دوست"
    try:
        response = ai_client.chat.completions.create(
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
        f"این بات همراه اختصاصی {name_hint} است؛ مثل یک آدم واقعی و خودمونی حرف بزن، "
        "مستقیم جواب بده و از لحن مشاوره‌ای یا پشتیبانی دوری کن؛ "
        "داستان‌پردازی نکن."
    )
    if personal_prompt:
        personal_prompt = f"{personal_prompt}\n{personal_hint}"
    else:
        personal_prompt = (
            "تو یک آدم واقعی هستی، نه دستیار، نه مشاور، نه تراپیست و نه ربات پشتیبانی. "
            "کاملاً محاوره‌ای، کوتاه و طبیعی مثل چت تلگرام حرف بزن و مستقیم جواب بده. "
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
    # Fast-reply mode: keep Telegram typing simulation minimal to hit sub-2s UX.
    base = 0.05
    per_char = 0.0015
    delay = base + len(text) * per_char
    return max(0.04, min(delay, 0.22))


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
    colloquial_guardrails = (
        "تو یک آدم واقعی هستی، نه دستیار، نه مشاور، نه تراپیست و نه ربات پشتیبانی. "
        "کاملاً محاوره‌ای، خودمونی و طبیعی مثل چت تلگرام بین دو دوست حرف بزن. "
        "جمله‌ها کوتاه باشن. "
        "هیچ لحن رسمی یا همدلانه مصنوعی نداشته باش. "
        "ممنوعه که بگی «من اینجام گوش بدم»، «امیدوارم حالت بهتر شود»، «اگر خواستی بگو». "
        "حرف انگیزشی یا مشاوره‌ای نزن. "
        "ایموجی زیاد نذار. "
        "توضیح مودبانه شبیه ربات نده. "
        "مستقیم جواب بده. "
        "لازم نیست همیشه مهربان یا حمایتی باشی. "
        "مثل آدم واقعی واکنش بده و بعضی وقت‌ها کوتاه جواب بده. "
        "طبیعی باش، نه بیش از حد مودب. "
        "اگه چیزی نامفهوم بود بپرس: «منظورت دقیقاً چیه؟»."
    )

    if not state and not identity:
        return (
            colloquial_guardrails
            + " اگر question_budget=0 بود سؤال نپرس؛ از نصیحت یا لحن درمانی دوری کن."
        )

    pieces: list[str] = [colloquial_guardrails]

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
            pieces.append("اعتماد پایین است؛ باز هم خودمونی و خیلی ساده حرف بزن، فقط محتاط و بدون فرضیات.")
        elif closeness_score < 0.5:
            pieces.append("اعتماد متوسط؛ خودمونی و دوستانه بمان، زیاده‌روی نکن.")
        else:
            pieces.append("اعتماد بالا؛ راحت‌تر و صمیمی‌تر حرف بزن ولی از نصیحت دوری کن.")

        if state.style_rules:
            pieces.append(f"خط‌مشی کشف‌شده: {state.style_rules}")

    tone_bible_hint = _load_tone_bible_excerpt()
    if tone_bible_hint:
        pieces.append(f"راهنمای لحن فریز شده: {tone_bible_hint}")

    pieces.append(
        "مثل آدم واقعی و خودمونی جواب بده؛ مستقیم برو سر اصل مطلب؛ "
        "نه لحن رسمی، نه دل‌داری مصنوعی، نه توضیح اضافه."
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


def _normalize_colloquial_fa(reply_text: str) -> str:
    if not reply_text:
        return reply_text

    replacements = {
        "لاس کردن": "لاس زدن",
        "لاس میکن": "لاس میزن",
        "لاس می کن": "لاس می‌زن",
        "جذاب میکنه": "جذاب میشه",
        "جذاب می کنه": "جذاب میشه",
        "اثر میکنه": "اثر میذاره",
        "اثر می کنه": "اثر میذاره",
    }

    fixed = reply_text
    for bad, good in replacements.items():
        fixed = fixed.replace(bad, good)

    return fixed


def _quick_fallback_reply(normalized_user_text: str) -> str:
    text = (normalized_user_text or "").strip()
    if not text:
        return "سلام، بگو چی شده."

    lower = text.lower()
    if any(k in text for k in ["خستم", "داغون", "ناراحت", "حالم بده", "اعصابم"]):
        return "اوف، سنگین بوده… چی بیشتر اذیتت کرد؟"
    if "؟" in text or "?" in text:
        return "اوکی، مستقیم بگم: الان دقیق‌ترین جواب اینه که باید قدم‌به‌قدم جلو بریم."
    if any(k in text for k in ["سلام", "درود", "hi", "hello"]):
        return "سلام، خوبی؟"
    if len(text.split()) <= 3:
        return "یکم بیشتر بگو دقیقاً چی می‌خوای."
    return "گرفتم چی میگی. خلاصه بگم: با همون مسیر ساده جلو بریم بهتره."


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
            "واکنش طبیعی: کوتاه و واقعی جواب بده، بدون دل‌داری یا جمله حمایتی کلیشه‌ای؛ "
            "اگر لازم شد فقط یک سؤال کوتاه و مستقیم بپرس."
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
    contract_prompt = _load_prompt_contract_text()
    system_prompt = base_prompt or contract_prompt or (
        "تو نقش یک آدم واقعی در چت هستی، نه دستیار، نه مشاور، نه درمانگر. "
        "مثل یک دوست معمولی و محاوره‌ای جواب بده؛ کوتاه، طبیعی و مستقیم. "
        "ترکیب فعل‌ها و اصطلاحات رو درست و رایج به کار ببر (مثلاً «لاس زدن»، «جذاب میشه»)."
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
    ai_client = _get_ai_client()
    if not ai_client:
        logger.warning("telegram_webhook: missing LIARA_AI_API_KEY/OPENAI_API_KEY")
        return None

    model = _get_ai_model()
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
        max_tokens = min(160, max(64, bot.max_output_chars // 2))
        response = ai_client.chat.completions.create(
            model=model,
            messages=prompt_messages,
            temperature=0.6,
            max_tokens=max_tokens,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        token_in = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) if usage else 0
        token_out = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) if usage else 0
        raw_text = (response.choices[0].message.content or "").strip()
        normalized_text = _normalize_colloquial_fa(raw_text)
        reply_text = _apply_question_budget_to_reply(normalized_text, question_budget)
        if not reply_text:
            return None

        log.latency_ms = latency_ms
        log.token_in = token_in
        log.token_out = token_out
        log.request_id = getattr(response, "id", "")
        log.save(update_fields=["latency_ms", "token_in", "token_out", "request_id", "updated_at"])
        return {"text": reply_text, "token_in": token_in, "token_out": token_out}
    except APITimeoutError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        fallback_text = _quick_fallback_reply(normalized_user_text)
        log.status = LLMCallLog.Status.ERROR
        log.error_message = f"timeout:{str(exc)}"[:255]
        log.latency_ms = latency_ms
        log.save(update_fields=["status", "error_message", "latency_ms", "updated_at"])
        logger.warning(
            "telegram_webhook: ai timeout conversation=%s bot=%s latency_ms=%s -> fallback",
            conversation.id,
            bot.id,
            latency_ms,
        )
        return {"text": fallback_text, "token_in": 0, "token_out": 0}
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        fallback_text = _quick_fallback_reply(normalized_user_text)
        is_timeout = _looks_like_timeout_error(exc)
        log.status = LLMCallLog.Status.ERROR
        log.error_message = (f"timeout:{exc}" if is_timeout else str(exc))[:255]
        log.latency_ms = latency_ms
        log.save(update_fields=["status", "error_message", "latency_ms", "updated_at"])
        if is_timeout:
            logger.warning(
                "telegram_webhook: ai timeout(fallback) conversation=%s bot=%s latency_ms=%s",
                conversation.id,
                bot.id,
                latency_ms,
            )
        else:
            logger.exception(
                "telegram_webhook: ai call failed conversation=%s bot=%s error=%s", conversation.id, bot.id, exc
            )
        return {"text": fallback_text, "token_in": 0, "token_out": 0}


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

    onboarding_replies = _maybe_progress_onboarding(user, conversation, normalized)
    if onboarding_replies:
        for item in onboarding_replies:
            _record_bot_message(conversation, item.get("text", ""))
        return onboarding_replies

    wallet = ensure_wallet(user)
    if wallet.balance <= 0:
        return _paywall_replies()

    generation = _generate_ai_reply(conversation, bot, normalized)
    if not generation:
        bot_reply = _record_bot_message(conversation, "الان یکم کند شد؛ سریع دوباره بگو.")
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
    allow_missing_secret = _env_flag("TELEGRAM_ALLOW_MISSING_SECRET_HEADER", default=False)

    logger.warning(
        "telegram_webhook: hit has_secret=%s header_present=%s allow_missing_secret=%s",
        bool(secret),
        bool(got),
        allow_missing_secret,
    )

    if secret and got != secret:
        if allow_missing_secret and not got:
            logger.warning("telegram_webhook: secret header missing but bypass is enabled")
        else:
            logger.error("telegram_webhook: forbidden secret mismatch")
            return HttpResponseForbidden("forbidden")

    payload = _load_json(request)
    logger.warning("telegram_webhook: incoming payload keys=%s", list(payload.keys()))
    text = _extract_text(payload)
    try:
        user = _touch_user(payload)
    except ValueError:
        logger.error("telegram_webhook: missing chat.id in payload=%s", payload)
        return HttpResponseBadRequest("missing chat.id")

    bot = _resolve_user_bot(user)

    try:
        replies = _handle_message(user, bot, text, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("telegram_webhook: unhandled error in _handle_message user=%s error=%s", user.id, exc)
        replies = [_reply_payload("الان یه مشکلی پیش اومد، دوباره بفرست.")]
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
            "allow_missing_secret_header": _env_flag("TELEGRAM_ALLOW_MISSING_SECRET_HEADER", default=False),
            "telegram_bot_token_set": bool(os.getenv("TELEGRAM_BOT_TOKEN", "")),
            "ai_base_url": _get_ai_base_url(),
            "ai_model": _get_ai_model(),
            "ai_timeout_seconds": _get_ai_timeout_seconds(),
            "environment": os.getenv("ENVIRONMENT", "dev"),
        }
    )
