"""Перевод настроек интенсивности в конкретные числа.

Пользователь задаёт: длину ролика, длину фрагмента и темп. Отсюда программа сама
считает, сколько фрагментов нужно, сколько кандидатов хранить и как часто сохранять.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from glimpsy.config import Settings

# speed — во сколько раз ускоряем видео (лёгкий таймлапс-эффект)
# length_bias — где внутри диапазона «от–до» будут длины фрагментов (0 — короче, 1 — длиннее)
PACE_PARAMS = {
    "calm": {"speed": 1.0, "length_bias": 0.8},
    "medium": {"speed": 1.25, "length_bias": 0.5},
    "dynamic": {"speed": 1.6, "length_bias": 0.2},
}


@dataclass
class Plan:
    speed: float
    length_bias: float
    clip_out_s: float        # средняя длина фрагмента в готовом ролике
    clips_needed: int        # сколько фрагментов войдёт в ролик
    pool_size: int           # сколько кандидатов хранить (в 2–3 раза больше)
    candidate_s: float       # сколько секунд реального времени сохраняем на кандидата
    min_gap_s: float         # минимальный промежуток между сохранениями

    def save_interval(self, active_seconds: float) -> float:
        """Средний интервал между сохранениями.

        В начале сессии сохраняем чаще, чтобы быстро набрать кандидатов. Дальше — реже:
        интервал растёт так, чтобы кандидаты равномерно покрывали весь день, а лишние
        отсеивались (см. CandidatePool.prune). Длину сессии заранее знать не нужно.
        """
        return max(self.min_gap_s, active_seconds / max(1, self.pool_size))


def make_plan(s: Settings) -> Plan:
    p = PACE_PARAMS.get(s.pace, PACE_PARAMS["medium"])
    clip_out = s.clip_min_s + (s.clip_max_s - s.clip_min_s) * p["length_bias"]
    clips_needed = max(1, math.ceil(s.target_length_s / clip_out))
    pool = max(clips_needed + 2, math.ceil(clips_needed * s.oversample))
    # Кандидат длиннее, чем нужно: при сборке из него выберем самый живой кусок
    longest_source = s.clip_max_s * p["speed"]
    candidate = min(s.buffer_s - 2, max(longest_source * 1.5, longest_source + 2))
    return Plan(
        speed=p["speed"], length_bias=p["length_bias"], clip_out_s=clip_out,
        clips_needed=clips_needed, pool_size=pool, candidate_s=candidate,
        min_gap_s=max(candidate, 20.0),
    )


def save_probability(plan: Plan, active_seconds: float, recent_score: float, avg_score: float,
                     dt: float = 1.0) -> float:
    """Вероятность сохранить момент прямо сейчас (проверяется раз в секунду).

    Базовая частота 1/интервал, умноженная на «вес» момента: активный — чаще, скучный — реже.
    Благодаря случайности фрагменты не выглядят «по расписанию».
    """
    base = dt / plan.save_interval(active_seconds)
    rel = recent_score / avg_score if avg_score > 1e-6 else 1.0
    weight = min(3.0, max(0.15, rel))
    return min(1.0, base * weight)
