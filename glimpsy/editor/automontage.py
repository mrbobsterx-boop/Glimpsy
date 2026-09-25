"""Автомонтаж: одна кнопка делает ролик динамичнее.

Что делает (каждый пункт можно выключить):
  а) автозум к курсору — не везде, а время от времени (примерно каждый 2–3-й фрагмент,
     в первую очередь там, где были клики), чтобы движение не утомляло;
  б) подсветка кликов;
  в) темп: скучные фрагменты ускоряются, активные — чуть замедляются;
  г) пустые фрагменты (ничего не происходило) убираются;
  д) «наезд» камеры на важных моментах (⭐);
  е) для 9:16 — кадр едет за курсором (на важных моментах — крупнее);
  ж) склейки попадают в долю музыки (если музыка добавлена).

Всё — обычные правки проекта: их можно поменять руками или отменить одним Ctrl+Z.
"""

from __future__ import annotations

import logging
import math
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from glimpsy.editor.project import MAX_SPEED, MIN_SPEED, Clip, Project
from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)


@dataclass
class Options:
    zoom: bool = True          # а
    clicks: bool = True        # б
    pace: bool = True          # в
    drop_empty: bool = True    # г
    pushin: bool = True        # д
    follow: bool = True        # е
    beats: bool = True         # ж


@dataclass
class Report:
    zoomed: int = 0
    pushed: int = 0
    clicks: int = 0
    faster: int = 0
    slower: int = 0
    dropped: int = 0
    followed: int = 0
    beat_cuts: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        for n, text in ((self.zoomed, "автозум"), (self.pushed, "наезд"), (self.clicks, "клики"),
                        (self.faster, "быстрее"), (self.slower, "медленнее"), (self.dropped, "убрано пустых"),
                        (self.followed, "9:16 за курсором"), (self.beat_cuts, "склеек в долю")):
            if n:
                parts.append(f"{text}: {n}")
        return ", ".join(parts) if parts else "менять нечего"


# ---------------- оценка фрагментов ----------------

def _in_clip(points: list, c: Clip) -> list:
    return [p for p in points if c.in_s <= p[0] <= c.out_s]


def activity(c: Clip) -> float | None:
    """Насколько «живой» фрагмент: клики, путь курсора и оценка при записи. None — данных нет."""
    if c.kind != "video" or not (c.cursor or c.clicks or c.score >= 0):
        return None
    src = max(0.1, c.out_s - c.in_s)
    clicks = len(_in_clip(c.clicks, c)) / src
    pts = _in_clip(c.cursor, c)
    path = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts, pts[1:])) / src
    return clicks * 0.6 + min(path, 1.5) * 0.6 + max(c.score, 0.0)


def voiced(c: Clip) -> bool:
    """Фрагмент со включённым звуком (речь) — его темп и длину не трогаем."""
    return c.kind == "video" and c.has_audio and not c.muted


def is_empty(c: Clip) -> bool:
    """Ничего не происходило: ни кликов, ни движения курсора, и при записи оценка почти ноль."""
    if c.kind != "video" or c.priority or voiced(c) or c.score < 0:
        return False
    pts = _in_clip(c.cursor, c)
    path = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts, pts[1:]))
    return c.score < 0.04 and not _in_clip(c.clicks, c) and path < 0.03


# ---------------- доли музыки ----------------

def detect_beats(ffmpeg: str, path: Path, start: float = 0.0, length: float = 600.0) -> tuple[list[float], float]:
    """Доли (удары) музыки: времена в секундах от start и длительность одной доли.

    Громкость считаем кусочками по ~23 мс, всплески громкости — «атаки» звука; темп — самый
    сильный повтор атак (60–180 ударов в минуту), затем подбираем, где стоит первая доля.
    """
    sr, hop = 11025, 256
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                        "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True, **subprocess_flags())
    x = np.frombuffer(r.stdout, np.float32)
    if len(x) < sr * 3:
        return [], 0.0
    n = len(x) // hop
    energy = np.log1p(100 * np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)))
    onset = np.maximum(0.0, np.diff(energy, prepend=energy[0]))
    onset -= onset.mean()
    fps = sr / hop
    lo, hi = int(fps * 60 / 180), int(fps * 60 / 60)
    ac = np.correlate(onset, onset, "full")[len(onset) - 1:]
    if hi >= len(ac):
        return [], 0.0
    # лёгкий перевес темпов около 120 BPM — так реже путаются половинные и двойные темпы
    lags = np.arange(lo, hi + 1)
    weight = np.exp(-0.5 * (np.log2(60 * fps / lags / 120) / 0.9) ** 2)
    lag = int(lags[np.argmax(ac[lo:hi + 1] * weight)])
    phases = [onset[p::lag].sum() for p in range(lag)]
    phase = int(np.argmax(phases))
    period = lag / fps
    beats = [round((phase + k * lag) / fps, 3) for k in range((n - phase) // lag + 1)]
    return beats, period


def music_beats(ffmpeg: str, project: Project) -> tuple[list[float], float]:
    """Доли музыки проекта во времени ролика (с учётом места начала и повтора трека)."""
    m = project.music
    if m is None:
        return [], 0.0
    total = project.total
    usable = max(0.0, m.duration - m.in_s)
    beats, period = detect_beats(ffmpeg, project.dir / m.src, m.in_s, min(usable, total + 2))
    if not beats:
        return [], 0.0
    if m.loop and usable < total:
        # трек повторяется: каждый повтор начинается снова с m.in_s (см. музыку в экспорте)
        again = detect_beats(ffmpeg, project.dir / m.src, 0.0, min(m.duration, total))[0] or beats
        out, t0 = list(beats), usable
        while t0 < total:
            out += [t0 + b for b in again if t0 + b <= total + 1]
            t0 += m.duration
        beats = out
    return [b for b in beats if b <= total + 1], period


# ---------------- сам автомонтаж ----------------

def apply(project: Project, opts: Options, beats: list[float] | None = None, period: float = 0.0) -> Report:
    rep = Report()
    clips = project.clips

    # г) пустые — убираем (не больше трети и так, чтобы осталось хотя бы 2 фрагмента)
    if opts.drop_empty:
        empty = [c for c in clips if is_empty(c)]
        limit = min(len(clips) // 3, max(0, len(clips) - 2))
        for c in sorted(empty, key=lambda c: c.score)[:limit]:
            clips.remove(c)
            rep.dropped += 1

    # в) темп по активности (относительно «обычной» активности этого ролика)
    acts = {c.id: activity(c) for c in clips}
    known = [a for a in acts.values() if a is not None]
    median = statistics.median(known) if known else 0.0
    for c in clips:
        if c.kind != "video" or not opts.pace:
            continue
        if not c.base_speed:
            c.base_speed = c.speed
        a = acts.get(c.id)
        factor = 1.0
        if a is not None and median > 0 and not voiced(c):
            r = a / median
            factor = 1.8 if r < 0.35 else 1.35 if r < 0.7 else 0.8 if r > 1.8 else 1.0
            if c.priority:
                factor = min(factor, 1.0)        # важное не ускоряем
        c.speed = max(MIN_SPEED, min(MAX_SPEED, c.base_speed * factor))
        rep.faster += factor > 1
        rep.slower += factor < 1

    # а) и д) движение для 16:9: наезд на важных, автозум время от времени
    last_zoom = -10
    for i, c in enumerate(clips):
        if c.kind != "video" or not c.cursor:
            continue
        if opts.clicks and c.clicks:
            c.click_fx = True
            rep.clicks += 1
        if opts.pushin and c.priority:
            c.set_motion("16:9", "pushin")
            rep.pushed += 1
            last_zoom = i
            continue
        if not opts.zoom:
            continue
        has_clicks = bool(_in_clip(c.clicks, c))
        if (has_clicks and i - last_zoom >= 2) or i - last_zoom >= 3:
            c.set_motion("16:9", "autozoom")
            rep.zoomed += 1
            last_zoom = i
        else:
            c.set_motion("16:9", "none")

    # е) 9:16 — кадр за курсором; на важных — крупнее
    if opts.follow:
        for c in clips:
            if c.kind == "video" and c.cursor:
                c.set_motion("9:16", "follow_zoom" if c.priority and opts.pushin else "follow")
                rep.followed += 1

    # ж) склейки — в долю музыки
    if opts.beats and beats and period > 0:
        rep.beat_cuts = align_to_beats(clips, beats, period)
    elif opts.beats and project.music is None:
        rep.notes.append("склейки под музыку — добавьте музыку")
    return rep


def align_to_beats(clips: list[Clip], beats: list[float], period: float) -> int:
    """Сдвигает концы фрагментов на ближайшую долю — скоростью (видео) или длиной (фото)."""
    moved = 0
    t = 0.0
    for c in clips[:-1]:
        dur = c.duration
        end = t + dur
        if voiced(c) or dur <= 0:
            t = end
            continue
        near = [b for b in beats if abs(b - end) <= period * 0.6 and b - t >= max(0.8, dur * 0.6)]
        if not near:
            t = end
            continue
        target = min(near, key=lambda b: abs(b - end))
        new_dur = target - t
        if c.kind == "image":
            c.out_s = c.in_s + new_dur
        else:
            speed = (c.out_s - c.in_s) / new_dur
            if not (MIN_SPEED <= speed <= MAX_SPEED) or not 0.65 <= speed / c.speed <= 1.5:
                t = end
                continue
            c.speed = speed
        moved += abs(target - end) > 0.01
        t = t + c.duration
    return moved
