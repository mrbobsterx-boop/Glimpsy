"""Автозапуск Worklapse вместе с компьютером.

  Windows — запись в реестре «HKCU\\...\\Run» (как у Telegram, Dropbox и т. п.)
  macOS   — файл ~/Library/LaunchAgents/io.github.worklapse.plist
  Linux   — файл ~/.config/autostart/worklapse.desktop (стандарт freedesktop)

Никаких прав администратора не нужно: всё только для текущего пользователя.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shlex
import sys
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "Worklapse"
MAC_LABEL = "io.github.worklapse"
AUTOSTART_FLAG = "--autostart"


def command() -> list[str]:
    """Как запустить программу: собранная версия, AppImage или из исходников."""
    appimage = os.environ.get("APPIMAGE")          # AppImage сообщает путь к самому себе
    if appimage:
        return [appimage, AUTOSTART_FLAG]
    if getattr(sys, "frozen", False):
        return [sys.executable, AUTOSTART_FLAG]
    exe = sys.executable
    if sys.platform.startswith("win") and exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"             # без чёрного окна консоли
    return [exe, "-m", "worklapse", AUTOSTART_FLAG]


# ---------------- Linux ----------------

def _linux_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "worklapse.desktop"


def _linux_enable() -> None:
    f = _linux_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "[Desktop Entry]\nType=Application\nName=Worklapse\n"
        "Comment=Фоновая запись рабочего процесса\n"
        f"Exec={' '.join(shlex.quote(a) for a in command())}\n"
        "Terminal=false\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")


# ---------------- macOS ----------------

def _mac_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _mac_enable() -> None:
    f = _mac_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "wb") as fh:
        plistlib.dump({"Label": MAC_LABEL, "ProgramArguments": command(), "RunAtLoad": True,
                       "ProcessType": "Interactive"}, fh)


# ---------------- Windows ----------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_value() -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in command())


def _win_set(enable: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, _win_value())
        else:
            try:
                winreg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass


def _win_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, NAME)
            return True
    except OSError:
        return False


# ---------------- общее ----------------

def is_enabled() -> bool:
    try:
        if sys.platform.startswith("win"):
            return _win_enabled()
        if sys.platform == "darwin":
            return _mac_file().exists()
        return _linux_file().exists()
    except Exception:
        log.exception("Не удалось проверить автозапуск")
        return False


def set_enabled(enable: bool) -> str | None:
    """Включить/выключить автозапуск. Возвращает текст ошибки или None."""
    try:
        if sys.platform.startswith("win"):
            _win_set(enable)
        elif sys.platform == "darwin":
            _mac_enable() if enable else _mac_file().unlink(missing_ok=True)
        else:
            _linux_enable() if enable else _linux_file().unlink(missing_ok=True)
        log.info("Автозапуск: %s (%s)", "включён" if enable else "выключен", command())
        return None
    except Exception as e:
        log.exception("Не удалось изменить автозапуск")
        return str(e)


def refresh() -> None:
    """Если программу перенесли в другую папку — обновить путь в автозапуске."""
    if is_enabled():
        set_enabled(True)
