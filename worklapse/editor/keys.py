"""Горячие клавиши редактора, которые работают в ЛЮБОЙ раскладке.

Обычно Ctrl+Z перестаёт работать, если включена русская раскладка: программа видит
не «Z», а «Я». Поэтому мы смотрим на физическую клавишу (её код от системы),
а не на букву, которую она печатает. Запасной вариант — таблица «русская буква →
латинская на той же клавише».
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent

# Коды физических клавиш
_MAC = {0: "a", 1: "s", 2: "d", 6: "z", 7: "x", 8: "c", 9: "v", 11: "b", 16: "y", 17: "t"}
_X11 = {38: "a", 39: "s", 40: "d", 52: "z", 53: "x", 54: "c", 55: "v", 56: "b", 29: "y", 28: "t"}
# ЙЦУКЕН → QWERTY (та же клавиша)
_CYR = {"ф": "a", "ы": "s", "в": "d", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "н": "y", "е": "t", "у": "e"}


def latin_letter(ev: QKeyEvent) -> str | None:
    """Латинская буква физической клавиши (a–z) или None."""
    if sys.platform.startswith("win"):
        vk = ev.nativeVirtualKey()        # на Windows это код клавиши, не зависящий от раскладки
        if 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
    elif sys.platform == "darwin":
        letter = _MAC.get(ev.nativeVirtualKey())
        if letter:
            return letter
    else:
        letter = _X11.get(ev.nativeScanCode())
        if letter:
            return letter
    k = ev.key()
    if Qt.Key.Key_A <= k <= Qt.Key.Key_Z:
        return chr(k).lower()
    return _CYR.get(ev.text().lower())


def has_ctrl(ev: QKeyEvent) -> bool:
    # На Mac Qt сам превращает Cmd в ControlModifier — так что Cmd+Z тоже работает
    return bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)


def has_shift(ev: QKeyEvent) -> bool:
    return bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
