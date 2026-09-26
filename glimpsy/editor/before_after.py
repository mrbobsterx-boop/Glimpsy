"""«Было → стало»: короткая вставка, которая показывает результат работы.

Три варианта:
  шторка     — кадр «было», по нему проезжает шторка и открывает «стало»;
  таймлапс   — вся работа, сжатая в несколько секунд (с полоской прогресса);
  стоп-кадр  — «Было» (замерший первый кадр), резкая склейка, «Стало» (последний).

Вставка рисуется заранее в обычный видеофайл внутри проекта и появляется на ленте как
фрагмент: её можно двигать, обрезать, ускорять или удалить, как любой другой.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable

from glimpsy.editor.project import Clip, Project, new_id
from glimpsy.paths import subprocess_flags

MODES = {"wipe": "Шторка", "timelapse": "Таймлапс", "freeze": "Стоп-кадр"}
DEFAULT_S = {"wipe": 4.0, "timelapse": 6.0, "freeze": 3.2}
BG = "#0D0F13"
MAX_W = 1920

Progress = Callable[[float], bool]      # доля готовности → False, если отменили


class BeforeAfterError(RuntimeError):
    pass


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def frame_size(clips: list[Clip]) -> tuple[int, int]:
    """Размер вставки: как у записи (не больше Full HD по ширине)."""
    c = next((c for c in clips if c.kind == "video" and c.width and c.height), None)
    w, h = (c.width, c.height) if c else (1920, 1080)
    k = min(1.0, MAX_W / w)
    return _even(w * k), _even(h * k)


def _decode(ffmpeg: str, src: Path, start: float, length: float, n: int, w: int, h: int) -> list[bytes]:
    """n кадров (RGBA w×h), равномерно по отрезку [start, start + length] файла."""
    rate = max(0.01, n / max(0.05, length))
    vf = (f"fps={rate:.5f},scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={BG.replace('#', '0x')},format=rgba")
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.3f}",
                        "-t", f"{max(0.05, length):.3f}", "-i", str(src), "-vf", vf, "-frames:v", str(n),
                        "-f", "rawvideo", "-"], capture_output=True, **subprocess_flags())
    size = w * h * 4
    frames = [r.stdout[i:i + size] for i in range(0, len(r.stdout) - size + 1, size)]
    if not frames:
        raise BeforeAfterError(f"Не удалось прочитать кадры из {src.name}: "
                               + r.stderr.decode("utf-8", "replace")[-300:])
    return frames


def _qimage(raw: bytes, w: int, h: int):
    from PySide6.QtGui import QImage

    return QImage(raw, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


class _Writer:
    """Кадры из Qt → FFmpeg → mp4."""

    def __init__(self, ffmpeg: str, out: Path, w: int, h: int, fps: int) -> None:
        self.w, self.h = w, h
        self.proc = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, **subprocess_flags())

    def write(self, img) -> None:
        from PySide6.QtGui import QImage

        img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        assert self.proc.stdin is not None
        self.proc.stdin.write(bytes(img.constBits())[: self.w * self.h * 4])

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        err = self.proc.stderr.read() if self.proc.stderr else b""
        if self.proc.wait() != 0:
            raise BeforeAfterError("FFmpeg: " + err.decode("utf-8", "replace")[-400:])

    def abort(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        self.proc.kill()
        self.proc.wait()


# ---------------- рисование ----------------

def _badge(p, text: str, x: float, y: float, h: float, right: bool = False, accent: bool = False) -> None:
    """Плашка «Было»/«Стало» в углу."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont

    f = QFont("Inter")
    f.setPixelSize(max(10, int(h * 0.46)))
    f.setWeight(QFont.Weight.DemiBold)
    p.setFont(f)
    tw = p.fontMetrics().horizontalAdvance(text)
    dot = h * 0.28
    w = tw + h * 0.9 + dot + h * 0.2
    r = QRectF(x - w if right else x, y, w, h)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(13, 15, 19, 200))
    p.drawRoundedRect(r, h / 2, h / 2)
    p.setBrush(QColor("#22AEBB") if accent else QColor("#8B8D98"))
    p.drawEllipse(QRectF(r.x() + h * 0.45, r.center().y() - dot / 2, dot, dot))
    p.setPen(QColor("#FFFFFF"))
    p.drawText(QRectF(r.x() + h * 0.45 + dot + h * 0.2, r.y(), tw + 2, h),
               Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


def _ease(k: float) -> float:
    k = min(1.0, max(0.0, k))
    return k * k * (3 - 2 * k)


def _painter(img):
    from PySide6.QtGui import QPainter

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    return p


def wipe_frame(before, after, k: float, labels: tuple[str, str] = ("Было", "Стало")):
    """Кадр шторки: k = 0 — только «было», 1 — только «стало»."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPen

    w, h = before.width(), before.height()
    img = before.copy()
    p = _painter(img)
    x = w * k
    if x > 0:
        p.drawImage(QRectF(0, 0, x, h), after, QRectF(0, 0, x, h))
    bh = h * 0.06
    m = h * 0.04
    if k < 1:
        _badge(p, labels[0], w - m, m, bh, right=True)
    if k > 0:
        _badge(p, labels[1], m, m, bh, accent=True)
    if 0 < k < 1:
        p.setPen(QPen(QColor(255, 255, 255, 235), max(2.0, w * 0.0025)))
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        r = h * 0.035
        p.setPen(QPen(QColor(13, 15, 19, 120), max(1.0, w * 0.001)))
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(x, h / 2), r, r)
        p.setPen(QPen(QColor(13, 15, 19), max(1.5, r * 0.14), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        a = r * 0.38
        for sgn in (-1, 1):          # стрелки ‹ ›
            cx = x + sgn * r * 0.3
            p.drawLine(QPointF(cx + sgn * a * 0.6, h / 2 - a), QPointF(cx + sgn * a * 1.3, h / 2))
            p.drawLine(QPointF(cx + sgn * a * 1.3, h / 2), QPointF(cx + sgn * a * 0.6, h / 2 + a))
    p.end()
    return img


def freeze_frame(src, label: str, k: float, accent: bool):
    """Стоп-кадр с лёгким наездом (1 → 1.04) и плашкой."""
    from PySide6.QtCore import QRectF

    w, h = src.width(), src.height()
    img = src.copy()
    img.fill(0)
    p = _painter(img)
    z = 1 + 0.04 * _ease(k)
    p.drawImage(QRectF(w * (1 - z) / 2, h * (1 - z) / 2, w * z, h * z), src)
    _badge(p, label, h * 0.04, h * 0.04, h * 0.06, accent=accent)
    p.end()
    return img


def timelapse_frame(src, k: float, speed: float):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor

    w, h = src.width(), src.height()
    img = src.copy()
    p = _painter(img)
    _badge(p, f"Таймлапс ×{speed:.0f}", h * 0.04, h * 0.04, h * 0.06, accent=True)
    bar = max(3.0, h * 0.008)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 60))
    p.drawRect(QRectF(0, h - bar, w, bar))
    p.setBrush(QColor("#22AEBB"))
    p.drawRect(QRectF(0, h - bar, w * k, bar))
    p.end()
    return img


# ---------------- сборка вставки ----------------

def _sources(project: Project) -> list[Clip]:
    return [c for c in project.clips if c.kind == "video" and c.out_s - c.in_s > 0.05]


def render(ffmpeg: str, project: Project, mode: str, seconds: float, out: Path,
           progress: Progress | None = None) -> Path:
    """Нарисовать вставку в out (mp4). Бросает BeforeAfterError."""
    progress = progress or (lambda f: True)
    clips = _sources(project)
    if not clips:
        raise BeforeAfterError("В проекте нет видеофрагментов.")
    fps = project.fps
    w, h = frame_size(clips)
    n = max(2, int(round(seconds * fps)))
    first, last = clips[0], clips[-1]
    writer = _Writer(ffmpeg, out, w, h, fps)
    try:
        if mode == "timelapse":
            total = sum(c.out_s - c.in_s for c in clips)
            done = 0
            for i, c in enumerate(clips):
                share = (c.out_s - c.in_s) / total
                cnt = n - done if i == len(clips) - 1 else max(1, int(round(n * share)))
                cnt = min(cnt, n - done)
                if cnt <= 0:
                    continue
                for raw in _decode(ffmpeg, project.path_of(c), c.in_s, c.out_s - c.in_s, cnt, w, h):
                    writer.write(timelapse_frame(_qimage(raw, w, h), done / max(1, n - 1), total / seconds))
                    done += 1
                    if not progress(done / n):
                        raise BeforeAfterError("")
        else:
            before = _qimage(_decode(ffmpeg, project.path_of(first), first.in_s + 0.05, 0.2, 1, w, h)[0], w, h)
            after = _qimage(_decode(ffmpeg, project.path_of(last), max(last.in_s, last.out_s - 0.3), 0.25, 1,
                                    w, h)[-1], w, h)
            for i in range(n):
                t = i / fps
                if mode == "wipe":
                    hold_a, hold_b = min(0.8, seconds * 0.2), min(1.0, seconds * 0.25)
                    k = _ease((t - hold_a) / max(0.2, seconds - hold_a - hold_b))
                    img = wipe_frame(before, after, k)
                else:
                    half = seconds / 2
                    img = freeze_frame(before, "Было", t / half, False) if t < half else \
                        freeze_frame(after, "Стало", (t - half) / half, True)
                writer.write(img)
                if not progress((i + 1) / n):
                    raise BeforeAfterError("")
    except BaseException:
        writer.abort()
        out.unlink(missing_ok=True)
        raise
    writer.close()
    return out


def make_clip(ffmpeg: str, project: Project, mode: str, seconds: float,
              progress: Progress | None = None) -> Clip:
    """Нарисовать вставку в папку проекта и вернуть фрагмент для ленты."""
    media = project.dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    out = media / f"before_after_{mode}_{int(time.time())}.mp4"
    render(ffmpeg, project, mode, seconds, out, progress)
    w, h = frame_size(_sources(project))
    rel = out.relative_to(project.dir).as_posix()
    dur = max(2, int(round(seconds * project.fps))) / project.fps
    return Clip(new_id(), "video", rel, dur, 0.0, dur, width=w, height=h,
                label=f"Было → стало: {MODES[mode].lower()}")
