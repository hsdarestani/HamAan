"""Lightweight helpers for persisting per-user relationship cues."""

from __future__ import annotations

import re
from collections.abc import Iterable
from django.utils import timezone

from .models import BotUserState


_STOPWORDS = {
    "من",
    "تو",
    "ما",
    "شما",
    "اون",
    "این",
    "اینکه",
    "اونجا",
    "برای",
    "اینها",
    "یک",
    "یه",
    "و",
    "یا",
    "اما",
    "که",
    "چون",
    "با",
    "به",
    "از",
    "در",
    "روی",
    "تا",
}

_NICKNAMES = {
    "رفیق",
    "دوست",
    "داداش",
    "عزیز",
    "عزیزم",
    "جون",
    "جونم",
    "رفیق جان",
    "برادر",
    "همراه",
}

_FEELINGS = {
    "خسته",
    "بیحال",
    "بی‌حال",
    "دلگیر",
    "نگران",
    "ناراحت",
    "غمگین",
    "استرس",
    "استرسی",
    "آروم",
    "آرام",
    "شاد",
    "خوشحال",
    "هیجان",
}


def _merge_unique(existing: Iterable[str], additions: Iterable[str], limit: int = 8) -> list[str]:
    seen = set()
    merged: list[str] = []
    for item in existing:
        normalized = (item or "").strip()
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)
    for item in additions:
        normalized = (item or "").strip()
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)
        if len(merged) >= limit:
            break
    return merged[:limit]


def _extract_topics(text: str) -> list[str]:
    lowered = text.lower()
    hashtags = [tag[:32] for tag in re.findall(r"#([\w\d\u0600-\u06FF_]{2,})", lowered)]
    tokens = re.findall(r"[\w\u0600-\u06FF]{3,}", lowered)
    topics: list[str] = []
    for tok in hashtags + tokens:
        if tok in _STOPWORDS:
            continue
        if tok in _NICKNAMES:
            continue
        if tok in _FEELINGS:
            continue
        topics.append(tok[:48])
        if len(topics) >= 6:
            break
    return topics


def _extract_nicknames(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in _NICKNAMES if name in lowered]


def _extract_feelings(text: str) -> list[str]:
    lowered = text.lower()
    feelings: list[str] = []
    for feeling in _FEELINGS:
        if feeling in lowered:
            feelings.append(feeling)
    return feelings


def extract_relationship_signals(text: str) -> dict[str, list[str]]:
    """Detect soft relationship cues from free-form text."""

    snippet = (text or "").strip()
    if not snippet:
        return {}

    shared_topics = _extract_topics(snippet)
    nicknames = _extract_nicknames(snippet)
    recent_feelings = _extract_feelings(snippet)

    signals: dict[str, list[str]] = {}
    if shared_topics:
        signals["shared_topics"] = shared_topics
    if nicknames:
        signals["nicknames"] = nicknames
    if recent_feelings:
        signals["recent_feelings"] = recent_feelings
    return signals


def update_relationship_memory(state: BotUserState, text: str) -> bool:
    """Merge detected signals into the state's relationship_memory."""

    signals = extract_relationship_signals(text)
    if not signals:
        return False

    current = state.relationship_memory or {}
    updated = dict(current)
    changed = False

    for key, additions in signals.items():
        existing_values = current.get(key) or []
        merged = _merge_unique(existing_values, additions)
        if merged != existing_values:
            updated[key] = merged
            changed = True

    if not changed:
        return False

    BotUserState.objects.filter(id=state.id).update(
        relationship_memory=updated,
        updated_at=timezone.now(),
    )
    state.relationship_memory = updated
    state.updated_at = timezone.now()
    return True
