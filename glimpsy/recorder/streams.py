"""Потоки: запись только выбранных окон, каждое — в свой ролик.

По умолчанию Glimpsy снимает весь экран. Если в трее выбрать окно (или до трёх окон),
записываются только они: пока впереди окно потока — идёт запись в его ролик, переключились
на другое окно — запись ждёт, вернулись — продолжается. Из кадра монитора вырезается
только само окно, поэтому в ролик не попадает ничего вокруг.

Как узнать «своё» окно:
  window — именно это окно (по его номеру в системе; закрыли окно — поток больше не пишется);
  app    — любое окно этой программы;
  title  — окно, в заголовке которого есть слово (например, название проекта или сайта во вкладке).

Поток «экран» (screen) — весь монитор целиком. Такие потоки пишутся все сразу, одновременно
с окнами: каждый — в свой ролик. Звук, голос, камера и «важный момент» достаются только одному:
если есть потоки-окна — окну, которое впереди (экраны тогда пишут только картинку);
если потоков-окон нет — экрану, где сейчас курсор.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from glimpsy.platform.base import Monitor, WindowInfo

MAX_STREAMS = 3
MODES = ("window", "app", "title", "screen")


@dataclass
class StreamSpec:
    id: int
    name: str
    mode: str = "window"
    wid: int = 0
    app: str = ""
    title: str = ""
    monitor: int = 0             # screen: номер монитора (с 1)

    @property
    def is_screen(self) -> bool:
        return self.mode == "screen"

    def matches(self, win: WindowInfo | None) -> bool:
        if win is None or self.is_screen:
            return False
        if self.mode == "window":
            return bool(self.wid) and win.wid == self.wid
        if self.mode == "app":
            return bool(self.app) and win.app == self.app
        if self.mode == "title":
            return bool(self.title.strip()) and self.title.strip().lower() in win.title.lower()
        return False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> StreamSpec:
        return cls(**{k: d[k] for k in ("id", "name", "mode", "wid", "app", "title", "monitor")
                   if k in d})


def match(streams: list[StreamSpec], win: WindowInfo | None) -> StreamSpec | None:
    """Какой поток-окно сейчас впереди (первый подходящий)."""
    return next((s for s in streams if s.matches(win)), None)


def screen_for(streams: list[StreamSpec], monitor_index: int) -> StreamSpec | None:
    """Поток «экран» для этого монитора."""
    return next((s for s in streams if s.is_screen and s.monitor == monitor_index), None)


def monitor_for(monitors: list[Monitor], rect: tuple[int, int, int, int] | None) -> Monitor | None:
    """Монитор, на котором окно (по его середине; если середина за краем — где его больше всего)."""
    if not rect or not monitors:
        return None
    x, y, w, h = rect
    for m in monitors:
        if m.contains(x + w / 2, y + h / 2):
            return m

    def area(m: Monitor) -> float:
        dx = min(x + w, m.x + m.width) - max(x, m.x)
        dy = min(y + h, m.y + m.height) - max(y, m.y)
        return max(0, dx) * max(0, dy)

    best = max(monitors, key=area)
    return best if area(best) > 0 else None


def crop_fraction(rects: list[tuple[int, int, int, int]], mon: Monitor) -> list[float] | None:
    """Где окно на мониторе, в долях: [x, y, ширина, высота]. None — окно на весь экран или неизвестно.

    Окно за время фрагмента могли подвинуть — берём «среднее» (медиану) положение.
    """
    if not rects:
        return None
    x = statistics.median(r[0] for r in rects)
    y = statistics.median(r[1] for r in rects)
    w = statistics.median(r[2] for r in rects)
    h = statistics.median(r[3] for r in rects)

    def clamp(v: float) -> float:
        return min(1.0, max(0.0, v))

    x0, y0 = clamp((x - mon.x) / mon.width), clamp((y - mon.y) / mon.height)
    x1, y1 = clamp((x + w - mon.x) / mon.width), clamp((y + h - mon.y) / mon.height)
    if x1 - x0 < 0.05 or y1 - y0 < 0.05:
        return None                      # окно почти целиком за краем — снимаем весь монитор
    if x1 - x0 > 0.97 and y1 - y0 > 0.97:
        return None                      # развёрнуто на весь экран — резать нечего
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def to_crop(points: list[list[float]], crop: list[float] | None, margin: float = 0.02) -> list[list[float]]:
    """Курсор и клики [t, x, y] (доли монитора) → доли вырезанного окна; точки вне окна отбрасываются."""
    if not crop:
        return points
    cx, cy, cw, ch = crop
    out = []
    for t, x, y in points:
        nx, ny = (x - cx) / cw, (y - cy) / ch
        if -margin <= nx <= 1 + margin and -margin <= ny <= 1 + margin:
            out.append([t, round(min(1.0, max(0.0, nx)), 4), round(min(1.0, max(0.0, ny)), 4)])
    return out


def crop_filter(crop: list[float]) -> str:
    """Фильтр FFmpeg: вырезать окно из кадра монитора (размеры чётные — так любят кодеки)."""
    cx, cy, cw, ch = crop
    return (f"crop=w=floor(iw*{cw}/2)*2:h=floor(ih*{ch}/2)*2:"
            f"x=floor(iw*{cx}/2)*2:y=floor(ih*{cy}/2)*2")
