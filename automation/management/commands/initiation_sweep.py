from __future__ import annotations

import logging
import os
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from automation.models import InitiationEvent, InitiationRule, ScheduledJob
from chat.models import Conversation, Message, next_message_seq
from persona.models import BotIdentity, BotUserState
from safety.models import UserRestriction
from users.models import User, UserPrefs

logger = logging.getLogger(__name__)


RECENT_USER_GRACE_MINUTES = 20
MAX_ELIGIBLE_BATCH = 250
MAX_SEND_BATCH = 100


@dataclass
class EligibilityContext:
    rule: InitiationRule
    prefs: UserPrefs
    identity: BotIdentity
    restriction: UserRestriction | None = None


def _user_timezone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _is_within_quiet_hours(now: datetime, prefs: UserPrefs, user_tz: ZoneInfo) -> bool:
    if not prefs.quiet_hours_enabled or not prefs.quiet_hours_start or not prefs.quiet_hours_end:
        return False
    local_now = now.astimezone(user_tz)
    start_dt = datetime.combine(local_now.date(), prefs.quiet_hours_start, tzinfo=user_tz)
    end_dt = datetime.combine(local_now.date(), prefs.quiet_hours_end, tzinfo=user_tz)
    if start_dt <= end_dt:
        return start_dt <= local_now <= end_dt
    return local_now >= start_dt or local_now <= end_dt


def _recent_user_activity(state: BotUserState, now: datetime) -> bool:
    if not state.last_user_message_at:
        return False
    return (now - state.last_user_message_at) < timedelta(minutes=RECENT_USER_GRACE_MINUTES)


def _has_reached_limits(state: BotUserState, ctx: EligibilityContext, now: datetime) -> bool:
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    day_count = InitiationEvent.objects.filter(
        state=state,
        created_at__gte=day_ago,
        status__in=[InitiationEvent.Status.PLANNED, InitiationEvent.Status.SENT, InitiationEvent.Status.ACKED],
    ).count()
    if day_count >= ctx.rule.max_per_day:
        return True

    week_count = InitiationEvent.objects.filter(
        state=state,
        created_at__gte=week_ago,
        status__in=[InitiationEvent.Status.PLANNED, InitiationEvent.Status.SENT, InitiationEvent.Status.ACKED],
    ).count()
    if week_count >= ctx.rule.max_per_week:
        return True

    cooldown_hours = max(ctx.rule.cooldown_hours, ctx.prefs.initiation_cooldown_hours)
    if state.last_initiation_at and (now - state.last_initiation_at) < timedelta(hours=cooldown_hours):
        return True
    return False


def _allowed_hour(rule: InitiationRule, now: datetime) -> bool:
    hour = now.hour
    if rule.allowed_start_hour <= rule.allowed_end_hour:
        return rule.allowed_start_hour <= hour < rule.allowed_end_hour
    return hour >= rule.allowed_start_hour or hour < rule.allowed_end_hour


def _should_initiate(state: BotUserState, ctx: EligibilityContext, now: datetime) -> bool:
    if not ctx.rule.enabled:
        return False
    if not state.initiation_opt_in:
        return False
    if not state.user.initiation_opt_in or not ctx.prefs.prefers_initiation:
        return False
    if state.user.is_blocked:
        return False
    if ctx.restriction and (ctx.restriction.block_initiation or ctx.restriction.level == UserRestriction.Level.BLOCKED):
        return False
    if state.familiarity < ctx.rule.min_familiarity or state.trust < ctx.rule.min_trust:
        return False
    if _recent_user_activity(state, now):
        return False
    if _has_reached_limits(state, ctx, now):
        return False
    if not _allowed_hour(ctx.rule, now):
        return False
    user_tz = _user_timezone(state.user)
    if _is_within_quiet_hours(now, ctx.prefs, user_tz):
        return False
    return True


def _pick_theme(identity: BotIdentity, state: BotUserState) -> str:
    relationship = state.relationship_memory or {}
    identity_profile = identity.identity_profile or {}

    shared_topics = relationship.get("shared_topics") or []
    nicknames = relationship.get("nicknames") or []
    recent_feelings = relationship.get("recent_feelings") or []
    favorites = identity_profile.get("favorites") or []
    dreams = identity_profile.get("dreams") or []
    values = identity_profile.get("values") or []

    choices = []
    for pool in (shared_topics, nicknames, recent_feelings, favorites, dreams, values):
        if pool:
            choices.append(random.choice(pool))
    if choices:
        return random.choice(choices)
    return ""


def _tone_prefix(identity: BotIdentity) -> str:
    prefixes = {
        "QUIET": ["یه سلام کوتاه.", "یه نگاهی انداختم.", "فقط یه سلام."],
        "PLAIN": ["سلام.", "یه پیام کوتاه دادم.", "گفتم خبر بگیرم."],
        "WARM": ["هی.", "یه سلام گرم.", "دل تنگ شدم."],
        "DRY": ["سلام.", "یه پیام سریع.", "فقط یادآوری."],
    }
    return random.choice(prefixes.get(identity.core_tone, ["سلام."]))


def _build_message(state: BotUserState, ctx: EligibilityContext) -> str:
    theme = _pick_theme(ctx.identity, state)
    base = _tone_prefix(ctx.identity)

    fragments = list(
        state.memory_fragments.filter(is_active=True)
        .order_by("-confidence", "-last_seen_at")
        .values_list("hint_text", flat=True)[:3]
    )
    fragment_text = random.choice(fragments) if fragments else ""

    lines = []
    if fragment_text:
        lines.append(f"{base} حواسم به {fragment_text} بود.")
    elif theme:
        lines.append(f"{base} یاد {theme} افتادم.")
    else:
        lines.append(f"{base} اومدم یه سر بزنم.")

    if ctx.rule.allow_question:
        followups = [
            "گفتم یه احوالی بگیرم، چطوری؟",
            "یه سر زدم ببینم روزت چطور گذشت؟",
            "خلاصه بگو امروز چه خبر بود؟",
        ]
    else:
        followups = [
            "فقط گفتم یه سلام بدم.",
            "لازم نیست جواب بدی، فقط خواستم یادم باشی.",
            "یه سر زدم، همین.",
        ]
    lines.append(random.choice(followups))

    message = " ".join(lines).strip()
    return message[: ctx.rule.max_chars]


def _send_telegram_message(user: User, text: str) -> tuple[bool, int | None, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, None, "missing_bot_token"
    payload = {"chat_id": user.telegram_id, "text": text}
    try:
        res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=5)
        res.raise_for_status()
        data = res.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception("initiation_sweep: telegram send failed user=%s error=%s", user.id, exc)
        return False, None, "telegram_send_failed"

    message_id = None
    try:
        message_id = data.get("result", {}).get("message_id")
    except Exception:  # noqa: BLE001
        message_id = None
    return True, message_id, ""


@transaction.atomic
def _deliver_event(event: InitiationEvent) -> InitiationEvent.Status:
    now = timezone.now()
    state = event.state
    user = state.user
    bot = state.bot

    if user.is_blocked or (hasattr(user, "restriction") and user.restriction.block_initiation):
        InitiationEvent.objects.filter(id=event.id).update(
            status=InitiationEvent.Status.SKIPPED,
            updated_at=now,
            error_message="blocked_before_send",
        )
        return InitiationEvent.Status.SKIPPED

    conversation, _ = Conversation.objects.get_or_create(
        user=user,
        bot=bot,
        status=Conversation.Status.ACTIVE,
        defaults={"last_activity_at": now},
    )
    seq = next_message_seq(conversation.id)

    ok, telegram_message_id, error_message = _send_telegram_message(user, event.message_text)
    status = InitiationEvent.Status.SENT if ok else InitiationEvent.Status.FAILED

    Message.objects.create(
        conversation=conversation,
        role=Message.Role.BOT,
        text=event.message_text,
        seq=seq,
        telegram_message_id=telegram_message_id,
    )

    BotUserState.objects.filter(id=state.id).update(
        last_bot_reply_at=now,
        last_initiation_at=now,
        total_bot_replies=F("total_bot_replies") + 1,
        updated_at=now,
    )
    Conversation.objects.filter(id=conversation.id).update(
        last_activity_at=now,
        last_bot_reply_at=now,
        has_unread_bot_message=True,
        updated_at=now,
    )
    User.objects.filter(id=user.id).update(last_seen_at=now, updated_at=now)

    InitiationEvent.objects.filter(id=event.id).update(
        status=status,
        sent_at=now if ok else None,
        telegram_message_id=telegram_message_id,
        error_message=error_message,
        updated_at=now,
    )
    return status




def _preferred_contact_hours(state: BotUserState) -> list[int]:
    """Infer preferred local hours from recent user messages + onboarding preference."""

    user_tz = _user_timezone(state.user)
    recent = list(
        Message.objects.filter(conversation__user=state.user, conversation__bot=state.bot, role=Message.Role.USER)
        .order_by("-created_at")
        .values_list("created_at", flat=True)[:120]
    )

    counter: Counter[int] = Counter()
    for dt in recent:
        if not dt:
            continue
        local = dt.astimezone(user_tz)
        counter[local.hour] += 1

    # onboarding hint can bias hour selection
    onboarding = (state.style_rules or {}).get("onboarding_v1") or {}
    active_hint = ((onboarding.get("answers") or {}).get("active_time_pattern") or "").strip()
    if "شب" in active_hint:
        for h in (20, 21, 22, 23):
            counter[h] += 2
    elif "روز" in active_hint:
        for h in (10, 11, 12, 13, 14, 15, 16):
            counter[h] += 2

    if not counter:
        return [11, 20]

    top = [hour for hour, _ in counter.most_common(3)]
    return sorted(set(top))


def _schedule_for_preferred_window(state: BotUserState, now: datetime) -> tuple[datetime, int]:
    user_tz = _user_timezone(state.user)
    preferred_hours = _preferred_contact_hours(state)

    local_now = now.astimezone(user_tz)
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        date = local_now.date() + timedelta(days=day_offset)
        for hour in preferred_hours:
            candidate = datetime(date.year, date.month, date.day, hour, random.randint(2, 44), tzinfo=user_tz)
            if candidate > local_now + timedelta(minutes=2):
                candidates.append(candidate)

    if not candidates:
        fallback = local_now + timedelta(hours=6)
        candidates = [fallback]

    chosen_local = min(candidates)
    chosen_utc = chosen_local.astimezone(timezone.utc)
    jitter_seconds = int((chosen_utc - now).total_seconds())
    return chosen_utc, max(jitter_seconds, 0)

def _due_events(now: datetime) -> list[InitiationEvent]:
    return list(
        InitiationEvent.objects.filter(
            status=InitiationEvent.Status.PLANNED,
            scheduled_for__lte=now,
        )
        .select_related("state__bot", "state__user", "state__user__restriction")
        .order_by("scheduled_for")[:MAX_SEND_BATCH]
    )


def _plan_candidates(now: datetime) -> list[BotUserState]:
    return list(
        BotUserState.objects.select_related("bot", "user")
        .filter(
            bot__is_active=True,
            initiation_opt_in=True,
            user__is_active=True,
            user__is_blocked=False,
        )
        .order_by("updated_at")[:MAX_ELIGIBLE_BATCH]
    )


def _context_maps(states: list[BotUserState]) -> dict:
    bot_ids = {state.bot_id for state in states}
    user_ids = {state.user_id for state in states}

    rules = {r.bot_id: r for r in InitiationRule.objects.filter(bot_id__in=bot_ids, enabled=True)}
    identities = {
        ident.bot_id: ident
        for ident in BotIdentity.objects.filter(bot_id__in=bot_ids)
        .select_related("bot")
    }
    prefs = {pref.user_id: pref for pref in UserPrefs.objects.filter(user_id__in=user_ids)}
    restrictions = {r.user_id: r for r in UserRestriction.objects.filter(user_id__in=user_ids)}
    return {"rules": rules, "identities": identities, "prefs": prefs, "restrictions": restrictions}


def _ensure_pref(pref_map: dict, user: User) -> UserPrefs:
    pref = pref_map.get(user.id)
    if pref:
        return pref
    pref, _ = UserPrefs.objects.get_or_create(user=user)
    pref_map[user.id] = pref
    return pref


def _ensure_identity(identity_map: dict, bot_id) -> BotIdentity:
    identity = identity_map.get(bot_id)
    if identity:
        return identity
    identity, _ = BotIdentity.objects.get_or_create(bot_id=bot_id)
    identity_map[bot_id] = identity
    return identity


def run_initiation_sweep() -> dict[str, int]:
    now = timezone.now()
    summary = {"planned": 0, "sent": 0, "skipped": 0, "failed": 0}

    due_events = _due_events(now)
    for event in due_events:
        status = _deliver_event(event)
        if status == InitiationEvent.Status.SENT:
            summary["sent"] += 1
        elif status == InitiationEvent.Status.SKIPPED:
            summary["skipped"] += 1
        else:
            summary["failed"] += 1

    candidates = _plan_candidates(now)
    context = _context_maps(candidates)

    for state in candidates:
        rule = context["rules"].get(state.bot_id)
        if not rule:
            continue
        prefs = _ensure_pref(context["prefs"], state.user)
        identity = _ensure_identity(context["identities"], state.bot_id)
        restriction = context["restrictions"].get(state.user_id)
        ctx = EligibilityContext(rule=rule, prefs=prefs, identity=identity, restriction=restriction)

        if not _should_initiate(state, ctx, now):
            summary["skipped"] += 1
            continue

        idem_key = f"{state.id}:{now.date().isoformat()}:{now.hour // 6}"
        if InitiationEvent.objects.filter(state=state, idempotency_key=idem_key).exists():
            summary["skipped"] += 1
            continue

        scheduled_for, jitter_seconds = _schedule_for_preferred_window(state, now)
        message_text = _build_message(state, ctx)
        InitiationEvent.objects.create(
            state=state,
            bot=state.bot,
            user=state.user,
            trigger=InitiationEvent.Trigger.SCHEDULER,
            status=InitiationEvent.Status.PLANNED,
            scheduled_for=scheduled_for,
            message_text=message_text,
            idempotency_key=idem_key,
            meta={"jitter_seconds": jitter_seconds},
        )
        summary["planned"] += 1
    return summary


class Command(BaseCommand):
    help = "Run initiation sweep to schedule and send proactive interactions."

    def handle(self, *args, **options):
        job = ScheduledJob.objects.create(
            job_type=ScheduledJob.JobType.INITIATION_SWEEP,
            status=ScheduledJob.Status.RUNNING,
            started_at=timezone.now(),
            meta={},
        )
        try:
            summary = run_initiation_sweep()
            ScheduledJob.objects.filter(id=job.id).update(
                status=ScheduledJob.Status.OK,
                finished_at=timezone.now(),
                meta=summary,
            )
            self.stdout.write(self.style.SUCCESS(f"initiation_sweep completed: {summary}"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("initiation_sweep failed: %s", exc)
            ScheduledJob.objects.filter(id=job.id).update(
                status=ScheduledJob.Status.ERROR,
                finished_at=timezone.now(),
                error_message=str(exc)[:255],
            )
            raise
