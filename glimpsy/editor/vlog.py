"""Автомонтаж влогов (походы, прогулки, магазины, дорога): из длинного видео — готовый ролик.

Что смотрим (всё прямо на компьютере):
  * речь — по расшифровке: где вы говорите, сколько и как громко;
  * картинка — по опорным кадрам видео (их декодировать быстро): резкость, яркость и
    насколько меняется кадр (пейзаж плывёт — интересно, кадр трясётся или смазан — нет).

Что делаем:
  * «Влог» (16:9) — оставляем всю речь (длинные паузы вырезаются), а из мест без речи
    берём лучшие кадры по несколько секунд — как перебивки между фразами;
  * «Короткий ролик» (9:16) — самые живые фразы и короткие красивые кадры, всего на
    15–60 секунд, кадр по вертикали, субтитры включены.

Результат — обычные пометки монтажа по тексту: слова можно вернуть, кадры убрать,
отменить всё одним Ctrl+Z.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

SIDE = 96                          # опорные кадры уменьшаем до 96×96 — для оценки хватает
FORMATS = {
    "vlog": ("Влог 16:9 — YouTube", "16:9"),
    "short": ("Короткий ролик 9:16 — Shorts, Reels, TikTok", "9:16"),
}


@dataclass
class Visual:
    """Оценка картинки одного видео по опорным кадрам."""
    t: np.ndarray                  # время кадров, с
    sharp: np.ndarray              # резкость (больше — чётче)
    bright: np.ndarray             # яркость 0…1
    change: np.ndarray             # насколько кадр изменился с прошлого (в секунду)

    def save(self, f: Path) -> None:
        np.savez_compressed(f, t=self.t, sharp=self.sharp, bright=self.bright, change=self.change)

    @classmethod
    def load(cls, f: Path) -> "Visual":
        d = np.load(f)
        return cls(d["t"], d["sharp"], d["bright"], d["change"])


@dataclass
class Plan:
    """Что оставить: удалить фразы (номера слов) и добавить кадры без речи [(начало, конец)]."""
    deleted: dict[str, set[int]] = field(default_factory=dict)
    broll: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    length: float = 0.0
    talk: float = 0.0
    shots: int = 0


# ---------------- картинка ----------------

def frame_stats(frames: np.ndarray, times: np.ndarray) -> Visual:
    """frames: [n, SIDE, SIDE] (0…255) → резкость, яркость, изменение."""
    f = frames.astype(np.float32) / 255.0
    lap = (4 * f[:, 1:-1, 1:-1] - f[:, :-2, 1:-1] - f[:, 2:, 1:-1] - f[:, 1:-1, :-2] - f[:, 1:-1, 2:])
    sharp = lap.reshape(len(f), -1).var(axis=1) if len(f) else np.zeros(0, np.float32)
    bright = f.reshape(len(f), -1).mean(axis=1) if len(f) else np.zeros(0, np.float32)
    change = np.zeros(len(f), np.float32)
    if len(f) > 1:
        diff = np.abs(f[1:] - f[:-1]).reshape(len(f) - 1, -1).mean(axis=1)
        dt = np.maximum(0.2, np.diff(times))
        change[1:] = diff / dt
        change[0] = change[1]
    return Visual(times.astype(np.float32), sharp.astype(np.float32), bright.astype(np.float32), change)


def analyze_visual(ffmpeg: str, src: Path, duration: float, progress: Callable[[float], None] | None = None,
                   cancel: threading.Event | None = None) -> Visual:
    """Опорные кадры видео → оценка картинки. Если опорных кадров мало (редкие у некоторых камер) —
    берём кадр раз в секунду."""
    progress = progress or (lambda f: None)
    cancel = cancel or threading.Event()
    vis = _decode(ffmpeg, src, duration, ["-skip_frame", "nokey"], "", progress, cancel)
    if len(vis.t) < max(3, duration / 4):
        vis = _decode(ffmpeg, src, duration, [], "fps=1,", progress, cancel)
    return vis


def _decode(ffmpeg: str, src: Path, duration: float, pre: list[str], vf: str, progress, cancel) -> Visual:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", *pre, "-i", str(src), "-an", "-sn",
           "-vf", f"{vf}scale={SIDE}:{SIDE},format=gray,showinfo", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **subprocess_flags())
    times: list[float] = []

    def read_err() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            m = re.search(rb"pts_time:\s*([-\d.]+)", raw)
            if m:
                times.append(float(m.group(1)))

    th = threading.Thread(target=read_err, daemon=True)
    th.start()
    size = SIDE * SIDE
    chunks: list[bytes] = []
    assert proc.stdout is not None
    while True:
        if cancel.is_set():
            proc.kill()
            proc.wait()
            raise RuntimeError("cancelled")
        buf = proc.stdout.read(size * 32)
        if not buf:
            break
        chunks.append(buf)
        if times and duration > 0:
            progress(min(1.0, times[-1] / duration))
    proc.wait()
    th.join(timeout=5)
    data = b"".join(chunks)
    n = min(len(data) // size, len(times))
    frames = np.frombuffer(data[: n * size], np.uint8).reshape(n, SIDE, SIDE)
    return frame_stats(frames, np.array(times[:n], np.float32))


def _pct(x: np.ndarray) -> np.ndarray:
    """Место каждого значения среди всех (0…1)."""
    if len(x) == 0:
        return x
    order = np.argsort(np.argsort(x))
    return order / max(1, len(x) - 1)


def shot_scores(vis: Visual) -> np.ndarray:
    """Насколько хорош каждый опорный кадр как «красивый кадр» (0…1)."""
    if len(vis.t) == 0:
        return np.zeros(0, np.float32)
    sharp = _pct(vis.sharp)
    ch = _pct(vis.change)
    interest = np.where(ch > 0.92, 0.2, 0.3 + 0.7 * ch)          # совсем сильная смена — тряска/рывок
    light = np.where((vis.bright > 0.12) & (vis.bright < 0.9), 1.0, 0.25)
    shaky = (ch > 0.85) & (sharp < 0.35)                           # двигается и смазано
    return (0.5 * sharp + 0.3 * interest + 0.2 * light) * np.where(shaky, 0.4, 1.0) * light


def best_window(vis: Visual, scores: np.ndarray, a: float, b: float, length: float) -> tuple[float, float, float] | None:
    """Лучший отрезок длины length внутри [a, b]: (начало, конец, оценка)."""
    if b - a < length * 0.6 or len(vis.t) == 0:
        return None
    length = min(length, b - a)
    best = None
    step = max(0.5, length / 4)
    t = a
    while t + length <= b + 1e-6:
        sel = (vis.t >= t - 0.5) & (vis.t <= t + length + 0.5)
        s = float(scores[sel].mean()) if sel.any() else 0.3
        if best is None or s > best[2]:
            best = (t, t + length, s)
        t += step
    return best


# ---------------- речь ----------------

@dataclass
class Sentence:
    src: str
    first: int
    last: int
    a: float
    b: float
    text: str
    score: float = 0.0


def sentences(src: str, words: list, gone: set[int]) -> list[Sentence]:
    """Фразы одного видео (без уже удалённых слов)."""
    out: list[Sentence] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        if i in gone:
            continue
        if cur and (re.search(r"[.!?…]$", words[cur[-1]].text) or w.start - words[cur[-1]].end > 1.2):
            out.append(_sent(src, words, cur))
            cur = []
        cur.append(i)
    if cur:
        out.append(_sent(src, words, cur))
    return out


def _sent(src: str, words: list, idx: list[int]) -> Sentence:
    return Sentence(src, idx[0], idx[-1], words[idx[0]].start, words[idx[-1]].end,
                    " ".join(words[i].text for i in idx))


def score_sentences(sents: list[Sentence], env: np.ndarray | None, frame_s: float = 0.01) -> None:
    """Насколько фраза «живая»: громкость, темп, эмоция (! ?), удобная длина."""
    if not sents:
        return
    loud = []
    rate = []
    for s in sents:
        if env is not None and len(env):
            seg = env[int(s.a / frame_s):max(int(s.a / frame_s) + 1, int(s.b / frame_s))]
            loud.append(float(seg.mean()) if len(seg) else 0.0)
        else:
            loud.append(0.0)
        rate.append(len(s.text.split()) / max(0.5, s.b - s.a))
    lp, rp = _pct(np.array(loud)), _pct(np.array(rate))
    for k, s in enumerate(sents):
        dur = s.b - s.a
        fit = 1.0 if 1.5 <= dur <= 9 else 0.5
        emo = 0.15 if re.search(r"[!?]", s.text) else 0.0
        s.score = 0.4 * lp[k] + 0.3 * rp[k] + 0.3 * fit + emo


# ---------------- план монтажа ----------------

def plan(fmt: str, sources: list[dict], words_of: Callable[[str], list | None], env_of: Callable[[str], np.ndarray | None],
         vis_of: Callable[[str], Visual | None], gone_of: Callable[[str], set[int]],
         target_s: float = 0.0) -> Plan:
    """Собрать план: что из речи оставить и какие кадры без речи добавить.

    target_s — желаемая длина (0 — для влога «как получится»; для короткого — 45 с)."""
    p = Plan()
    if fmt == "short":
        target_s = target_s or 45.0
    all_sents: list[Sentence] = []
    shots: list[tuple[str, float, float, float]] = []      # (видео, начало, конец, оценка)
    for s in sources:
        src, dur = s["src"], float(s["duration"])
        words = words_of(src) or []
        gone = gone_of(src)
        sents = sentences(src, words, gone)
        score_sentences(sents, env_of(src))
        all_sents += sents
        vis = vis_of(src)
        if vis is None or len(vis.t) == 0:
            continue
        sc = shot_scores(vis)
        # места без речи
        edges = [0.0] + [x for se in sents for x in (se.a, se.b)] + [dur]
        gaps = [(edges[k] + 0.3, edges[k + 1] - 0.3) for k in range(0, len(edges) - 1, 2)]
        shot_len = 2.0 if fmt == "short" else 4.0
        for a, b in gaps:
            if b - a < shot_len * 0.75:
                continue
            n = 1 if b - a < 20 else min(3, int((b - a) // 12))         # длинная прогулка — пара кадров
            part = (b - a) / n
            for k in range(n):
                w = best_window(vis, sc, a + k * part, a + (k + 1) * part, shot_len)
                if w is not None and w[2] > 0.35:
                    shots.append((src, w[0], w[1], w[2]))
    talk = sum(s.b - s.a for s in all_sents)
    keep_sents = list(all_sents)
    keep_shots = list(shots)
    if fmt == "short":
        # ≈ 65 % — лучшие фразы, остальное — кадры; порядок — как было в жизни
        budget = target_s * (0.65 if shots else 1.0)
        keep_sents = []
        for s in sorted(all_sents, key=lambda x: -x.score):
            if sum(x.b - x.a for x in keep_sents) + (s.b - s.a) <= budget + min(2.0, budget * 0.1):
                keep_sents.append(s)
        left = target_s - sum(x.b - x.a for x in keep_sents)
        keep_shots = []
        for sh in sorted(shots, key=lambda x: -x[3]):
            if left < 1.0:
                break
            keep_shots.append(sh)
            left -= sh[2] - sh[1]
    elif target_s > 0:
        # влог с ограничением длины: сначала меньше кадров, потом — самые слабые фразы
        total = talk + sum(b - a for _s, a, b, _sc in shots)
        for sh in sorted(shots, key=lambda x: x[3]):
            if total <= target_s:
                break
            keep_shots.remove(sh)
            total -= sh[2] - sh[1]
        for s in sorted(all_sents, key=lambda x: x.score):
            if total <= target_s:
                break
            keep_sents.remove(s)
            total -= s.b - s.a
    kept = {(s.src, s.first) for s in keep_sents}
    for s in all_sents:
        if (s.src, s.first) not in kept:
            p.deleted.setdefault(s.src, set()).update(range(s.first, s.last + 1))
    for src, a, b, _sc in keep_shots:
        p.broll.setdefault(src, []).append((round(a, 2), round(b, 2)))
    for v in p.broll.values():
        v.sort()
    p.talk = sum(s.b - s.a for s in keep_sents)
    p.shots = len(keep_shots)
    p.length = p.talk + sum(b - a for _s, a, b, _sc in keep_shots)
    return p
