"""Поиск по всем проектам: в каком ролике и на какой минуте сказано слово или фраза.

Ищем в том, что звучит в смонтированном ролике: в расшифровке (монтаж по тексту) или в
субтитрах. Слова сравниваются без учёта регистра, знаков и «ё»; начало слова тоже подходит
(«камер» найдёт «камера», «камеру»).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from glimpsy.editor import transcript as tr

log = logging.getLogger(__name__)

CONTEXT_WORDS = 6


@dataclass
class Hit:
    project: Path
    name: str
    t: float                # время в ролике
    context: str            # фраза вокруг найденного


def project_words(d: Path) -> tuple[str, list[tuple[float, float, str]]]:
    """Имя проекта и его речь во времени ролика."""
    from glimpsy.editor.project import Project
    from glimpsy.editor.subtitles_panel import subtitle_items

    p = Project.load(d)
    if p.text_edit:
        words = tr.output_words(p, tr.TranscriptStore(p.dir))
    else:
        words = [(t.start, t.end, w) for t in subtitle_items(p) for w in t.text.split()]
    name = p.name
    srcs = p.cuts.get("sources") or []
    if srcs:
        name = Path(srcs[0]["src"]).stem + (f" и ещё {len(srcs) - 1}" if len(srcs) > 1 else "")
    return name, words


def find(words: list[tuple[float, float, str]], query: str) -> list[tuple[float, str]]:
    q = [tr.norm_word(x) for x in query.split()]
    q = [x for x in q if x]
    if not q:
        return []
    normed = [tr.norm_word(w[2]) for w in words]
    out = []
    n = len(q)
    for i in range(len(normed) - n + 1):
        if all(normed[i + k] == q[k] for k in range(n - 1)) and normed[i + n - 1].startswith(q[-1]):
            a, b = max(0, i - CONTEXT_WORDS), min(len(words), i + n + CONTEXT_WORDS)
            ctx = " ".join(w[2] for w in words[a:b])
            out.append((words[i][0], ("…" if a else "") + ctx + ("…" if b < len(words) else "")))
    return out


class Index:
    """Речь всех проектов — читается один раз, дальше поиск мгновенный."""

    def __init__(self, dirs: list[Path]) -> None:
        self.items: list[tuple[Path, str, list]] = []
        for d in dirs:
            try:
                name, words = project_words(d)
            except Exception:                         # noqa: BLE001 — один битый проект не мешает искать
                log.exception("Проект %s не читается для поиска", d)
                continue
            if words:
                self.items.append((d, name, words))

    def search(self, query: str, limit: int = 300) -> list[Hit]:
        hits: list[Hit] = []
        for d, name, words in self.items:
            for t, ctx in find(words, query):
                hits.append(Hit(d, name, t, ctx))
                if len(hits) >= limit:
                    return hits
        return hits
