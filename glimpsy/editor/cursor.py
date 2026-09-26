"""Свой плавный курсор (как в Screen Studio).

Экран записывается без системного курсора, а Glimpsy рисует свой — по точкам, где был
курсор при записи (10 раз в секунду). Между точками путь сглаживается кривой, поэтому курсор
не дёргается, а плывёт. Раз курсор рисуем мы, его вид и размер можно поменять в редакторе
уже после записи.

Одни и те же формулы — в окне просмотра (Qt) и в готовом ролике (FFmpeg: картинка курсора
накладывается фильтром overlay, положение — кусочно-линейная функция времени).
"""

from __future__ import annotations

import math
from pathlib import Path

STYLES = {"arrow": "Стрелка", "arrow_dark": "Тёмная", "dot": "Точка", "ring": "Подсветка"}
DEFAULT_STYLE = "arrow"
BASE = 0.02                 # высота курсора — доля ширины исходного кадра (при размере ×1)
MIN_SIZE, MAX_SIZE = 0.5, 3.0
STEP = 0.05                 # шаг сглаженного пути, с
GAP = 0.6                   # нет точек дольше — курсор был на другом мониторе: не рисуем
CHUNK = 2.0                 # в FFmpeg путь режется на куски (короткие выражения быстрее)

# классическая стрелка в долях высоты: остриё в (0, 0)
ARROW = [(0.0, 0.0), (0.0, 0.8), (0.19, 0.63), (0.32, 0.93), (0.45, 0.87), (0.33, 0.58), (0.57, 0.58)]


# ---------------- путь ----------------

def _catmull(p0: float, p1: float, p2: float, p3: float, k: float) -> float:
    return 0.5 * (2 * p1 + (-p0 + p2) * k + (2 * p0 - 5 * p1 + 4 * p2 - p3) * k * k
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * k ** 3)


def smooth_track(cursor: list, duration: float) -> list[tuple[float, float, float, bool]]:
    """[(t, x, y, виден)] с шагом STEP по всему файлу: плавная кривая через точки курсора."""
    pts = sorted((float(t), float(x), float(y)) for t, x, y in cursor if -0.5 <= t <= duration + 0.5)
    if not pts or duration <= 0:
        return []
    out = []
    j = 0
    n = int(duration / STEP) + 1
    sx, sy = pts[0][1], pts[0][2]
    for i in range(n + 1):
        t = min(i * STEP, duration)
        while j < len(pts) - 2 and pts[j + 1][0] <= t:
            j += 1
        a, b = pts[j], pts[min(j + 1, len(pts) - 1)]
        near = min(abs(t - a[0]), abs(t - b[0]))
        inside = a[0] <= t <= b[0] and b[0] - a[0] <= GAP
        visible = inside or near <= GAP / 2
        if b[0] > a[0] and a[0] <= t <= b[0]:
            k = (t - a[0]) / (b[0] - a[0])
            p0 = pts[max(j - 1, 0)]
            p3 = pts[min(j + 2, len(pts) - 1)]
            x, y = _catmull(p0[1], a[1], b[1], p3[1], k), _catmull(p0[2], a[2], b[2], p3[2], k)
        else:
            x, y = (a[1], a[2]) if abs(t - a[0]) <= abs(t - b[0]) else (b[1], b[2])
        # лёгкое «догоняние» убирает остатки дрожи руки
        f = 1 - math.exp(-STEP / 0.05)
        sx, sy = (sx + (x - sx) * f, sy + (y - sy) * f) if out and out[-1][3] else (x, y)
        out.append((round(t, 3), min(1.0, max(0.0, sx)), min(1.0, max(0.0, sy)), visible))
    return out


def position_at(track: list, t: float) -> tuple[float, float] | None:
    """Где курсор в момент t (для просмотра). None — не виден."""
    if not track:
        return None
    i = min(len(track) - 1, max(0, int(round(t / STEP))))
    a = track[i]
    if not a[3]:
        return None
    b = track[min(i + 1, len(track) - 1)]
    if b[3] and b[0] > a[0] and a[0] <= t <= b[0]:
        k = (t - a[0]) / (b[0] - a[0])
        return a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k
    return a[1], a[2]


# ---------------- картинка курсора ----------------

def height_px(src_w: float, size: float) -> int:
    return max(8, int(round(src_w * BASE * max(MIN_SIZE, min(MAX_SIZE, size)))))


def hotspot(style: str, px: int) -> tuple[float, float]:
    """Точка картинки, которая показывает на место курсора."""
    if style in ("dot", "ring"):
        return px / 2, px / 2
    pad = max(1.5, px * 0.08)
    return pad, pad


def image(style: str, px: int):
    """QImage курсора высотой px (с прозрачным фоном)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

    style = style if style in STYLES else DEFAULT_STYLE
    w = px if style in ("dot", "ring") else int(math.ceil(px * 0.72))
    img = QImage(max(2, w), max(2, px), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if style in ("dot", "ring"):
        r = px / 2 - max(1.0, px * 0.06)
        c = QPointF(px / 2, px / 2)
        if style == "dot":
            p.setPen(QPen(QColor(255, 255, 255, 235), max(1.2, px * 0.09)))
            p.setBrush(QColor(20, 22, 28, 235))
            p.drawEllipse(c, r * 0.55, r * 0.55)
        else:
            p.setPen(QPen(QColor(255, 196, 30, 230), max(1.5, px * 0.08)))
            p.setBrush(QColor(255, 196, 30, 70))
            p.drawEllipse(c, r, r)
    else:
        pad = max(1.5, px * 0.08)
        s = px - 2 * pad
        path = QPainterPath()
        path.moveTo(pad + ARROW[0][0] * s, pad + ARROW[0][1] * s)
        for x, y in ARROW[1:]:
            path.lineTo(pad + x * s, pad + y * s)
        path.closeSubpath()
        fill, edge = (QColor(255, 255, 255), QColor(15, 15, 18)) if style == "arrow" else \
            (QColor(18, 18, 22), QColor(255, 255, 255))
        pen = QPen(edge, max(1.0, px * 0.055))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        # мягкая тень — курсор виден на любом фоне
        p.save()
        p.translate(px * 0.03, px * 0.05)
        p.fillPath(path, QColor(0, 0, 0, 70))
        p.restore()
        p.setPen(pen)
        p.setBrush(fill)
        p.drawPath(path)
    p.end()
    return img


def png(style: str, px: int) -> Path:
    """Картинка курсора в файле (для FFmpeg), с кэшем."""
    from glimpsy import paths

    d = paths.temp_root() / "cursors"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{style}_{px}.png"
    if not f.exists():
        tmp = f.with_suffix(".tmp.png")
        image(style, px).save(str(tmp))
        tmp.replace(f)
    return f


# ---------------- FFmpeg ----------------

def _piecewise(keys: list[tuple[float, float]]) -> str:
    expr = f"{keys[-1][1]:.1f}"
    for (ta, va), (tb, vb) in reversed(list(zip(keys, keys[1:]))):
        dt = max(1e-3, tb - ta)
        expr = f"if(lt(t,{tb:.3f}),{va:.1f}+({vb - va:.1f})*(t-{ta:.3f})/{dt:.3f},{expr})"
    return expr


def runs(track: list, t0: float, t1: float) -> list[list[tuple[float, float, float]]]:
    """Куски пути, где курсор виден, на отрезке [t0, t1]; время — от t0; каждый не длиннее CHUNK."""
    out: list[list] = []
    cur: list = []
    for t, x, y, vis in track:
        if t < t0 - STEP or t > t1 + STEP:
            continue
        if vis:
            cur.append((round(t - t0, 3), x, y))
            if cur[-1][0] - cur[0][0] >= CHUNK:
                out.append(cur)
                cur = [cur[-1]]
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [r for r in out if len(r) >= 2 or (r and r[0][0] >= 0)]


def overlay_graph(label_in: str, label_out: str, input_idx: int, cursor: list, duration: float,
                  t0: float, t1: float, src_w: int, src_h: int, style: str, size: float) -> str | None:
    """Кусок графа: [label_in] + картинка курсора (вход input_idx) → [label_out].

    Координаты курсора — в долях кадра [label_in] (после вырезания окна, до зума).
    """
    parts_runs = runs(smooth_track(cursor, duration), t0, t1)
    if not parts_runs or src_w <= 0 or src_h <= 0:
        return None
    px = height_px(src_w, size)
    hx, hy = hotspot(style, px)
    n = len(parts_runs)
    outs = "".join(f"[cur{i}]" for i in range(n))
    parts = [f"[{input_idx}:v]format=rgba" + (f",split={n}" if n > 1 else "") + outs,
             f"[{label_in}]setpts=PTS-STARTPTS[cbase]"]
    prev = "cbase"
    for i, run in enumerate(parts_runs):
        if len(run) == 1:
            run = [run[0], (run[0][0] + STEP, run[0][1], run[0][2])]
        xs = [(t, x * src_w - hx) for t, x, _ in run]
        ys = [(t, y * src_h - hy) for t, _, y in run]
        a, b = max(0.0, run[0][0]), run[-1][0]
        out = label_out if i == n - 1 else f"cv{i}"
        parts.append(f"[{prev}][cur{i}]overlay=x='{_piecewise(xs)}':y='{_piecewise(ys)}':eval=frame:"
                     f"eof_action=pass:enable='between(t,{a:.3f},{b:.3f})'[{out}]")
        prev = out
    return ";".join(parts)


def input_args(style: str, px: int, duration: float) -> list[str]:
    """Вход FFmpeg с картинкой курсора."""
    return ["-loop", "1", "-t", f"{max(0.1, duration):.3f}", "-i", str(png(style, px))]
