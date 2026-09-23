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


def normalize(combo: str) -> str:
    """Красивая запись для интерфейса: ctrl+alt+s -> Ctrl+Alt+S."""
    mods, key = parse(combo)
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Cmd"}
    return "+".join([names[m] for m in mods] + [key.upper() if len(key) == 1 else key.capitalize()])
