"""Windows: горячие клавиши и оценка активности БЕЗ глобальных перехватчиков клавиатуры/мыши.

Почему так: библиотека pynput на Windows ставит «низкоуровневые хуки» — каждое нажатие
клавиши и движение мыши во всей системе сначала проходит через нашу программу. Это может
мешать другим программам (например, скриншотам по Print Screen или «Ножницам»),
а при нагрузке — замедлять курсор.

Поэтому на Windows:
  * горячие клавиши регистрируются штатной функцией RegisterHotKey — Windows сама
    сообщает нам только о наших сочетаниях, остальные клавиши мы не видим вообще;
  * активность определяется опросом 30 раз в секунду: положение курсора, состояние
    кнопок мыши и «время последнего ввода» (GetLastInputInfo). Какие клавиши нажимались,
    программа не знает и знать не может.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable

from glimpsy.platform import hotkey_format
from glimpsy.platform.base import HotkeyBackend

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WindowsHotkeys(HotkeyBackend):
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    def bind(self, bindings: dict[str, tuple[str, Callable[[], None]]]) -> list[str]:
        self.stop()
        errors: list[str] = []
        parsed: list[tuple[int, int, int, str, Callable[[], None]]] = []
        for i, (_id, (combo, fn)) in enumerate(bindings.items(), start=1):
            try:
                mods, vk = hotkey_format.to_win32(combo)
                parsed.append((i, mods | MOD_NOREPEAT, vk, combo, fn))
            except hotkey_format.HotkeyError as e:
                errors.append(str(e))
        if not parsed:
            return errors

        ready = threading.Event()
        reg_errors: list[str] = []

        def loop() -> None:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            self._thread_id = kernel32.GetCurrentThreadId()
            callbacks: dict[int, Callable[[], None]] = {}
            for hid, mods, vk, combo, fn in parsed:
                # Регистрировать нужно в том же потоке, который потом ждёт сообщения
                if user32.RegisterHotKey(None, hid, mods, vk):
                    callbacks[hid] = fn
                else:
                    reg_errors.append(f"Сочетание {combo} уже занято другой программой. "
                                      f"Выберите другое в настройках.")
            ready.set()
            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == WM_HOTKEY:
                        fn = callbacks.get(int(msg.wParam))
                        if fn:
                            try:
                                fn()
                            except Exception:
                                log.exception("Ошибка в обработчике горячей клавиши")
            finally:
                for hid in callbacks:
                    user32.UnregisterHotKey(None, hid)

        self._thread = threading.Thread(target=loop, daemon=True, name="win-hotkeys")
        self._thread.start()
        ready.wait(timeout=3)
        return errors + reg_errors

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)  # type: ignore[attr-defined]
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0


class WindowsInputPoller:
    """Считает движение мыши, клики и «прочий ввод» (клавиатура) опросом, без хуков.

    record(kind, amount) и on_input(t) — функции трекера активности.
    """

    HZ = 30
    VK_BUTTONS = (0x01, 0x02, 0x04)   # левая, правая, средняя кнопки мыши

    def __init__(self, record: Callable[[str, float], None], on_input: Callable[[float], None]) -> None:
        self._record = record
        self._on_input = on_input
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="win-input-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        pt = wintypes.POINT()
        last_pos: tuple[int, int] | None = None
        last_input = None
        buttons_down = [False] * len(self.VK_BUTTONS)
        while not self._stop.wait(1 / self.HZ):
            now = time.time()
            moved = clicked = False
            if user32.GetCursorPos(ctypes.byref(pt)):
                pos = (pt.x, pt.y)
                if last_pos is not None and pos != last_pos:
                    dist = ((pos[0] - last_pos[0]) ** 2 + (pos[1] - last_pos[1]) ** 2) ** 0.5
                    self._record("mouse", dist)
                    moved = True
                last_pos = pos
            for i, vk in enumerate(self.VK_BUTTONS):
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not buttons_down[i]:
                    self._record("clicks", 1)
                    clicked = True
                buttons_down[i] = down
            if user32.GetLastInputInfo(ctypes.byref(lii)):
                if last_input is not None and lii.dwTime != last_input:
                    if not moved and not clicked and not any(buttons_down):
                        self._record("keys", 1)   # ввод был, но не мышью → скорее всего клавиатура
                    self._on_input(now)
                elif moved or clicked:
                    self._on_input(now)
                last_input = lii.dwTime
