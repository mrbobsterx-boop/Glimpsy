"""Главы для YouTube: «00:00 Вступление, 01:25 …» — для описания видео.

Программа предлагает главы сама — по расшифровке речи: делит ролик на части по концам фраз
примерно равной длины и берёт начало первой фразы части как название. Всё можно поправить.

Правила YouTube: первая глава — с 00:00, глав не меньше трёх, каждая не короче 10 секунд.
"""

from __future__ import annotations

import re

MIN_CHAPTER_S = 10.0
TITLE_WORDS = 6


def fmt(t: float, long: bool = False) -> str:
    t = max(0, int(t))
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if long or h else f"{m:02d}:{s:02d}"


def title_from(words: list[str]) -> str:
    text = " ".join(words[:TITLE_WORDS]).strip()
    text = re.sub(r"[\s,;:—–-]+$", "", text)
    text = text.rstrip(".!?…")
    if len(words) > TITLE_WORDS and text:
        text += "…"
    return text[:1].upper() + text[1:]


def sentences(words: list[tuple[float, float, str]]) -> list[tuple[float, list[str]]]:
    """Фразы: (время начала, слова)."""
    out: list[tuple[float, list[str]]] = []
    cur: list[str] = []
    start = 0.0
    prev_end = None
    for a, b, text in words:
        if not cur:
            start = a
        elif prev_end is not None and a - prev_end > 1.0 and len(cur) >= 3:
            out.append((start, cur))                 # долгая пауза — тоже конец фразы
            cur, start = [], a
        cur.append(text)
        prev_end = b
        if text.rstrip().endswith((".", "!", "?", "…")):
            out.append((start, cur))
            cur = []
    if cur:
        out.append((start, cur))
    return out


def suggest(words: list[tuple[float, float, str]], total: float) -> list[dict]:
    """Предложить главы: [{"t": секунды, "title": название}]."""
    phrases = sentences(words)
    if not phrases or total < 3 * MIN_CHAPTER_S:
        return []
    count = max(3, min(12, int(total // 90) + 1))
    step = max(MIN_CHAPTER_S * 1.5, total / count)
    out = [{"t": 0.0, "title": title_from(phrases[0][1]) or "Начало"}]
    for t, ws in phrases[1:]:
        if t - out[-1]["t"] >= step and total - t >= MIN_CHAPTER_S:
            out.append({"t": round(t, 2), "title": title_from(ws)})
    return out if len(out) >= 3 else []


def problems(items: list[dict], total: float) -> list[str]:
    """Что YouTube не примет — чтобы сказать человеку заранее."""
    out = []
    items = sorted(items, key=lambda c: c["t"])
    if len(items) < 3:
        out.append("YouTube показывает главы, только если их хотя бы три.")
    if items and items[0]["t"] > 0.5:
        out.append("Первая глава должна начинаться с 00:00.")
    ends = [c["t"] for c in items[1:]] + [total]
    if any(e - c["t"] < MIN_CHAPTER_S for c, e in zip(items, ends)):
        out.append("Каждая глава должна длиться не меньше 10 секунд.")
    return out


def as_text(items: list[dict], total: float = 0.0) -> str:
    items = sorted(items, key=lambda c: c["t"])
    long = total >= 3600
    lines = []
    for i, c in enumerate(items):
        t = 0.0 if i == 0 else c["t"]                # первая — всегда с нуля
        lines.append(f"{fmt(t, long)} {c['title'].strip() or 'Глава'}")
    return "\n".join(lines)
