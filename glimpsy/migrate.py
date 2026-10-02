"""Переезд со старого названия программы (Worklapse → Glimpsy).

При первом запуске Glimpsy переносит всё, что накопилось у Worklapse: настройки, проекты
редактора, свои шрифты, скачанную модель субтитров и незаконченные сессии. Автозапуск
тоже переключается на новое название. Готовые ролики остаются там, где были.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

from glimpsy import autostart, paths

log = logging.getLogger(__name__)

OLD_NAME = "Worklapse"


def _move_contents(old: Path, new: Path) -> int:
    """Перенести содержимое папки. То, что уже есть в новой, не трогаем. Возвращает число перенесённых."""
    if not old.is_dir() or old.resolve() == new.resolve():
        return 0
    new.mkdir(parents=True, exist_ok=True)
    moved = 0
    for child in list(old.iterdir()):
        dest = new / child.name
        if dest.exists():
            if child.is_dir() and dest.is_dir():
                moved += _move_contents(child, dest)      # например, projects/ — переносим по одному
            continue
        try:
            shutil.move(str(child), str(dest))
            moved += 1
        except OSError:
            log.exception("Не удалось перенести %s", child)
    try:
        old.rmdir()                                        # удаляется, только если опустела
    except OSError:
        pass
    return moved


def _old_autostart_entries() -> list:
    """Способы удалить старый автозапуск Worklapse (если он был включён)."""
    found = []
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart._RUN_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
                winreg.QueryValueEx(k, OLD_NAME)

            def drop() -> None:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart._RUN_KEY, 0, winreg.KEY_SET_VALUE) as k2:
                    winreg.DeleteValue(k2, OLD_NAME)
            found.append(drop)
        except OSError:
            pass
    else:
        if sys.platform == "darwin":
            f = Path.home() / "Library" / "LaunchAgents" / "io.github.worklapse.plist"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            f = base / "autostart" / "worklapse.desktop"
        if f.exists():
            found.append(lambda f=f: f.unlink(missing_ok=True))
    return found


def run() -> int:
    """Перенос данных. Безопасно вызывать при каждом запуске: если переносить нечего, ничего не делает."""
    moved = 0
    pairs = [
        (Path(user_config_dir(OLD_NAME, appauthor=False)), paths.config_dir()),
        (Path(user_data_dir(OLD_NAME, appauthor=False)), paths.data_dir()),
        (Path(tempfile.gettempdir()) / OLD_NAME, paths.temp_root()),
    ]
    for old, new in pairs:
        n = _move_contents(old, new)
        if n:
            log.info("Перенесено из %s в %s: %s", old, new, n)
        moved += n
    try:
        drops = _old_autostart_entries()
        if drops:
            for drop in drops:
                drop()
            autostart.set_enabled(True)
            log.info("Автозапуск переключён с Worklapse на Glimpsy")
    except Exception:
        log.exception("Не удалось перенести автозапуск")
    remove_voice_module()
    return moved


def remove_voice_module() -> None:
    """Переозвучку голосом убрали из программы: её модуль (~5 ГБ) и сохранённые голоса больше не нужны.
    Удаляем в фоне, чтобы не задерживать запуск."""
    import shutil
    import threading

    olds = [d for d in (paths.data_dir() / "voice", paths.data_dir() / "voices") if d.exists()]
    if not olds:
        return

    def run() -> None:
        for d in olds:
            shutil.rmtree(d, ignore_errors=True)
            log.info("Удалён ненужный голосовой модуль: %s", d)

    threading.Thread(target=run, daemon=True, name="remove-voice").start()
