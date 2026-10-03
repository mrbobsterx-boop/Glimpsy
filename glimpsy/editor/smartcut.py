"""Быстрое сохранение нарезки: видео не пересчитывается целиком.

Видео (H.264) хранится группами кадров: полный «ключевой» кадр и за ним изменения.
Кусок от одного ключевого кадра до другого можно переложить в новый файл как есть —
без распаковки и сжатия. Пересчитываются только края кусков: от места разреза до
ближайшего ключевого кадра (обычно доли секунды). Поэтому сохранение — в разы быстрее,
а нетронутые кадры остаются в исходном качестве.

Работает, если всё видео — нарезка из записей одного размера и частоты кадров, без
эффектов, наложений и текстов. Иначе — обычный экспорт.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

from glimpsy.editor.project import ASPECTS, DEFAULT_FRAME, Clip, Project
from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

MIN_COPY_S = 1.0          # кусок короче — проще пересчитать целиком
MIN_GAIN = 0.3            # если без пересчёта можно взять меньше этой доли — смысла нет
AUDIO_CHUNK = 40          # столько кусков звука собирает один запуск FFmpeg
EDGE_FADE_S = 0.035


@dataclass
class VideoStream:
    codec: str
    pix_fmt: str
    width: int
    height: int
    fps: float
    timescale: int
    rotated: bool
    pts: list[float] = field(default_factory=list)       # время каждого кадра, по порядку
    keys: list[float] = field(default_factory=list)      # время ключевых кадров


@dataclass
class Piece:
    src: Path
    a: float               # откуда (секунды в исходнике)
    b: float               # докуда
    frames: int
    copy: bool             # True — переложить как есть, False — пересчитать


@dataclass
class Plan:
    pieces: list[Piece]
    starts: list[float]             # где в исходнике первый кадр каждого куска — оттуда и звук
    durations: list[float]          # настоящая длина видео каждого куска (по кадрам) — под неё звук
    fps: float
    timescale: int
    copied: float                   # доля, взятая без пересчёта


_cache: dict[tuple[str, float, int], VideoStream | None] = {}


def scan(ffmpeg: str, path: Path) -> VideoStream | None:
    """Кадры видео и ключевые кадры — читаются из файла без распаковки (быстро)."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    if key in _cache:
        return _cache[key]
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-map", "0:v:0", "-c", "copy",
                        "-f", "framecrc", "-"], capture_output=True, **subprocess_flags())
    info = parse_scan(r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")) \
        if r.returncode == 0 else None
    _cache[key] = info
    return info


def parse_scan(crc: str, header: str) -> VideoStream | None:
    m = re.search(r"Stream #\S+.*?Video:\s*(\w+)[^,]*,\s*(\w+)", header)
    tb = re.search(r"#tb 0: (\d+)/(\d+)", crc)
    dims = re.search(r"#dimensions 0: (\d+)x(\d+)", crc)
    if not m or not tb or not dims:
        return None
    base = Fraction(int(tb.group(1)), int(tb.group(2)))
    pts, keys = [], []
    for line in crc.splitlines():
        if line.startswith("#"):
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        t = float(int(p[2]) * base)
        pts.append(t)
        if "F=" not in line:                   # у ключевых кадров флаг не пишется
            keys.append(t)
    pts.sort()
    if len(pts) < 2:
        return None
    steps = sorted(b - a for a, b in zip(pts, pts[1:]))
    step = steps[len(steps) // 2]
    if step <= 0 or any(abs(s - step) > step * 0.5 for s in steps):
        return None                             # переменная частота кадров — не для этого способа
    rotated = bool(re.search(r"rotation of|rotate\s*:", header))
    return VideoStream(m.group(1), m.group(2), int(dims.group(1)), int(dims.group(2)), round(1 / step, 3),
                       int(tb.group(2)) // max(1, int(tb.group(1))), rotated, pts, sorted(keys))


def make_plan(project: Project, clips: list[Clip], streams: dict[str, VideoStream | None]) -> Plan | None:
    """Как собрать ролик: какие куски переложить как есть, какие пересчитать. None — нельзя."""
    first: VideoStream | None = None
    for c in clips:
        s = streams.get(str(project.path_of(c)))
        if c.kind != "video" or s is None or s.codec != "h264" or s.pix_fmt != "yuv420p" or s.rotated:
            return None
        if c.hidden or abs(c.speed - 1.0) > 1e-6 or tuple(c.frame_for(project.aspect)) != tuple(DEFAULT_FRAME):
            return None
        if first is None:
            first = s
        elif (s.width, s.height) != (first.width, first.height) or abs(s.fps - first.fps) > 0.01:
            return None
    if first is None:
        return None
    W, H = ASPECTS[project.aspect]
    if abs(first.width / first.height - W / H) > 0.01 or abs(first.fps - project.fps) > 0.05:
        return None
    eps = 0.25 / first.fps
    pieces: list[Piece] = []
    starts: list[float] = []
    durations: list[float] = []
    copied = total = 0
    for c in clips:
        s = streams[str(project.path_of(c))]
        assert s is not None
        src = project.path_of(c)
        inside = [t for t in s.pts if c.in_s - eps <= t < c.out_s - eps]
        if not inside:
            return None
        a, b = inside[0], inside[-1] + 1 / s.fps
        starts.append(a)
        durations.append(len(inside) / s.fps)
        total += len(inside)
        keys = [k for k in s.keys if a - eps <= k <= b + eps]
        k1, k2 = (keys[0], keys[-1]) if keys else (b, b)
        if k2 - k1 < MIN_COPY_S:
            pieces.append(Piece(src, a, b, len(inside), False))
            continue

        def count(x: float, y: float) -> int:
            return sum(1 for t in inside if x - eps <= t < y - eps)

        if count(a, k1):
            pieces.append(Piece(src, a, k1, count(a, k1), False))
        n = count(k1, k2)
        pieces.append(Piece(src, k1, k2, n, True))
        copied += n
        if count(k2, b):
            pieces.append(Piece(src, k2, b, count(k2, b), False))
    if not total or copied / total < MIN_GAIN:
        return None
    return Plan(pieces, starts, durations, first.fps, first.timescale, copied / total)


def plan_for(ffmpeg: str, project: Project, clips: list[Clip]) -> Plan | None:
    streams = {}
    for c in clips:
        p = project.path_of(c)
        if str(p) not in streams:
            streams[str(p)] = scan(ffmpeg, p) if c.kind == "video" else None
    return make_plan(project, clips, streams)


# ---------------- команды ----------------

def piece_command(ffmpeg: str, piece: Piece, plan: Plan, out: Path) -> list[str]:
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if piece.copy:
        # перематываем чуть дальше ключевого кадра — FFmpeg сам встанет ровно на него
        return base + ["-ss", f"{piece.a + 0.25 / plan.fps:.6f}", "-i", str(piece.src), "-map", "0:v:0",
                       "-c", "copy", "-frames:v", str(piece.frames), "-avoid_negative_ts", "make_zero",
                       "-video_track_timescale", str(plan.timescale), str(out)]
    return base + ["-ss", f"{piece.a - 0.25 / plan.fps:.6f}", "-i", str(piece.src), "-map", "0:v:0", "-frames:v", str(piece.frames),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-bf", "0", "-pix_fmt", "yuv420p",
                   "-r", f"{plan.fps:g}", "-video_track_timescale", str(plan.timescale), str(out)]


def audio_command(ffmpeg: str, project: Project, clips: list[Clip], starts: list[float], durations: list[float],
                  edges: list[tuple[bool, bool]], out: Path) -> list[str]:
    """Звук кусков подряд — каждый ровно той длины, что его видео (так звук не уплывает)."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    parts, labels = [], []
    n = 0
    for k, (c, start, dur, (fin, fout)) in enumerate(zip(clips, starts, durations, edges)):
        if c.has_audio and not c.muted:
            cmd += ["-ss", f"{start:.6f}", "-t", f"{dur + 0.1:.6f}", "-i", str(project.path_of(c))]
        else:
            cmd += ["-f", "lavfi", "-t", f"{dur:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
        src = f"[{n}:a]"
        n += 1
        fade = min(EDGE_FADE_S, dur / 4)
        fades = ([f"afade=t=in:d={fade:.3f}:curve=qsin"] if fin else []) + \
                ([f"afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}:curve=qsin"] if fout else [])
        parts.append(f"{src}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"apad=whole_dur={dur:.6f},atrim=0:{dur:.6f},asetpts=PTS-STARTPTS"
                     + "".join("," + f for f in fades) + f"[a{k}]")
        labels.append(f"[a{k}]")
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "pcm_s16le", str(out)]


def export(ffmpeg: str, project: Project, clips: list[Clip], plan: Plan, out: Path, work: Path,
           edges: list[tuple[bool, bool]], progress: Callable[[float, str], None],
           run: Callable[[list[str]], None], cancel: threading.Event) -> None:
    """Собрать ролик по плану: куски видео → склейка без пересчёта, звук отдельно, в конце — вместе."""
    files = []
    steps = len(plan.pieces) + 2
    for i, piece in enumerate(plan.pieces):
        if cancel.is_set():
            from glimpsy.editor.export import ExportCancelled
            raise ExportCancelled()
        progress(i / steps, f"Быстрое сохранение: кусок {i + 1} из {len(plan.pieces)}")
        f = work / f"piece_{i:05d}.mp4"
        run(piece_command(ffmpeg, piece, plan, f))
        files.append(f)
    progress(len(plan.pieces) / steps, "Звук")
    waves = []
    for i in range(0, len(clips), AUDIO_CHUNK):
        w = work / f"audio_{i:05d}.wav"
        run(audio_command(ffmpeg, project, clips[i:i + AUDIO_CHUNK], plan.starts[i:i + AUDIO_CHUNK],
                          plan.durations[i:i + AUDIO_CHUNK],
                          edges[i:i + AUDIO_CHUNK], w))
        waves.append(w)
    progress((len(plan.pieces) + 1) / steps, "Склейка")
    vlist = work / "pieces.txt"
    vlist.write_text("".join(f"file '{p.name}'\n" for p in files), encoding="utf-8")
    alist = work / "audio.txt"
    alist.write_text("".join(f"file '{p.name}'\n" for p in waves), encoding="utf-8")
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    video = work / "video.mp4"
    run(base + ["-f", "concat", "-safe", "0", "-i", str(vlist), "-c", "copy", str(video)])
    run(base + ["-i", str(video), "-f", "concat", "-safe", "0", "-i", str(alist), "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)])
