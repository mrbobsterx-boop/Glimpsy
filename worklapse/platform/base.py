"""Общий интерфейс платформенных модулей.

Остальная программа знает только эти классы и не думает о том, Windows это,
macOS или Linux. Каждая ОС подставляет свою реализацию (см. platform/__init__.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Monitor:
    index: int          # номер монитора, начиная с 1 (как видит пользователь)
    x: int
    y: int
    width: int
    height: int

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height

    @property
    def label(self) -> str:
        return f"Монитор {self.index} ({self.width}×{self.height})"


@dataclass
class CaptureInput:
    """Как FFmpeg должен получать картинку конкретного монитора.

    args        — параметры входа FFmpeg (включая -i ...)
    pre_filter  — фильтр, который нужно применить первым (например, скачать кадр из видеопамяти)
    producer    — внешняя программа, которая выдаёт сырые кадры в stdout (Wayland/GStreamer)
    """

    args: list[str]
    width: int
    height: int
    pre_filter: str = ""
    producer: list[str] | None = None
    producer_pass_fds: tuple[int, ...] = ()


@dataclass
class WindowInfo:
    app: str = ""
    title: str = ""

    def matches(self, patterns: list[str]) -> bool:
        hay = f"{self.app}\n{self.title}".lower()
        return any(p.lower() in hay for p in patterns if p)


class CursorTracker(ABC):
    supported: bool = True

    @abstractmethod
    def position(self) -> tuple[float, float] | None:
        """Координаты курсора на рабочем столе или None, если узнать нельзя."""


class ActiveWindowProbe(ABC):
    supported: bool = True

    @abstractmethod
    def active(self) -> WindowInfo | None:
        """Какая программа сейчас в фокусе. None — если узнать нельзя."""


class HotkeyBackend(ABC):
    supported: bool = True

    @abstractmethod
    def bind(self, bindings: dict[str, tuple[str, Callable[[], None]]]) -> list[str]:
        """bindings: id -> (сочетание вида 'Ctrl+Alt+S', функция).

        Возвращает список ошибок (пустой, если всё хорошо).
        """

    @abstractmethod
    def stop(self) -> None: ...


class CaptureBackend(ABC):
    """Источник видео для кольцевого буфера."""

    name: str = "base"

    @abstractmethod
    def monitors(self) -> list[Monitor]: ...

    @abstractmethod
    def input_for(self, monitor: Monitor, fps: int) -> CaptureInput: ...

    def close(self) -> None:
        pass

    def reset(self) -> None:
        """Забыть выбор экрана (актуально для Wayland)."""
        self.close()


@dataclass
class PlatformServices:
    """Всё платформенное в одном месте."""

    os_name: str                          # windows / macos / linux
    display_server: str                   # windows / macos / x11 / wayland
    capture: CaptureBackend
    cursor: CursorTracker
    active_window: ActiveWindowProbe
    hotkeys: HotkeyBackend
    input_events_supported: bool          # можно ли слушать мышь/клавиатуру глобально
    limitations: list[str] = field(default_factory=list)   # понятные сообщения пользователю
