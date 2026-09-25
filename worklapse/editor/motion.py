"""Движение кадра по курсору (этап 3).

Во время записи программа 10 раз в секунду запоминает, где курсор. По этим точкам:

  * «Автозум к курсору» — когда курсор работает в небольшой области (рисуете, правите
    слой, двигаете точки), кадр плавно приближается к этому месту. Когда курсор уходит
    далеко (переключаетесь между панелями) — плавно отдаляется.
  * «Следовать за курсором» (для 9:16) — из горизонтальной записи вырезается
    вертикальная полоса, которая плавно едет за курсором.

Идея взята у открытых программ вроде Recordly и Open Recorder (группировать движения,
плавные переезды, без дёрганья за каждым движением), код — свой.

Движение считается как «ключевые кадры» (время → масштаб и центр), а затем
одинаково применяется и в просмотре, и в экспорте (фильтры FFmpeg).
"""

from __future__ import annotations

import math

STEP = 0.1                  # шаг расчёта, с
KEY_STEP = 0.25             # шаг ключевых кадров для FFmpeg, с
WINDOW = 0.6                # окно анализа вокруг момента, с
ZOOM_TAU = 0.45             # плавность масштаба (чем больше — тем мягче)
PAN_TAU = 0.35              # плавность переезда
FOLLOW_TAU = 0.5
TIGHT, LOOSE = 0.12, 0.25   # «курсор в небольшой области»: разброс в долях экрана

MODES = {"none": "Без движения", "autozoom": "Автозум к курсору", "follow": "Следовать за курсором (9:16)"}


def samples(cursor: list, duration: float) -> list[tuple[float, float, float]]:
    """Точки курсора [(время от начала файла, x, y)] внутри [0, duration]."""
    pts = sorted((float(t), float(x), float(y)) for t, x, y in cursor if 0 <= t <= duration + 0.5)
    return [p for p in pts if 0 <= p[1] <= 1 and 0 <= p[2] <= 1]


def _smooth(prev: float, target: float, dt: float, tau: float) -> float:
    return prev + (target - prev) * (1 - math.exp(-dt / tau))


CLICK_BEFORE, CLICK_AFTER = 0.35, 1.1   # приближаемся чуть заранее и держим после клика


def autozoom_track(cursor: list, duration: float, strength: float,
                   clicks: list | None = None) -> list[tuple[float, float, float, float]]:
    """[(t, масштаб, центр x, центр y)] с шагом STEP. Масштаб 1 — весь кадр.

    Клики важнее всего: вокруг клика кадр приближается именно к нему (как в Screen Studio).
    """
    pts = samples(cursor, duration)
    taps = samples(clicks or [], duration)
    if not pts and taps:
        pts = taps
    if not pts or duration <= 0:
        return [(0.0, 1.0, 0.5, 0.5), (max(duration, STEP), 1.0, 0.5, 0.5)]
    out = []
    z, cx, cy = 1.0, pts[0][1], pts[0][2]
    last_c = (cx, cy)
    t = 0.0
    j0 = 0
    while t <= duration + 1e-6:
        while j0 < len(pts) and pts[j0][0] < t - WINDOW:
            j0 += 1
        win = [p for p in pts[j0:] if p[0] <= t + WINDOW]
        if win:
            mx = sorted(p[1] for p in win)[len(win) // 2]
            my = sorted(p[2] for p in win)[len(win) // 2]
            spread = max(math.hypot(p[1] - mx, p[2] - my) for p in win)
            last_c = (mx, my)
        else:
            spread = 1.0          # нет данных — кадр целиком
        tap = min((c for c in taps if c[0] - CLICK_BEFORE <= t <= c[0] + CLICK_AFTER),
                  key=lambda c: abs(c[0] - t), default=None)
        if tap is not None:
            target = strength
            last_c = (tap[1], tap[2])
        elif spread < TIGHT:
            target = strength
        elif spread < LOOSE:
            target = 1 + (strength - 1) * 0.4
        else:
            target = 1.0
        z = _smooth(z, target, STEP, ZOOM_TAU)
        cx = _smooth(cx, last_c[0], STEP, PAN_TAU)
        cy = _smooth(cy, last_c[1], STEP, PAN_TAU)
        half = 0.5 / z            # центр так, чтобы окно не вылезало за края кадра
        out.append((round(t, 3), z, min(max(cx, half), 1 - half), min(max(cy, half), 1 - half)))
        t += STEP
    return out


def follow_track(cursor: list, duration: float, src_w: int, src_h: int) -> list[tuple[float, float]]:
    """[(t, центр x)] для вертикальной полосы 9:16, которая едет за курсором."""
    pts = samples(cursor, duration)
    band = min(1.0, (src_h * 9 / 16) / src_w) if src_w and src_h else 0.316
    half = band / 2
    if not pts or duration <= 0:
        return [(0.0, 0.5), (max(duration, STEP), 0.5)]
    out = []
    cx = min(max(pts[0][1], half), 1 - half)
    t, j = 0.0, 0
    target = cx
    while t <= duration + 1e-6:
        while j < len(pts) - 1 and pts[j + 1][0] <= t:
            j += 1
        x = pts[j][1]
        # «мёртвая зона»: пока курсор внутри середины полосы, кадр не двигается
        if abs(x - target) > band * 0.25:
            target = x
        cx = _smooth(cx, target, STEP, FOLLOW_TAU)
        out.append((round(t, 3), min(max(cx, half), 1 - half)))
        t += STEP
    return out


def follow_band(src_w: int, src_h: int) -> float:
    """Ширина вертикальной полосы в долях ширины кадра."""
    return min(1.0, (src_h * 9 / 16) / src_w) if src_w and src_h else 0.316


# ---------------- значения в момент времени (для просмотра) ----------------

def value_at(track: list[tuple], t: float) -> tuple:
    """Линейная интерполяция по дорожке [(t, *значения)]."""
    if not track:
        return ()
    if t <= track[0][0]:
        return track[0][1:]
    for a, b in zip(track, track[1:]):
        if t <= b[0]:
            k = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0
            return tuple(va + (vb - va) * k for va, vb in zip(a[1:], b[1:]))
    return track[-1][1:]


# ---------------- выражения для FFmpeg ----------------

def _keys(track: list[tuple], t0: float, t1: float) -> list[tuple]:
    """Ключевые кадры на отрезке [t0, t1] с шагом KEY_STEP, время — от t0."""
    keys, t = [], t0
    while t < t1:
        keys.append((t - t0, *value_at(track, t)))
        t += KEY_STEP
    keys.append((t1 - t0, *value_at(track, t1)))
    return keys


def piecewise(keys: list[tuple], idx: int, var: str = "it") -> str:
    """Кусочно-линейная функция времени в синтаксисе выражений FFmpeg."""
    expr = f"{keys[-1][idx]:.4f}"
    for a, b in reversed(list(zip(keys, keys[1:]))):
        dt = max(1e-3, b[0] - a[0])
        seg = f"{a[idx]:.4f}+({b[idx] - a[idx]:.4f})*({var}-{a[0]:.3f})/{dt:.3f}"
        expr = f"if(lt({var},{b[0]:.3f}),{seg},{expr})"
    return expr


def autozoom_filter(track: list, in_s: float, out_s: float, w: int, h: int, fps: int) -> str:
    keys = _keys(track, in_s, out_s)
    z, cx, cy = piecewise(keys, 1), piecewise(keys, 2), piecewise(keys, 3)
    # время кадров — с нуля от начала фрагмента (после -ss у .ts оно может начинаться не с нуля)
    return (f"setpts=PTS-STARTPTS,zoompan=z='{z}':x='max(0,min(iw-iw/zoom,({cx})*iw-iw/zoom/2))':"
            f"y='max(0,min(ih-ih/zoom,({cy})*ih-ih/zoom/2))':d=1:s={w}x{h}:fps={fps},"
            # zoompan иногда выдаёт кадры с повторяющимся временем (запись .ts) — тогда следующий
            # fps начинает «досыпать» кадры без конца и съедает память. Нумеруем кадры заново.
            f"setpts=N/({fps}*TB)")


def follow_filter(track: list, in_s: float, out_s: float, w: int, h: int) -> str:
    keys = _keys(track, in_s, out_s)
    cx = piecewise(keys, 1, "t")
    bw = int(min(w, h * 9 / 16)) // 2 * 2
    return f"setpts=PTS-STARTPTS,crop=w={bw}:h={h // 2 * 2}:x='max(0,min(iw-{bw},({cx})*iw-{bw}/2))':y=0"
