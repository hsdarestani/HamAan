from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Sequence

from django.utils import timezone

from persona.models import BotUserState


_STOPWORDS = {
    "و",
    "که",
    "در",
    "به",
    "از",
    "با",
    "برای",
    "من",
    "تو",
    "ما",
    "شما",
    "این",
    "آن",
    "یه",
    "یک",
    "می",
    "هستم",
    "هستی",
    "هست",
    "هستیم",
    "بود",
    "بودم",
    "بودیم",
    "کردم",
    "کردی",
    "کردیم",
    "کرد",
    "بودم",
    "هستم",
    "باشم",
    "باش",
    "باشه",
    "باشیم",
    "کن",
    "باید",
    "نیست",
    "نیستم",
    "نیستی",
    "نیستیم",
    "هم",
    "یا",
    "تا",
    "اما",
    "ولی",
    "نه",
    "آره",
    "اون",
    "اونا",
    "خودم",
    "خودت",
    "خودمون",
    "چون",
    "همین",
    "الان",
    "داخل",
    "روی",
    "زیر",
    "بالا",
    "چیز",
    "چیه",
    "چی",
    "ها",
    "های",
    "میگم",
    "میگن",
    "میگه",
    "بهم",
    "بهت",
    "بهش",
    "یکم",
}

_FEELING_KEYWORDS = {
    "خسته": "خسته",
    "بیحال": "بی‌حال",
    "بی حال": "بی‌حال",
    "دلگیر": "دلگیر",
    "نگران": "نگران",
    "ناراحت": "ناراحت",
    "غمگین": "غمگین",
    "استرس": "استرس",
    "مضطرب": "مضطرب",
    "هیجان": "هیجان",
    "خوشحال": "خوشحال",
    "آروم": "آروم",
    "آرام": "آرام",
    "بی حوصله": "بی‌حوصله",
    "بی‌حوصله": "بی‌حوصله",
}

_NICKNAME_PATTERNS = (
    re.compile(r"صدام\s+کن\s+(?:به\s+)?([\w\u0600-\u06FF\s]{2,24})", re.IGNORECASE),
    re.compile(r"منو\s+([\w\u0600-\u06FF\s]{2,24})\s+صدا\s+کن", re.IGNORECASE),
    re.compile(r"اسم\s+من\s+([\w\u0600-\u06FF]{2,24})", re.IGNORECASE),
)


def _normalize_text(text: str | None) -> str:
    normalized = (text or "").replace("\u200c", " ").strip()
    return re.sub(r"\s+", " ", normalized)


def _merge_unique(existing: Iterable[str] | None, new: Iterable[str], *, limit: int) -> list[str]:
    merged: list[str] = []
    for item in (existing or []):
        cleaned = _normalize_text(item)
        if cleaned and cleaned not in merged:
            merged.append(cleaned[:64])
    for item in new:
        cleaned = _normalize_text(item)
        if cleaned and cleaned not in merged:
            merged.append(cleaned[:64])
        if len(merged) >= limit:
            break
    return merged[:limit]


def _extract_topics(history: Sequence[str], *, limit: int = 8) -> list[str]:
    tokens: list[str] = []
    for text in history:
        normalized = _normalize_text(text).lower()
        cleaned = re.sub(r"[^\w\s\u0600-\u06FF]", " ", normalized)
        for token in cleaned.split():
            if len(token) < 3 or token in _STOPWORDS:
                continue
            if token.isdigit():
                continue
            tokens.append(token)
    if not tokens:
        return []

    counts = Counter(tokens)
    return [token for token, _ in counts.most_common(limit)]


def _extract_nicknames(latest_text: str | None, *, limit: int = 5) -> list[str]:
    if not latest_text:
        return []
    text = _normalize_text(latest_text)
    candidates: list[str] = []
    for pattern in _NICKNAME_PATTERNS:
        for match in pattern.findall(text):
            candidate = _normalize_text(match)
            if 2 <= len(candidate) <= 24:
                candidates.append(candidate)
    return candidates[:limit]


def _extract_feelings(history: Sequence[str], *, limit: int = 5) -> list[str]:
    feelings: list[str] = []
    for text in history:
        normalized = _normalize_text(text).lower()
        for key, label in _FEELING_KEYWORDS.items():
            if key in normalized and label not in feelings:
                feelings.append(label)
                if len(feelings) >= limit:
                    return feelings
    return feelings


def update_relationship_memory(
    state: BotUserState, user_messages: Iterable[str], *, latest_text: str | None = None, now=None
) -> None:
    """
    Update structured relationship memory based on recent user messages.

    This keeps the field lightweight and avoids duplicates while preserving prior values.
    """

    history = [msg for msg in user_messages if msg]
    if latest_text:
        history.insert(0, latest_text)
    if not history:
        return

    topics = _extract_topics(history, limit=8)
    nicknames = _extract_nicknames(latest_text, limit=5)
    feelings = _extract_feelings(history, limit=5)

    memory = state.relationship_memory or {}
    updated_memory = dict(memory)
    changed = False

    if topics:
        merged_topics = _merge_unique(memory.get("shared_topics"), topics, limit=8)
        if merged_topics != memory.get("shared_topics"):
            updated_memory["shared_topics"] = merged_topics
            changed = True

    if nicknames:
        merged_nicknames = _merge_unique(memory.get("nicknames"), nicknames, limit=5)
        if merged_nicknames != memory.get("nicknames"):
            updated_memory["nicknames"] = merged_nicknames
            changed = True

    if feelings:
        merged_feelings = _merge_unique(memory.get("recent_feelings"), feelings, limit=5)
        if merged_feelings != memory.get("recent_feelings"):
            updated_memory["recent_feelings"] = merged_feelings
            changed = True

    if changed:
        timestamp = now or timezone.now()
        BotUserState.objects.filter(id=state.id).update(
            relationship_memory=updated_memory,
            updated_at=timestamp,
        )
        state.relationship_memory = updated_memory
