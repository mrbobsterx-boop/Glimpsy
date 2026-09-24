"""Определяем ОС и собираем подходящие платформенные модули.

Главное правило: если что-то недоступно (например, горячие клавиши на Wayland), программа
не падает, а подставляет «запасной» вариант и объясняет пользователю, что ограничено и почему.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable

from worklapse.platform.base import PlatformServices

log = logging.getLogger(__name__)


def detect_display_server() -> str:
    """windows / macos / x11 / wayland"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11"


def build_services(ffmpeg: str, capture_backend: str = "auto", wayland_token: str = "",
                   on_wayland_token: Callable[[str], None] | None = None) -> PlatformServices:
    ds = detect_display_server()
    log.info("Платформа: %s", ds)
    if ds == "windows":
        return _windows(ffmpeg, capture_backend)
    if ds == "macos":
        return _macos()
    if ds == "wayland":
        return _wayland(wayland_token, on_wayland_token)
    return _x11()


def _windows(ffmpeg: str, backend: str) -> PlatformServices:
    from worklapse.platform.active_window import WindowsActiveWindow
    from worklapse.platform.capture import DdaGrabCapture, GdiGrabCapture
    from worklapse.platform.common import PynputCursor
    from worklapse.platform.windows_input import WindowsHotkeys

    capture = GdiGrabCapture() if backend == "gdigrab" else DdaGrabCapture(ffmpeg)
    # На Windows никаких глобальных хуков клавиатуры/мыши: горячие клавиши — через
    # RegisterHotKey, активность — опросом (см. windows_input.py)
    return PlatformServices(
        os_name="windows", display_server="windows", capture=capture,
        cursor=PynputCursor(), active_window=WindowsActiveWindow(), hotkeys=WindowsHotkeys(),
        input_events_supported=True, input_backend="win32poll",
    )


def _macos() -> PlatformServices:
    from worklapse.platform.active_window import MacActiveWindow
    from worklapse.platform.capture import AVFoundationCapture
    from worklapse.platform.common import PynputCursor, PynputHotkeys

    return PlatformServices(
        os_name="macos", display_server="macos", capture=AVFoundationCapture(),
        cursor=PynputCursor(), active_window=MacActiveWindow(), hotkeys=PynputHotkeys(),
        input_events_supported=True,
        limitations=[
            "macOS попросит разрешения: «Запись экрана» (для видео) и «Мониторинг ввода» / "
            "«Универсальный доступ» (для горячих клавиш и оценки активности). Выдайте их в "
            "Системных настройках → Конфиденциальность и безопасность и перезапустите Worklapse.",
        ],
    )


def _x11() -> PlatformServices:
    from worklapse.platform.active_window import NullActiveWindow, X11ActiveWindow
    from worklapse.platform.capture import X11GrabCapture
    from worklapse.platform.common import PynputCursor, PynputHotkeys

    limitations = []
    try:
        aw = X11ActiveWindow()
    except Exception:
        log.exception("X11: активное окно недоступно")
        aw = NullActiveWindow()
        limitations.append("Не удалось подключиться к X11 для проверки активного окна — "
                           "чёрный список приложений не работает.")
    return PlatformServices(
        os_name="linux", display_server="x11", capture=X11GrabCapture(), cursor=PynputCursor(),
        active_window=aw, hotkeys=PynputHotkeys(), input_events_supported=True,
        limitations=limitations,
    )


def _wayland(token: str, on_token: Callable[[str], None] | None) -> PlatformServices:
    from worklapse.platform.active_window import NullActiveWindow
    from worklapse.platform.common import NullCursor, NullHotkeys
    from worklapse.platform.wayland_portal import PortalHotkeys, WaylandPortalCapture

    limitations = [
        "Wayland: программа не может узнать, где находится курсор. Записывается один монитор, "
        "который вы выберете в системном окне «Поделиться экраном». Сменить его можно в "
        "Настройках → «Выбрать экран заново».",
        "Wayland: нельзя узнать, какое приложение в фокусе, поэтому чёрный список приложений "
        "не работает. Перед мессенджером или банком ставьте паузу (Ctrl+Alt+P или через трей).",
        "Wayland: клики и нажатия клавиш в других программах не видны, поэтому «интересность» "
        "моментов оценивается только по изменению картинки, а автопауза — по неподвижному экрану.",
    ]
    capture = WaylandPortalCapture(token, on_token)
    if PortalHotkeys.available():
        hotkeys = PortalHotkeys()
        limitations.append("Горячие клавиши работают через системный портал: при первом запуске "
                           "окружение рабочего стола может попросить их подтвердить.")
    else:
        hotkeys = NullHotkeys()
        limitations.append("Ваше окружение рабочего стола не поддерживает глобальные горячие клавиши "
                           "для приложений (портал GlobalShortcuts). Управляйте записью через иконку в трее.")
    return PlatformServices(
        os_name="linux", display_server="wayland", capture=capture, cursor=NullCursor(),
        active_window=NullActiveWindow(), hotkeys=hotkeys, input_events_supported=False,
        limitations=limitations,
    )
