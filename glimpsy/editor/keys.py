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

# Коды физических клавиш (весь алфавит)
_MAC = {0: "a", 11: "b", 8: "c", 2: "d", 14: "e", 3: "f", 5: "g", 4: "h", 34: "i", 38: "j", 40: "k", 37: "l",
        46: "m", 45: "n", 31: "o", 35: "p", 12: "q", 15: "r", 1: "s", 17: "t", 32: "u", 9: "v", 13: "w",
        7: "x", 16: "y", 6: "z"}
_X11 = {38: "a", 56: "b", 54: "c", 40: "d", 26: "e", 41: "f", 42: "g", 43: "h", 31: "i", 44: "j", 45: "k",
        46: "l", 58: "m", 57: "n", 32: "o", 33: "p", 24: "q", 27: "r", 39: "s", 28: "t", 30: "u", 55: "v",
        25: "w", 53: "x", 29: "y", 52: "z"}
# ЙЦУКЕН → QWERTY (та же клавиша)
_CYR = dict(zip("йцукенгшщзфывапролдячсмить", "qwertyuiopasdfghjklzxcvbnm"))


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
