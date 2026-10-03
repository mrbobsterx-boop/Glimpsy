"""Обложка ролика: удачные кадры на выбор, крупный заголовок, своя картинка.

Кадры-кандидаты берутся равномерно по ролику; у каждого считаем резкость и «живость»
(контраст, не слишком тёмный и не пересвеченный) и оставляем лучшие, не стоящие рядом.
Обложка рисуется средствами Qt в картинку нужного размера: 1280×720 (YouTube) или
1080×1920 (Shorts, Reels).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
)

from glimpsy.paths import subprocess_flags

log = logging.getLogger(__name__)

SIZES = {"16:9": (1280, 720), "9:16": (1080, 1920)}
SAMPLES = 24
PICK = 6

DEFAULTS = {"title": "", "subtitle": "", "color": "#FFFFFF", "accent": "#FFD400", "place": "bottom",
            "size": 0.13, "outline": True, "shade": True, "t": None, "image": ""}


def settings(project) -> dict:
    d = dict(DEFAULTS)
    d.update((getattr(project, "cuts", {}) or {}).get("cover") or {})
    return d


def grab(ffmpeg: str, src: Path, t: float, width: int = 640) -> QImage:
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, **subprocess_flags())
    img = QImage()
    img.loadFromData(r.stdout, "PNG")
    return img


def score(img: QImage) -> float:
    """Чем выше — тем лучше кадр для обложки: резкий, контрастный, не тёмный и не пересвеченный."""
    if img.isNull():
        return -1.0
    g = img.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h = g.width(), g.height()
    a = np.frombuffer(g.constBits(), np.uint8, count=g.bytesPerLine() * h).reshape(h, g.bytesPerLine())[:, :w]
    a = a.astype(np.float32)
    lap = np.abs(4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])
    sharp = float(lap.mean())
    mean, spread = float(a.mean()), float(a.std())
    exposure = 1.0 - min(1.0, abs(mean - 120) / 120)
    return sharp * 0.6 + spread * 0.4 + exposure * 20


def frame_at(project, t: float) -> tuple[Path, float] | None:
    """Какой файл и какое место исходника показывается в момент t ролика (только видео и фото)."""
    i, off = project.locate(t)
    if i is None:
        return None
    c = project.clips[i]
    return project.path_of(c), (c.in_s + off * c.speed) if c.kind == "video" else 0.0


def candidates(ffmpeg: str, project, samples: int = SAMPLES, pick: int = PICK) -> list[tuple[float, QImage]]:
    """Лучшие кадры ролика: [(время в ролике, картинка)], по порядку."""
    total = project.total
    if total <= 0:
        return []
    scored = []
    for k in range(samples):
        t = total * (k + 0.5) / samples
        where = frame_at(project, t)
        if where is None:
            continue
        src, at = where
        img = QImage(str(src)) if src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp") \
            else grab(ffmpeg, src, at)
        if not img.isNull():
            scored.append((score(img), t, img))
    scored.sort(key=lambda x: -x[0])
    out: list[tuple[float, QImage]] = []
    gap = total / (pick * 2)
    for _s, t, img in scored:                       # лучшие — но не соседние кадры
        if all(abs(t - u) >= gap for u, _ in out):
            out.append((t, img))
        if len(out) >= pick:
            break
    return sorted(out, key=lambda x: x[0])


def compose(background: QImage, opts: dict, aspect: str = "16:9") -> QImage:
    """Обложка: картинка на весь кадр (обрезается по краям) + затемнение + заголовок."""
    W, H = SIZES.get(aspect, SIZES["16:9"])
    out = QImage(W, H, QImage.Format.Format_RGB32)
    out.fill(QColor("#14171D"))
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if not background.isNull():
        s = max(W / background.width(), H / background.height())
        w, h = background.width() * s, background.height() * s
        p.drawImage(QRectF((W - w) / 2, (H - h) / 2, w, h), background)
    title = (opts.get("title") or "").strip()
    sub = (opts.get("subtitle") or "").strip()
    place = opts.get("place", "bottom")
    if opts.get("shade", True) and (title or sub):
        grad = QLinearGradient(0, 0 if place == "top" else H, 0, H * 0.5)
        grad.setColorAt(0, QColor(0, 0, 0, 200))
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        if place == "center":
            p.fillRect(0, 0, W, H, QColor(0, 0, 0, 90))
        else:
            p.fillRect(0, 0, W, H, grad)
    if title or sub:
        margin = min(W, H) * 0.06
        box_w = W - margin * 2
        big = QFont()
        big.setBold(True)
        big.setPixelSize(max(12, int(float(opts.get("size", 0.13)) * min(W, H))))
        lines = _wrap(title, big, box_w) if title else []
        small = QFont()
        small.setBold(True)
        small.setPixelSize(max(10, int(big.pixelSize() * 0.42)))
        sub_lines = _wrap(sub, small, box_w) if sub else []
        fm, fs = QFontMetricsF(big), QFontMetricsF(small)
        height = fm.height() * 0.95 * len(lines) + (fs.height() * 1.1 * len(sub_lines) + fs.height() * 0.3
                                                     if sub_lines else 0)
        y = margin if place == "top" else (H - height) / 2 if place == "center" else H - margin - height
        for ln in lines:
            _draw_line(p, ln, big, W, y + fm.ascent(), QColor(opts.get("color", "#FFFFFF")),
                       bool(opts.get("outline", True)))
            y += fm.height() * 0.95
        if sub_lines:
            y += fs.height() * 0.3
            for ln in sub_lines:
                _draw_line(p, ln, small, W, y + fs.ascent(), QColor(opts.get("accent", "#FFD400")),
                           bool(opts.get("outline", True)))
                y += fs.height() * 1.1
    p.end()
    return out


def _wrap(text: str, font: QFont, width: float) -> list[str]:
    fm = QFontMetricsF(font)
    lines: list[str] = []
    for word in text.split():
        if lines and fm.horizontalAdvance(lines[-1] + " " + word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


def _draw_line(p: QPainter, text: str, font: QFont, W: int, baseline: float, color: QColor, outline: bool) -> None:
    fm = QFontMetricsF(font)
    x = (W - fm.horizontalAdvance(text)) / 2
    path = QPainterPath()
    path.addText(x, baseline, font, text)
    if outline:
        p.strokePath(path, QPen(QColor(0, 0, 0, 230), max(2.0, font.pixelSize() * 0.09), Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.fillPath(path, color)
