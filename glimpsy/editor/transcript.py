"""Монтаж по тексту: слова с таймкодами и вырезы по ним.

Как устроено (ничего не удаляется из исходников):
  * каждое исходное видео распознаётся один раз — слова со временем лежат в папке проекта
    (transcript/<ключ>.json) вместе с «громкостью» звука (transcript/<ключ>.npy, по 10 мс);
  * какие слова и паузы вырезаны — пометки в самом проекте (Project.cuts): поэтому работают
    отмена/повтор и автосохранение, а восстановить можно что угодно;
  * из слов и пометок заново строится список фрагментов (Project.clips) — лента, просмотр
    и экспорт работают с ними как обычно.

Время слов у Whisper неточное (особенно после долгой тишины — слово «прилипает» к её
началу), поэтому начало и конец каждого слова уточняются по громкости звука, а места
разрезов сдвигаются в самую тихую точку рядом — речь не обрезается на полуслове.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

RATE = 16000
FRAME_S = 0.01                   # громкость — по 10 мс
SNAP_S = 0.08                    # разрез ищет самую тихую точку в пределах ±80 мс
MIN_RANGE_S = 0.12               # куски короче не оставляем
DEFAULT_CUTS = {"pause_cut": True, "pause_min": 0.7, "pad": 0.15}
# подсказка распознаванию: так Whisper чаще записывает «э», «эм», «ну» как есть, а не выкидывает
PROMPT_RU = "Ну, э-э, вот, значит, эм… короче, типа, это самое, ну вот."


@dataclass
class Word:
    text: str
    start: float
    end: float
    p: float = 1.0               # уверенность распознавания (0…1)


@dataclass
class SourceWords:
    """Распознанная речь одного исходного видео."""
    src: str                     # путь к видео (как в Clip.src)
    duration: float
    words: list[Word] = field(default_factory=list)
    language: str = ""
    model: str = ""

    def to_dict(self) -> dict:
        return {"src": self.src, "duration": self.duration, "language": self.language, "model": self.model,
                "words": [[w.text, round(w.start, 3), round(w.end, 3), round(w.p, 3)] for w in self.words]}

    @classmethod
    def from_dict(cls, d: dict) -> "SourceWords":
        return cls(d["src"], float(d["duration"]), [Word(t, float(a), float(b), float(p)) for t, a, b, p in d["words"]],
                   d.get("language", ""), d.get("model", ""))


def source_key(src: str) -> str:
    """Короткое имя для файлов расшифровки одного видео."""
    return hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]


# ---------------- хранение ----------------

class TranscriptStore:
    """Расшифровки видео проекта (папка transcript/ рядом с edit.json)."""

    def __init__(self, project_dir: Path) -> None:
        self.dir = Path(project_dir) / "transcript"
        self._words: dict[str, SourceWords] = {}
        self._env: dict[str, np.ndarray] = {}

    def has(self, src: str) -> bool:
        return self.get(src) is not None

    def get(self, src: str) -> SourceWords | None:
        if src not in self._words:
            f = self.dir / f"{source_key(src)}.json"
            if not f.exists():
                return None
            try:
                self._words[src] = SourceWords.from_dict(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                log.exception("Расшифровка %s не читается", f)
                return None
        return self._words[src]

    def clean_noise(self, src: str) -> list[int] | None:
        """Старая расшифровка с подписями звуков («*звук*») — убрать их и сохранить. Возвращает
        новые номера слов (чтобы поправить пометки монтажа) или None, если убирать нечего."""
        sw = self.get(src)
        if sw is None:
            return None
        words, mapping = drop_noise(sw.words)
        if mapping is None:
            return None
        sw.words = words
        self.put(sw, None)
        log.info("Из расшифровки %s убраны подписи звуков: %d", src, mapping.count(-1))
        return mapping

    def envelope(self, src: str) -> np.ndarray | None:
        if src not in self._env:
            f = self.dir / f"{source_key(src)}.npy"
            if not f.exists():
                return None
            self._env[src] = np.load(f)
        return self._env[src]

    def put(self, sw: SourceWords, env: np.ndarray | None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        key = source_key(sw.src)
        tmp = self.dir / f"{key}.json.tmp"
        tmp.write_text(json.dumps(sw.to_dict(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / f"{key}.json")
        if env is not None:
            np.save(self.dir / f"{key}.npy", env.astype(np.float16))
            self._env[sw.src] = env.astype(np.float16)
        self._words[sw.src] = sw


# ---------------- громкость ----------------

def envelope(samples: np.ndarray) -> np.ndarray:
    """Громкость (RMS) каждых 10 мс."""
    hop = int(RATE * FRAME_S)
    n = len(samples) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    x = samples[: n * hop].reshape(n, hop)
    return np.sqrt((x * x).mean(axis=1)).astype(np.float32)


def silence_level(env: np.ndarray) -> float:
    """Порог «тишины» для этой записи: чуть выше шума в паузах."""
    if len(env) == 0:
        return 0.01
    floor = float(np.percentile(env, 10))
    return max(0.006, min(0.05, floor * 3.0))


def _frame_index(t: float) -> int:
    return max(0, int(t / FRAME_S + 1e-6))          # 1.97 / 0.01 = 196.999… — это кадр 197


def _frames(env: np.ndarray, a: float, b: float) -> np.ndarray:
    i, j = _frame_index(a), min(len(env), int(np.ceil(b / FRAME_S - 1e-6)))
    return env[i:j] if j > i else env[0:0]


# ---------------- разбор ответа Whisper ----------------

DTW_LEAD_S = 0.25                # точка выравнивания приходится примерно на середину первого звука слова
DTW_TAIL_S = 0.2


def parse_whisper_words(text: str) -> tuple[list[Word], str]:
    """Подробный JSON whisper.cpp (-ojf): куски слов (токены) → слова со временем.

    Новое слово начинается с токена, у которого впереди пробел; знаки препинания
    и продолжения слова приклеиваются к предыдущему. Если есть точное время по выравниванию
    со звуком (t_dtw, ключ -dtw), берём его: обычное время у Whisper бывает сдвинуто на секунды.
    """
    data = json.loads(text)
    words: list[Word] = []
    probs: list[list[float]] = []
    dtw: list[list[float]] = []
    for seg in data.get("transcription", []):
        for tok in seg.get("tokens", []):
            t = tok.get("text", "")
            if not t or t.startswith("[_") or t.startswith("<|"):
                continue                                     # служебные метки Whisper
            off = tok.get("offsets", {})
            a, b = off.get("from", 0) / 1000.0, off.get("to", 0) / 1000.0
            p = float(tok.get("p", 1.0))
            d = tok.get("t_dtw", -1)
            d = d / 100.0 if isinstance(d, (int, float)) and d >= 0 else None
            if t.startswith(" ") or not words:
                words.append(Word(t.strip(), a, max(a, b), p))
                probs.append([p])
                dtw.append([d] if d is not None else [])
            else:
                w = words[-1]
                w.text += t
                w.end = max(w.end, b)
                probs[-1].append(p)
                if d is not None:
                    dtw[-1].append(d)
    for w, ps in zip(words, probs):
        w.p = float(min(ps))
    if words and all(dtw):
        # точное время: начало — чуть раньше точки выравнивания первого куска слова,
        # конец — после последнего куска, но не позже начала следующего слова
        starts = [max(0.0, d[0] - DTW_LEAD_S) for d in dtw]
        for k in range(1, len(starts)):
            starts[k] = max(starts[k], starts[k - 1] + 0.02)
        for k, w in enumerate(words):
            w.start = round(starts[k], 3)
            end = dtw[k][-1] + DTW_TAIL_S
            if k + 1 < len(words):
                end = min(end, starts[k + 1])
            w.end = round(max(w.start + 0.05, end), 3)
    words = [w for w in words if w.text.strip()]
    return words, data.get("result", {}).get("language", "")


HALLUCINATIONS = ("продолжение следует", "субтитры сделал", "субтитры создавал", "редактор субтитров",
                  "спасибо за просмотр", "amara.org", "dimatorzok")


# Распознавание иногда подписывает не слова, а звуки: «*звук отзыва*», «*поёт*», «[музыка]»,
# «(смеётся)», «♪». Это не речь — в тексте и субтитрах этого быть не должно.
_OPEN, _CLOSE = "*[(", "*])"


def noise_words(words: list[Word], longest: int = 6) -> set[int]:
    """Номера «слов», которые на самом деле подписи звуков (между * *, [ ], ( ) или с ♪).
    Подпись не длиннее longest слов: незакрытая звёздочка не съест весь текст до конца."""
    out: set[int] = set()
    i = 0
    while i < len(words):
        t = words[i].text.strip()
        if "♪" in t or "♫" in t:
            out.add(i)
            i += 1
            continue
        if t[:1] in _OPEN:
            close = _CLOSE[_OPEN.index(t[0])]
            body = t.rstrip(".,!?…:;")
            if len(body) > 1 and body.endswith(close):          # «*поёт*», «[музыка]» — одно слово
                out.add(i)
                i += 1
                continue
            end = next((k for k in range(i + 1, min(len(words), i + longest))
                        if words[k].text.strip().rstrip(".,!?…:;").endswith(close)), None)
            if end is not None:
                out.update(range(i, end + 1))
                i = end + 1
                continue
            if t[0] == "*" and len(body) > 1:                   # «*звук» без пары — тоже подпись
                out.add(i)
        i += 1
    return out


def drop_noise(words: list[Word]) -> tuple[list[Word], list[int] | None]:
    """Убрать подписи звуков. Возвращает (слова, новый номер для каждого старого: −1 — убрано) или
    (те же слова, None), если убирать нечего."""
    gone = noise_words(words)
    if not gone:
        return words, None
    mapping, kept = [], []
    for i, w in enumerate(words):
        if i in gone:
            mapping.append(-1)
        else:
            mapping.append(len(kept))
            kept.append(w)
    return kept, mapping


def remap_cuts(cuts: dict, src: str, mapping: list[int]) -> None:
    """Пометки монтажа одного видео — на новые номера слов (после того как убрали подписи звуков)."""
    def new(i: int) -> int:
        return mapping[i] if 0 <= i < len(mapping) else -1

    def before(i: int) -> int:
        """Пауза после слова i — теперь после ближайшего оставшегося слова до него."""
        while i >= 0 and new(i) < 0:
            i -= 1
        return new(i) if i >= 0 else -1

    for field in ("deleted", "muted", "hidden", "kept_pauses"):
        lst = cuts.get(field, {}).get(src)
        if lst is not None:
            if field == "kept_pauses":
                cuts[field][src] = sorted({before(i) for i in lst})
            else:
                cuts[field][src] = sorted({new(i) for i in lst if new(i) >= 0})
    pm = cuts.get("pause_marks", {}).get(src)
    if pm is not None:
        cuts["pause_marks"][src] = {str(before(int(k)) if int(k) >= 0 else -1): v for k, v in pm.items()}
    wt = cuts.get("word_text", {}).get(src)
    if wt is not None:
        cuts["word_text"][src] = {str(new(int(k))): v for k, v in wt.items() if new(int(k)) >= 0}


def refine_words(words: list[Word], env: np.ndarray | None) -> list[Word]:
    """Уточнить время слов по громкости; убрать «слова», сказанные тишиной (выдумки Whisper),
    и подписи звуков («*звук*», «[музыка]»)."""
    words, _m = drop_noise(words)
    if env is None or len(env) == 0:
        return [w for w in words if w.end > w.start or w.text]
    thr = silence_level(env)
    out: list[Word] = []
    for k, w in enumerate(words):
        a, b = w.start, max(w.end, w.start + FRAME_S)
        fr = _frames(env, a, b)
        if len(fr) and float(fr.max()) < thr:
            # В этом месте тишина. Обычно Whisper просто поставил слово в начало паузы
            # (после долгой тишины первые слова «прилипают» к её началу) — слово прикрепляем
            # к началу речи, которая идёт дальше. Выдумки на тишине отсеиваются ниже по тексту.
            ahead = _frames(env, a, a + 60.0)
            loud = np.nonzero(ahead >= thr)[0]
            if len(loud) == 0:
                continue                                           # дальше только тишина — выдумка
            onset = _frame_index(a) * FRAME_S + loud[0] * FRAME_S
            a, b = onset, onset + FRAME_S
            fr = _frames(env, a, b)
        # начало: пропустить тишину в начале слова
        loud = np.nonzero(fr >= thr)[0]
        if len(loud):
            a2 = a + loud[0] * FRAME_S
            b2 = a + (loud[-1] + 1) * FRAME_S
            a, b = min(a2, b - FRAME_S), max(b2, a2 + FRAME_S)
        if out and a < out[-1].end:
            a = out[-1].end
        if b <= a:
            b = a + FRAME_S
        out.append(Word(w.text, round(a, 3), round(b, 3), w.p))
    joined = " ".join(w.text for w in out).lower()
    if any(h in joined for h in HALLUCINATIONS):
        out = [w for w in out if not any(h.split()[0] in w.text.lower() for h in HALLUCINATIONS)]
    return out


# ---------------- вырезы ----------------

def cuts_of(project) -> dict:
    c = dict(DEFAULT_CUTS)
    c.update(getattr(project, "cuts", {}) or {})
    return c


def pauses(words: list[Word], pause_min: float, duration: float) -> list[tuple[int, float]]:
    """Паузы длиннее порога: (номер слова перед паузой — или −1 для паузы в начале, длина)."""
    out = []
    if words and words[0].start > pause_min:
        out.append((-1, words[0].start))
    for i in range(len(words) - 1):
        gap = words[i + 1].start - words[i].end
        if gap > pause_min:
            out.append((i, gap))
    if words and duration - words[-1].end > pause_min:
        out.append((len(words) - 1, duration - words[-1].end))
    return out


def _snap(env: np.ndarray | None, t: float, lo: float, hi: float) -> float:
    """Сдвинуть разрез в самую тихую точку рядом (не выходя за [lo, hi])."""
    if env is None or len(env) == 0:
        return t
    a, b = max(lo, t - SNAP_S), min(hi, t + SNAP_S)
    if b - a < FRAME_S * 2:
        return t
    fr = _frames(env, a, b).astype(np.float32)
    if len(fr) == 0:
        return t
    # из почти одинаково тихих точек — ближайшая к задуманной (в ровной тишине разрез не двигается)
    quiet = np.nonzero(fr <= float(fr.min()) * 1.2 + 1e-4)[0]
    times = (_frame_index(a) + quiet) * FRAME_S + FRAME_S / 2
    return float(times[int(np.argmin(np.abs(times - t)))])


VOICED_REACH_S = 0.5            # дальше этого разрез «за голосом» не уходит
EDGE_PAD_S = 0.05               # и чуть тишины после найденного края


def _to_quiet(env: np.ndarray | None, t: float, limit: float, direction: int) -> float:
    """Если в момент t звучит голос — сдвинуть t в сторону direction (−1 раньше, +1 позже) до тишины,
    не дальше limit и не дальше VOICED_REACH_S."""
    if env is None or len(env) == 0:
        return t
    thr = silence_level(env)
    k = _frame_index(t) if direction > 0 else _frame_index(t) - 1
    if not (0 <= k < len(env)) or env[k] < thr:
        return t
    steps = int(VOICED_REACH_S / FRAME_S)
    for n in range(1, steps + 1):
        j = k + direction * n
        if not (0 <= j < len(env)):
            break
        edge = (j + (1 if direction < 0 else 0)) * FRAME_S
        if (direction < 0 and edge < limit) or (direction > 0 and edge > limit):
            return limit
        if env[j] < thr:
            out = edge + direction * EDGE_PAD_S
            return max(limit, out) if direction < 0 else min(limit, out)
    return t                                   # голос не кончается — это не край слова, оставляем как было


def pause_marks(cuts: dict, src: str) -> dict[int, float]:
    """Ручные пометки пауз одного видео: номер → сколько тишины оставить.

    −1 — оставить паузу целиком, 0 — вырезать (с обычным запасом), больше нуля — укоротить
    до стольких секунд. Без пометки пауза следует общему правилу («вырезать длиннее …»).
    """
    out = {int(i): -1.0 for i in cuts.get("kept_pauses", {}).get(src, [])}       # старые проекты
    out.update({int(i): float(v) for i, v in cuts.get("pause_marks", {}).get(src, {}).items()})
    return out


def pause_keep(i: int, gap: float, cuts: dict, marks: dict[int, float]) -> float | None:
    """Сколько тишины останется от паузы: None — вся пауза, иначе — секунд (запас с двух сторон)."""
    pad = float(cuts["pad"])
    m = marks.get(i)
    if m is None:
        m = 0.0 if bool(cuts["pause_cut"]) and gap > float(cuts["pause_min"]) else -1.0
    if m < 0:
        return None
    keep = 2 * pad if m == 0 else m
    return None if keep >= gap else keep


def pause_state(i: int, gap: float, cuts: dict, marks: dict[int, float]) -> str:
    """Как показать паузу в тексте: "keep" — осталась, "cut" — вырезана, "short" — укорочена."""
    if pause_keep(i, gap, cuts, marks) is None:
        return "keep"
    return "short" if marks.get(i, 0.0) > 0 else "cut"


def shown_pauses(sw: SourceWords, cuts: dict, marks: dict[int, float]) -> list[tuple[int, float]]:
    """Паузы, которые видны в тексте: длиннее порога — и те, что помечены вручную."""
    words = sw.words
    out = dict(pauses(words, float(cuts["pause_min"]), sw.duration))
    for i in marks:
        if i in out or not words or i >= len(words):
            continue
        if i == -1:
            gap = words[0].start
        elif i == len(words) - 1:
            gap = sw.duration - words[-1].end
        else:
            gap = words[i + 1].start - words[i].end
        if gap > 0.05:
            out[i] = gap
    return sorted(out.items())


def kept_ranges(sw: SourceWords, deleted: set[int], kept_pauses: set[int], cuts: dict,
                env: np.ndarray | None = None, marks: dict[int, float] | None = None) -> list[tuple[float, float]]:
    """Какие куски исходного видео остаются: [(начало, конец)], по порядку."""
    words, dur = sw.words, sw.duration
    pad, cut_p = float(cuts["pad"]), bool(cuts["pause_cut"])
    if not words:
        return [(0.0, dur)] if dur > 0 else []
    pm = {i: -1.0 for i in kept_pauses}
    pm.update(marks or {})

    def half(i: int, gap: float) -> float | None:
        """Сколько тишины оставить с каждой стороны вырезанной паузы (None — пауза остаётся)."""
        k = pause_keep(i, gap, cuts, pm)
        return None if k is None else k / 2

    # 1. куски речи: подряд идущие оставленные слова, пока их не разделит удалённое слово
    #    или вырезанная пауза
    runs: list[list[int]] = []
    for i in range(len(words)):
        if i in deleted:
            if runs and runs[-1]:
                runs.append([])
            continue
        if runs and runs[-1] and runs[-1][-1] == i - 1 and \
                half(i - 1, words[i].start - words[i - 1].end) is not None:
            runs.append([])
        if not runs:
            runs.append([])
        runs[-1].append(i)
    runs = [r for r in runs if r]

    out: list[tuple[float, float]] = []
    for r in runs:
        first, last = r[0], r[-1]
        # соседние удалённые слова — дальше них кусок не заходит
        lo = words[first - 1].end if first > 0 and (first - 1) in deleted else 0.0
        hi = words[last + 1].start if last + 1 < len(words) and (last + 1) in deleted else dur
        # начало куска
        if first == 0:
            h = half(-1, words[0].start)
            start = 0.0 if h is None else words[0].start - h
        elif (first - 1) in deleted:
            start = lo if not cut_p else max(lo, words[first].start - pad)
        else:
            h = half(first - 1, words[first].start - words[first - 1].end)       # после вырезанной паузы
            start = words[first].start - (pad if h is None else h)
        # конец куска
        if last == len(words) - 1:
            h = half(last, dur - words[-1].end)
            end = dur if h is None else words[-1].end + h
        elif (last + 1) in deleted:
            end = hi if not cut_p else min(hi, words[last].end + pad)
        else:
            h = half(last, words[last + 1].start - words[last].end)               # перед вырезанной паузой
            end = words[last].end + (pad if h is None else h)
        start = max(lo, start)
        end = min(hi, end)
        # время слов у распознавания бывает неточным: если в месте разреза ещё звучит голос —
        # отодвигаем разрез до тишины (иначе слово обрезается на середине)
        start = _to_quiet(env, start, lo, -1)
        end = _to_quiet(env, end, hi, +1)
        # разрез — в самую тихую точку рядом, но не внутрь оставленных или удалённых слов
        start = _snap(env, start, lo, words[first].start) if start > 0 else start
        end = _snap(env, end, words[last].end, hi) if end < dur else end
        start, end = max(0.0, start), min(dur, end)
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        elif end - start >= MIN_RANGE_S:
            out.append((round(start, 3), round(end, 3)))
    return out


# ---------------- слова-паразиты ----------------

DEFAULT_FILLERS = ["э", "эм", "ммм", "ну", "вот", "короче", "типа", "как бы", "значит", "это самое", "в общем",
                   "так сказать"]


def norm_word(text: str) -> str:
    """Слово для сравнения: строчными, без знаков, «ё» как «е», растянутые звуки — одной буквой
    (так «Э-э-э,», «Ээ» и «э» — одно и то же)."""
    t = "".join(ch for ch in text.lower().replace("ё", "е") if ch.isalpha())
    return re.sub(r"(.)\1+", r"\1", t)


def find_fillers(words: list[Word], fillers: list[str]) -> dict[str, list[int]]:
    """Где в речи слова из списка: {слово из списка: [номера слов]} (фраза — все её слова)."""
    normed = [norm_word(w.text) for w in words]
    out: dict[str, list[int]] = {}
    for f in fillers:
        parts = [norm_word(x) for x in f.split()]
        parts = [x for x in parts if x]
        if not parts:
            continue
        hits: list[int] = []
        n = len(parts)
        i = 0
        while i + n <= len(normed):
            if normed[i:i + n] == parts:
                hits.extend(range(i, i + n))
                i += n
            else:
                i += 1
        out[f] = hits
    return out


# ---------------- неудачные дубли ----------------

RETAKE_MAX_WORDS = 30        # неудачная попытка длиннее — это уже не дубль, а другая мысль
RETAKE_MAX_S = 25.0
RETAKE_SIMILAR = 0.6         # насколько попытка похожа на начало повтора
RETAKE_GAP_S = 0.3           # пауза, после которой слово считается началом фразы


@dataclass
class Retake:
    start: int               # первое слово неудачной попытки
    end: int                 # слово, с которого начат повтор (не входит в попытку)

    @property
    def words(self) -> range:
        return range(self.start, self.end)


def phrase_starts(words: list[Word]) -> list[int]:
    """Где начинаются фразы: после точки, запятой и т. п. или после паузы."""
    out = [0] if words else []
    for k in range(1, len(words)):
        prev = words[k - 1]
        if prev.text.rstrip().endswith((".", "!", "?", "…", ",", ";", ":", "—", "-")) or \
                words[k].start - prev.end > RETAKE_GAP_S:
            out.append(k)
    return out


def find_retakes(words: list[Word]) -> list[Retake]:
    """Неудачные дубли: фразу начали, сбились и сказали заново с тех же слов.

    «Сегодня мы погово… Сегодня мы поговорим о камере» → неудачная попытка — «Сегодня мы погово…»
    (всё от её начала до повтора). Повтор узнаём по тем же первым словам (двум, а если они
    совсем короткие, вроде «и в», — трём) и по тому, что попытка похожа на начало повтора.
    Несколько попыток подряд — каждая отдельно, остаётся последняя.
    """
    import difflib

    normed = [norm_word(w.text) for w in words]
    starts = phrase_starts(words)
    out: list[Retake] = []
    for n, i in enumerate(starts):
        m = 2 if any(len(x) > 2 for x in normed[i:i + 2]) else 3
        head = normed[i:i + m]
        if len(head) < m or not all(head):
            continue
        for j in starts[n + 1:]:
            if j - i > RETAKE_MAX_WORDS or words[j].start - words[i].start > RETAKE_MAX_S:
                break
            if j < i + m or normed[j:j + m] != head:
                continue
            attempt = normed[i:j]
            again = normed[j:j + len(attempt)]
            if difflib.SequenceMatcher(None, attempt, again, autojunk=False).ratio() >= RETAKE_SIMILAR:
                out.append(Retake(i, j))
            break
    return out


def mark_spans(sw: SourceWords, marked: set[int], cuts: dict,
               env: np.ndarray | None = None) -> list[tuple[float, float]]:
    """Где во времени исходника лежат помеченные слова (подряд идущие — одним куском).

    Края — в тишине между словами: не дальше запаса от слова и не дальше середины паузы.
    """
    words, dur = sw.words, sw.duration
    pad = max(0.03, float(cuts["pad"]))
    out: list[tuple[float, float]] = []
    idx = sorted(i for i in marked if 0 <= i < len(words))
    k = 0
    while k < len(idx):
        first = last = idx[k]
        while k + 1 < len(idx) and idx[k + 1] == last + 1:
            k += 1
            last = idx[k]
        k += 1
        lo = words[first - 1].end if first > 0 else 0.0
        hi = words[last + 1].start if last + 1 < len(words) else dur
        a = words[first].start - min(pad, (words[first].start - lo) / 2)
        b = words[last].end + min(pad, (hi - words[last].end) / 2)
        a = _snap(env, a, lo, words[first].start) if a > 0 else 0.0
        b = _snap(env, b, words[last].end, hi) if b < dur else dur
        out.append((round(max(0.0, a), 3), round(min(dur, b), 3)))
    return out


def add_ranges(ranges: list[tuple[float, float]], extra: list[tuple[float, float]],
               dur: float) -> list[tuple[float, float]]:
    """Добавить куски (кадры без речи от автомонтажа) к оставленным: объединить и упорядочить."""
    if not extra:
        return ranges
    allr = sorted([*ranges, *((max(0.0, a), min(dur, b)) for a, b in extra if b > a)])
    out: list[tuple[float, float]] = []
    for a, b in allr:
        if out and a <= out[-1][1] + 0.05:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((round(a, 3), round(b, 3)))
    return out


def broll_in_gap(cuts: dict, src: str, a: float, b: float) -> float:
    """Сколько секунд кадров автомонтажа оставлено внутри паузы [a, b]."""
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in cuts.get("broll", {}).get(src, []))


EDGE_MATCH_S = 0.02


def apply_edges(ranges: list[tuple[float, float]], edges: list, dur: float) -> list[tuple]:
    """Ручные правки краёв кусков (растянули или укоротили кусок на ленте).

    edges: [[край, как его посчитала программа; куда его передвинули]]. Возвращает
    [(начало, конец, исходное начало, исходный конец)] — исходные края нужны, чтобы
    запомнить следующую правку; у слившихся кусков — от первого и последнего."""
    def moved(t: float) -> float:
        for raw, to in edges:
            if abs(float(raw) - t) <= EDGE_MATCH_S:
                return float(to)
        return t

    adj = []
    for a, b in ranges:
        a2, b2 = max(0.0, moved(a)), min(dur, moved(b))
        if b2 - a2 >= MIN_RANGE_S:
            adj.append([round(a2, 3), round(b2, 3), a, b])
    adj.sort()
    out: list[list] = []
    for r in adj:
        if out and r[0] <= out[-1][1] + 0.01:
            if r[1] >= out[-1][1]:
                out[-1][1], out[-1][3] = r[1], r[3]
        else:
            out.append(r)
    return [tuple(r) for r in out]


def set_edge(cuts: dict, src: str, raw: float, to: float) -> None:
    """Запомнить: край куска raw (как его посчитала программа) теперь стоит в to."""
    edges = cuts.setdefault("edges", {}).setdefault(src, [])
    edges[:] = [e for e in edges if abs(float(e[0]) - raw) > EDGE_MATCH_S]
    if abs(to - raw) > 1e-3:
        edges.append([round(raw, 3), round(to, 3)])


def split_ranges(ranges: list[tuple[float, float]], muted: list[tuple[float, float]],
                 hidden: list[tuple[float, float]]) -> list[tuple[float, float, bool, bool]]:
    """Оставленные куски → куски с пометками: (начало, конец, без звука, без картинки)."""
    def inside(t: float, spans) -> bool:
        return any(a <= t < b for a, b in spans)

    out: list[tuple[float, float, bool, bool]] = []
    for a, b in ranges:
        cuts_at = sorted({a, b, *(x for sp in (muted, hidden) for s in sp for x in s if a < x < b)})
        for x, y in zip(cuts_at, cuts_at[1:]):
            if y - x < 0.02:
                continue
            mid = (x + y) / 2
            m, h = inside(mid, muted), inside(mid, hidden)
            if out and out[-1][1] == x and out[-1][2:] == (m, h):
                out[-1] = (out[-1][0], y, m, h)
            else:
                out.append((round(x, 3), round(y, 3), m, h))
    return out


def output_time(project, src: str, t: float) -> float | None:
    """Момент t исходника src → время в готовом ролике (None — этот момент вырезан)."""
    acc = 0.0
    for c in project.clips:
        if c.src == src and c.in_s - 1e-3 <= t < c.out_s:
            return acc + max(0.0, t - c.in_s) / c.speed
        acc += c.duration
    return None


def rebuild_clips(project, store: TranscriptStore) -> None:
    """Пересобрать фрагменты проекта по расшифровке и пометкам (для проектов «монтаж по тексту»)."""
    from glimpsy.editor.project import Clip, new_id

    cuts = cuts_of(project)
    deleted_all = cuts.get("deleted", {})
    clips = []
    keys: dict[str, tuple] = {}
    for s in cuts.get("sources", []):
        src = s["src"]
        sw = store.get(src)
        if sw is None:
            pieces = [(0.0, float(s["duration"]), False, False)]
        else:
            env = store.envelope(src)
            rngs = kept_ranges(sw, set(deleted_all.get(src, [])), set(), cuts, env, pause_marks(cuts, src))
            rngs = add_ranges(rngs, [(float(a), float(b)) for a, b in cuts.get("broll", {}).get(src, [])],
                              float(s["duration"]))
            muted = set(cuts.get("muted", {}).get(src, []))
            mspans = mark_spans(sw, muted, cuts, env)
            hspans = mark_spans(sw, set(cuts.get("hidden", {}).get(src, [])), cuts, env)
            pieces = []
            for a2, b2, ra, rb in apply_edges(rngs, cuts.get("edges", {}).get(src, []), float(s["duration"])):
                part = split_ranges([(a2, b2)], mspans, hspans)
                for n, (a, b, m, h) in enumerate(part):
                    # края, которые можно тянуть на ленте: (как их посчитала программа) — чтобы запомнить правку
                    pieces.append((a, b, m, h, ra if n == 0 else None, rb if n == len(part) - 1 else None))
        for a, b, m, h, *raw in pieces:
            c = Clip(new_id(), "video", src, float(s["duration"]), a, b, muted=m, hidden=h,
                     has_audio=bool(s.get("has_audio", True)), width=int(s.get("width", 0)),
                     height=int(s.get("height", 0)), label=s.get("label", Path(src).name),
                     frames={k: list(v) for k, v in s.get("frames", {}).items()})
            clips.append(c)
            if raw:
                keys[c.id] = (src, raw[0], raw[1], a, b)
    project.clips = clips
    project.edge_keys = keys


def source_time(project, t: float) -> tuple[str, float] | None:
    """Момент ролика → (видео, время внутри исходника)."""
    idx, local = project.locate(t)
    if idx is None:
        return None
    c = project.clips[idx]
    return c.src, c.in_s + local * c.speed


def word_at(sw: SourceWords, t: float) -> int:
    """Номер слова, которое звучит (или последним прозвучало) в момент t исходника."""
    starts = [w.start for w in sw.words]
    return max(0, bisect_right(starts, t) - 1) if starts else -1


# ---------------- распознавание ----------------

class TranscribeError(RuntimeError):
    pass


def extract_audio(ffmpeg: str, src: Path, wav: Path) -> None:
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn",
                        "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, **subprocess_flags())
    if r.returncode != 0 or not wav.exists():
        raise TranscribeError("Не удалось достать звук из видео: " + r.stderr.decode("utf-8", "replace")[-400:])


def whisper_threads() -> int:
    return max(2, min(8, os.cpu_count() or 4))


DTW_PRESETS = {"tiny": "tiny", "base": "base", "small": "small", "turbo": "large.v3.turbo"}


def word_command(exe: str, model: Path, wav: Path, out_base: Path, language: str, model_key: str = "") -> list[str]:
    from glimpsy.editor.subtitles import _short_path

    cmd = [exe, "-m", _short_path(model), "-f", _short_path(wav), "-l", language, "-ojf",
           "-of", _short_path(out_base.parent) + "/" + out_base.name, "-pp", "-np", "-sns", "-t", str(whisper_threads())]
    if model_key in DTW_PRESETS:
        # точное время слов — выравнивание по звуку (DTW); с ускоренным вниманием оно не работает
        cmd += ["-dtw", DTW_PRESETS[model_key], "-nfa"]
    if language in ("ru", "auto"):
        cmd += ["--prompt", PROMPT_RU]
    # без детектора речи: с ним время отдельных слов у whisper.cpp сбивается
    return cmd


def transcribe_source(ffmpeg: str, src: Path, src_key: str, duration: float, model_key: str, language: str,
                      store: TranscriptStore, progress: Callable[[float, str], None] | None = None,
                      cancel: threading.Event | None = None, retime: bool = False) -> SourceWords:
    """Распознать одно видео по словам и сохранить в проект."""
    from glimpsy.editor import subtitles as subs

    progress = progress or (lambda f, t: None)
    cancel = cancel or threading.Event()
    exe = subs.whisper_exe()
    if not exe:
        raise TranscribeError("В этой сборке нет программы распознавания речи (whisper-cli).")
    if not subs.model_ready(model_key):
        raise TranscribeError("Модель распознавания ещё не скачана.")
    work = store.dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "speech.wav"
    try:
        progress(0.01, "Достаю звук…")
        extract_audio(ffmpeg, src, wav)
        if cancel.is_set():
            raise subs.Cancelled()
        samples = subs.read_wav(wav)
        env = envelope(samples)
        out_base = work / "words"
        (work / "words.json").unlink(missing_ok=True)
        cmd = word_command(exe, subs.model_path(model_key), wav, out_base, language, model_key)
        log.info("whisper (слова): %s", " ".join(cmd))
        progress(0.03, "Распознаю речь…")
        subs.run_whisper(cmd, lambda f: progress(0.03 + 0.95 * f, "Распознаю речь…"), cancel)
        js = work / "words.json"
        if not js.exists():
            raise TranscribeError("Программа распознавания не вернула результат.")
        words, lang = parse_whisper_words(js.read_text(encoding="utf-8", errors="replace"))
        words = refine_words(words, env)
        old = store.get(src_key) if retime else None
        if old is not None:
            # уточняем только время: текст, номера слов и все пометки монтажа остаются как были
            moved = retime_words(old.words, words, duration)
            sw = SourceWords(src_key, duration, refine_times(moved, env), old.language, old.model)
            log.info("Время слов %s уточнено: %d слов", src.name, len(moved))
        else:
            sw = SourceWords(src_key, duration, words, lang or language, model_key)
            log.info("Расшифровка %s: %d слов, язык %s", src.name, len(words), lang)
        store.put(sw, env)
        return sw
    finally:
        wav.unlink(missing_ok=True)


def retime_words(old: list[Word], new: list[Word], duration: float) -> list[Word]:
    """Перенести время из новой расшифровки на старые слова (те же слова по тексту — то же время);
    слова, которых в новой нет, — равномерно между ближайшими найденными."""
    import difflib

    a = [norm_word(w.text) for w in old]
    b = [norm_word(w.text) for w in new]
    times: list[tuple[float, float] | None] = [None] * len(old)
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            nw = new[blk.b + k]
            times[blk.a + k] = (nw.start, nw.end)
    out: list[Word] = []
    k = 0
    while k < len(old):
        if times[k] is not None:
            out.append(Word(old[k].text, times[k][0], times[k][1], old[k].p))
            k += 1
            continue
        j = k
        while j < len(old) and times[j] is None:
            j += 1
        lo = out[-1].end if out else (old[k].start if j >= len(old) else 0.0)
        hi = times[j][0] if j < len(old) else max(lo, min(duration, old[j - 1].end))
        if hi <= lo:                                   # некуда поставить — оставляем прежнее время
            for m in range(k, j):
                out.append(Word(old[m].text, old[m].start, old[m].end, old[m].p))
        else:
            step = (hi - lo) / (j - k)
            for m in range(k, j):
                a0 = lo + (m - k) * step
                out.append(Word(old[m].text, round(a0, 3), round(a0 + step * 0.9, 3), old[m].p))
        k = j
    return out


def refine_times(words: list[Word], env: np.ndarray | None) -> list[Word]:
    """Уточнить по громкости, не выбрасывая слов (номера слов должны сохраниться)."""
    kept = refine_words(words, env)
    if len(kept) == len(words):
        return kept
    return words


# ---------------- субтитры .srt ----------------

def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def output_words(project, store: TranscriptStore) -> list[tuple[float, float, str]]:
    """Оставленные слова во времени готового ролика: [(начало, конец, текст)]."""
    cuts = cuts_of(project)
    deleted_all = cuts.get("deleted", {})
    fixed_all = cuts.get("word_text", {})
    out = []
    t0 = 0.0
    for c in project.clips:
        sw = store.get(c.src)
        if sw is not None:
            gone = set(deleted_all.get(c.src, [])) | set(cuts.get("muted", {}).get(c.src, []))
            fixed = fixed_all.get(c.src, {})
            for i, w in enumerate(sw.words):
                if i in gone or w.end <= c.in_s or w.start >= c.out_s:
                    continue
                a = t0 + (max(w.start, c.in_s) - c.in_s) / c.speed
                b = t0 + (min(w.end, c.out_s) - c.in_s) / c.speed
                text = fixed.get(str(i), w.text)
                if text:                                    # пустое — слово убрали из текста
                    out.append((a, b, text))
        t0 += c.duration
    return out


def word_text(cuts: dict, src: str, i: int, text: str) -> str:
    """Слово с учётом исправления, сделанного в тексте."""
    return cuts.get("word_text", {}).get(src, {}).get(str(i), text)


def cues(words: list[tuple[float, float, str]], max_chars: int = 42,
         max_gap: float = 0.8) -> list[tuple[float, float, str]]:
    """Слова → фразы субтитров: строка до max_chars знаков, новая — после паузы или конца предложения."""
    groups: list[list[tuple[float, float, str]]] = []
    for w in words:
        cur = groups[-1] if groups else None
        if cur is None or w[0] - cur[-1][1] > max_gap or \
                len(" ".join(x[2] for x in cur)) + 1 + len(w[2]) > max_chars or \
                re.search(r"[.!?…]$", cur[-1][2]):
            groups.append([w])
        else:
            cur.append(w)
    out = []
    for n, g in enumerate(groups):
        a = g[0][0]
        b = max(g[-1][1], a + 0.5)
        if n + 1 < len(groups):
            b = min(b, groups[n + 1][0][0])
        out.append((a, b, " ".join(x[2] for x in g)))
    return out


def srt_text(lines: list[tuple[float, float, str]]) -> str:
    """Строки субтитров [(начало, конец, текст)] → файл .srt."""
    return "\n".join(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n" for n, (a, b, text) in enumerate(lines, 1))


def make_srt(words: list[tuple[float, float, str]], max_chars: int = 42, max_gap: float = 0.8) -> str:
    """Слова → файл субтитров .srt."""
    return srt_text(cues(words, max_chars, max_gap))

