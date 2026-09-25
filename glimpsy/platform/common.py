"""Реализации, которые работают одинаково на Windows, macOS и Linux/X11
(благодаря библиотекам mss и pynput)."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from glimpsy.platform import hotkey_format
from glimpsy.platform.base import CursorTracker, HotkeyBackend, Monitor

log = logging.getLogger(__name__)


def open_mss():
    """В новых версиях mss класс называется MSS, в старых — mss."""
    import mss

    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def list_monitors_mss() -> list[Monitor]:
    """Список мониторов. mss создаём каждый раз заново: он не любит работу из разных потоков."""
    with open_mss() as sct:
        # sct.monitors[0] — это весь рабочий стол целиком, настоящие мониторы начинаются с 1
        return [
            Monitor(i, int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]))
            for i, m in enumerate(sct.monitors[1:], start=1)
        ]


def monitor_at(monitors: list[Monitor], x: float, y: float) -> Monitor | None:
    for m in monitors:
        if m.contains(x, y):
            return m
    return None


class PynputCursor(CursorTracker):
    def __init__(self) -> None:
        from pynput.mouse import Controller

        self._ctl = Controller()

    def position(self) -> tuple[float, float] | None:
        try:
            pos = self._ctl.position
            return (float(pos[0]), float(pos[1])) if pos else None
        except Exception:
            return None


class NullCursor(CursorTracker):
    """Запасной вариант, когда положение мыши узнать нельзя (Wayland)."""

    supported = False

    def position(self) -> tuple[float, float] | None:
        return None


class PynputHotkeys(HotkeyBackend):
    """Глобальные горячие клавиши (Windows, macOS, Linux/X11)."""

    def __init__(self) -> None:
        self._listener = None
        self._lock = threading.Lock()

    def bind(self, bindings: dict[str, tuple[str, Callable[[], None]]]) -> list[str]:
        from pynput import keyboard

        self.stop()
        errors: list[str] = []
        mapping: dict[str, Callable[[], None]] = {}
        for _id, (combo, fn) in bindings.items():
            try:
                mapping[hotkey_format.to_pynput(combo)] = _safe(fn)
            except hotkey_format.HotkeyError as e:
                errors.append(str(e))
        if not mapping:
            return errors
        try:
            with self._lock:
                self._listener = keyboard.GlobalHotKeys(mapping)
                self._listener.daemon = True
                self._listener.start()
        except Exception as e:  # например, нет прав «Мониторинг ввода» на macOS
            log.exception("Горячие клавиши не запустились")
            errors.append(f"Горячие клавиши не работают: {e}")
        return errors

    def stop(self) -> None:
        with self._lock:
            if self._listener is not None:
                try:
                    self._listener.stop()
                except Exception:
                    pass
                self._listener = None


class NullHotkeys(HotkeyBackend):
    supported = False

    def bind(self, bindings):  # noqa: D401
        return []

    def stop(self) -> None:
        pass


def _safe(fn: Callable[[], None]) -> Callable[[], None]:
    """Ошибка в обработчике не должна «убить» поток клавиатуры."""

    def wrapper() -> None:
        try:
            fn()
        except Exception:
            log.exception("Ошибка в обработчике горячей клавиши")

    return wrapper
