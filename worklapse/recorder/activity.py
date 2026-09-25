"""Оценка «интересности» моментов.

Для каждой секунды считаем:
  * сколько пикселей проехала мышь;
  * сколько было кликов и прокруток;
  * сколько нажато клавиш (ТОЛЬКО количество — какие именно клавиши, программа не запоминает);
  * насколько изменилась картинка на экране (по миниатюрам от FFmpeg).

Из этого получается оценка от 0 до 1. Чем больше происходит — тем выше шанс,
что момент попадёт в ролик.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Вклад каждого сигнала в итоговую оценку (в сумме 1.0)
WEIGHTS = {"mouse": 0.25, "clicks": 0.25, "keys": 0.2, "screen": 0.3}
# Сколько считается «много» за секунду (дальше оценка не растёт)
SATURATION = {"mouse": 900.0, "clicks": 2.0, "keys": 5.0, "screen": 6.0}
# Если сигналы клавиатуры/мыши недоступны (Wayland) — оцениваем только по экрану
SCREEN_ONLY = {"screen": 1.0}

HISTORY_S = 600   # храним последние 10 минут посекундной статистики


@dataclass
class SecondStats:
    mouse: float = 0.0
    clicks: int = 0
    keys: int = 0
    screen: float = 0.0     # максимум изменения картинки за секунду


class ActivityTracker:
    def __init__(self, listen_input: bool, backend: str = "pynput") -> None:
        self.listen_input = listen_input
        self.backend = backend   # "pynput" (хуки) или "win32poll" (опрос без хуков, Windows)
        self._bins: dict[int, SecondStats] = {}
        self._lock = threading.Lock()
        self._last_mouse: tuple[float, float] | None = None
        self.last_input_time = time.time()
        self.last_screen_change_time = time.time()
        self._listeners: list = []
        self.cursor_track: collections.deque[tuple[float, int, float, float]] = collections.deque(maxlen=HISTORY_S * 10)
        self.click_times: collections.deque[float] = collections.deque(maxlen=HISTORY_S * 5)   # для подсветки кликов

    # ---------- слушатели ----------

    def start(self) -> list[str]:
        """Запускает глобальные слушатели мыши и клавиатуры. Возвращает список ошибок."""
        if not self.listen_input:
            return []
        if self.backend == "win32poll":
            from worklapse.platform.windows_input import WindowsInputPoller

            poller = WindowsInputPoller(self._record, self._mark_input)
            poller.start()
            self._listeners = [poller]
            return []
        try:
            from pynput import keyboard, mouse

            ml = mouse.Listener(on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)
            kl = keyboard.Listener(on_press=self._on_key)
            for listener in (ml, kl):
                listener.daemon = True
                listener.start()
            self._listeners = [ml, kl]
            return []
        except Exception as e:
            log.exception("Слушатели ввода не запустились")
            self.listen_input = False
            return [f"Не удалось отслеживать мышь и клавиатуру: {e}. Оценка — только по картинке."]

    def stop(self) -> None:
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception:
                pass
        self._listeners = []

    def _bin(self, t: float) -> SecondStats:
        key = int(t)
        b = self._bins.get(key)
        if b is None:
            b = self._bins[key] = SecondStats()
            if len(self._bins) > HISTORY_S * 1.2:
                cutoff = key - HISTORY_S
                for k in [k for k in self._bins if k < cutoff]:
                    del self._bins[k]
        return b

    def _on_move(self, x, y) -> None:
        now = time.time()
        with self._lock:
            if self._last_mouse is not None:
                self._bin(now).mouse += math.hypot(x - self._last_mouse[0], y - self._last_mouse[1])
            self._last_mouse = (x, y)
        self.last_input_time = now

    def _on_click(self, x, y, button, pressed) -> None:
        if pressed:
            self._record("clicks")

    def _on_scroll(self, x, y, dx, dy) -> None:
        self._record("clicks", 0.5)

    def _on_key(self, key) -> None:
        # Сама клавиша (key) сознательно игнорируется — только счётчик.
        self._record("keys")

    def _record(self, kind: str, amount: float = 1) -> None:
        now = time.time()
        with self._lock:
            b = self._bin(now)
            setattr(b, kind, getattr(b, kind) + amount)
            if kind == "clicks" and amount >= 1:        # нажатие кнопки мыши (прокрутка — 0.5)
                self.click_times.append(now)
        self.last_input_time = now

    def _mark_input(self, t: float) -> None:
        self.last_input_time = t

    def add_frame_diff(self, diff: float, t: float) -> None:
        with self._lock:
            b = self._bin(t)
            b.screen = max(b.screen, diff)
        if diff > 1.0:
            self.last_screen_change_time = t

    def add_cursor(self, t: float, monitor_index: int, nx: float, ny: float) -> None:
        """Положение курсора (0..1 внутри монитора) — пригодится для автозума на этапе 3."""
        self.cursor_track.append((t, monitor_index, nx, ny))

    # ---------- оценки ----------

    def second_score(self, s: SecondStats) -> float:
        weights = WEIGHTS if self.listen_input else SCREEN_ONLY
        total = 0.0
        for k, w in weights.items():
            total += w * min(1.0, getattr(s, k) / SATURATION[k])
        return total

    def per_second(self, t0: float, t1: float) -> list[float]:
        with self._lock:
            return [self.second_score(self._bins.get(k, SecondStats())) for k in range(int(t0), int(math.ceil(t1)))]

    def score(self, t0: float, t1: float) -> float:
        """Средняя интересность отрезка; немного поощряем яркие пики."""
        vals = self.per_second(t0, t1)
        if not vals:
            return 0.0
        return 0.7 * (sum(vals) / len(vals)) + 0.3 * max(vals)

    def cursor_between(self, t0: float, t1: float) -> list[tuple[float, int, float, float]]:
        return [c for c in list(self.cursor_track) if t0 <= c[0] <= t1]

    def clicks_between(self, t0: float, t1: float, monitor: int) -> list[tuple[float, float, float]]:
        """Клики [(время, x, y)] на мониторе. Где был курсор — берём из ближайшей точки дорожки курсора."""
        track = [c for c in list(self.cursor_track) if c[1] == monitor and t0 - 0.3 <= c[0] <= t1 + 0.3]
        out = []
        for t in [t for t in list(self.click_times) if t0 <= t <= t1]:
            near = min(track, key=lambda c: abs(c[0] - t), default=None)
            if near is not None and abs(near[0] - t) <= 0.25:
                out.append((t, near[2], near[3]))
        return out

    def idle_for(self, now: float) -> float:
        """Сколько секунд пользователь ничего не делает."""
        last = self.last_input_time if self.listen_input else self.last_screen_change_time
        return now - last
