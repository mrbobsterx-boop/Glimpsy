"""Перевод сочетаний клавиш из «человеческого» вида (Ctrl+Alt+S) в форматы библиотек.

В настройках хранится понятная запись вроде "Ctrl+Alt+S". Библиотеке pynput нужна
"<ctrl>+<alt>+s", а порталу Wayland — "CTRL+ALT+s".
"""

from __future__ import annotations

_MODS = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "win": "cmd", "super": "cmd", "meta": "cmd",
}
_NAMED = {
    "space": "space", "enter": "enter", "return": "enter", "tab": "tab",
    "esc": "esc", "escape": "esc", "home": "home", "end": "end",
    "pageup": "page_up", "pagedown": "page_down", "insert": "insert", "delete": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
}


class HotkeyError(ValueError):
    pass


def parse(combo: str) -> tuple[list[str], str]:
    """'Ctrl+Alt+S' -> (['ctrl','alt'], 's'). Бросает HotkeyError при ошибке."""
    parts = [p.strip() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("Пустое сочетание клавиш")
    mods: list[str] = []
    key = None
    for p in parts:
        low = p.lower()
        if low in _MODS:
            m = _MODS[low]
            if m not in mods:
                mods.append(m)
        elif key is None:
            if len(low) == 1 and (low.isalnum() or low in ",./;'[]-=`"):
                key = low
            elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
                key = low
            elif low in _NAMED:
                key = _NAMED[low]
            else:
                raise HotkeyError(f"Неизвестная клавиша: {p}")
        else:
            raise HotkeyError(f"В сочетании «{combo}» больше одной обычной клавиши")
    if key is None:
        raise HotkeyError(f"В сочетании «{combo}» нет обычной клавиши (только модификаторы)")
    if not mods:
        raise HotkeyError(f"Сочетание «{combo}» должно содержать Ctrl, Alt, Shift или Cmd")
    return mods, key


def to_pynput(combo: str) -> str:
    mods, key = parse(combo)
    key_part = key if len(key) == 1 else f"<{key}>"
    return "+".join([f"<{m}>" for m in mods] + [key_part])


def to_portal(combo: str) -> str:
    """Формат XDG shortcuts: CTRL+ALT+s, LOGO для Win/Cmd."""
    mods, key = parse(combo)
    names = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "cmd": "LOGO"}
    return "+".join([names[m] for m in mods] + [key.upper() if len(key) > 1 else key])


_WIN_VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "home": 0x24, "end": 0x23,
    "page_up": 0x21, "page_down": 0x22, "insert": 0x2D, "delete": 0x2E,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    # знаки на американской раскладке (OEM-клавиши)
    ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, ";": 0xBA, "=": 0xBB,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}


def to_win32(combo: str) -> tuple[int, int]:
    """Для WinAPI RegisterHotKey: (модификаторы, код клавиши)."""
    mods, key = parse(combo)
    bits = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "cmd": 0x8}
    m = 0
    for x in mods:
        m |= bits[x]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        vk = 0x70 + int(key[1:]) - 1
    else:
        vk = _WIN_VK[key]
    return m, vk


def normalize(combo: str) -> str:
    """Красивая запись для интерфейса: ctrl+alt+s -> Ctrl+Alt+S."""
    mods, key = parse(combo)
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Cmd"}
    return "+".join([names[m] for m in mods] + [key.upper() if len(key) == 1 else key.capitalize()])
