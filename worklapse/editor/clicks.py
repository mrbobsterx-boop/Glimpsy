"""Подсветка кликов: в месте клика расходится круг (как в Screen Studio и Loom).

Круг рисует сам FFmpeg (фильтр geq) прямо на исходном кадре — поэтому он точно на месте
клика и вместе с кадром приближается автозумом. В окне просмотра редактора тот же круг
рисует Qt по тем же формулам.
"""

from __future__ import annotations

RIPPLE_S = 0.55                 # сколько длится один круг в готовом ролике, с
SIZE = 0.085                    # размер круга — доля ширины исходного кадра
COLOR = (255, 196, 30)          # тёплый жёлтый: виден и на светлом, и на тёмном
FILL_ALPHA = 70                 # заливка внутри круга (0–255)


def radius_at(p: float) -> float:
    """Радиус кольца в долях половины размера круга, p — от 0 до 1."""
    return 0.3 + 0.65 * p


def active(clicks: list, t_src: float, speed: float) -> list[tuple[float, float, float]]:
    """Круги, видимые в момент t_src (время исходника): [(x, y, прогресс 0..1)]."""
    dur = RIPPLE_S * speed
    return [(x, y, (t_src - t) / dur) for t, x, y in clicks if 0 <= t_src - t < dur]


def ripple_graph(label_in: str, label_out: str, clicks: list, t0: float, t1: float, speed: float,
                 src_w: int) -> str | None:
    """Кусок графа FFmpeg: [label_in] → круги в местах кликов → [label_out].

    t0/t1 — какой отрезок исходника попал во фрагмент (кадры после -ss начинаются с нуля).
    """
    taps = [(t - t0, x, y) for t, x, y in clicks if t0 - 0.05 <= t < t1 and 0 <= x <= 1 and 0 <= y <= 1]
    if not taps or src_w <= 0:
        return None
    size = max(24, int(src_w * SIZE) // 2 * 2)
    c = size / 2
    dur = RIPPLE_S * speed
    th = max(2.0, size * 0.06)
    d = f"hypot(X-{c},Y-{c})"
    radius = f"{c}*(0.3+0.65*T/{dur:.3f})"
    fade = f"(1-T/{dur:.3f})"
    core = f"clip(1-abs({d}-{radius})/{th:.1f},0,1)"               # яркое кольцо
    halo = f"clip(1-abs({d}-{radius})/{th * 2.4:.1f},0,1)"         # тёмная кайма — видно на светлом
    inside = f"lt({d},{radius})"
    alpha = f"min(255,{fade}*max(255*{core},max(150*{halo},{FILL_ALPHA}*{inside})))"
    r, g, b = COLOR
    ring = f"gt({core},0.25)+{inside}"                              # кольцо и заливка — цветные, кайма — тёмная
    color = f"geq=r='if({ring},{r},25)':g='if({ring},{g},20)':b='if({ring},{b},15)':a='{alpha}'"
    n = len(taps)
    outs = "".join(f"[rs{i}]" for i in range(n))
    parts = [f"color=c=black@0:s={size}x{size}:r=30:d={dur:.3f},format=rgba,"
             f"{color}" + (f",split={n}" if n > 1 else "") + outs]
    parts.append(f"[{label_in}]setpts=PTS-STARTPTS[rbase]")     # время кадров — с нуля от начала фрагмента
    prev = "rbase"
    for i, (t, x, y) in enumerate(taps):
        start = max(0.0, t)
        parts.append(f"[rs{i}]setpts=PTS-STARTPTS+{start:.3f}/TB[rp{i}]")
        out = label_out if i == n - 1 else f"rv{i}"
        parts.append(f"[{prev}][rp{i}]overlay=x={x:.4f}*W-w/2:y={y:.4f}*H-h/2:eof_action=pass:"
                     f"enable='between(t,{start:.3f},{start + dur:.3f})'[{out}]")
        prev = out
    return ";".join(parts)
