"""Какая программа сейчас в фокусе — для чёрного списка приватности."""

from __future__ import annotations

import logging
import subprocess

from glimpsy.platform.base import ActiveWindowProbe, WindowInfo

log = logging.getLogger(__name__)


def _process_name(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return ""


class WindowsActiveWindow(ActiveWindowProbe):
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._dword = wintypes.DWORD

    def active(self) -> WindowInfo | None:
        try:
            hwnd = self._user32.GetForegroundWindow()
            if not hwnd:
                return WindowInfo()
            length = self._user32.GetWindowTextLengthW(hwnd)
            buf = self._ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = self._dword()
            self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
            return WindowInfo(app=_process_name(pid.value), title=buf.value)
        except Exception:
            log.debug("Не удалось определить активное окно", exc_info=True)
            return None


class MacActiveWindow(ActiveWindowProbe):
    """Имя программы — через NSWorkspace, заголовок окна — через Quartz
    (заголовки видны, когда выдано разрешение «Запись экрана», а оно у нас и так есть)."""

    def __init__(self) -> None:
        try:
            from AppKit import NSWorkspace  # pyobjc

            self._ws = NSWorkspace.sharedWorkspace()
        except Exception:
            self._ws = None

    def active(self) -> WindowInfo | None:
        try:
            if self._ws is None:
                out = subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to get name of first '
                                        'application process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2,
                )
                return WindowInfo(app=out.stdout.strip())
            app = self._ws.frontmostApplication()
            if app is None:
                return WindowInfo()
            name = f"{app.localizedName() or ''} {app.bundleIdentifier() or ''}".strip()
            return WindowInfo(app=name, title=self._front_title(int(app.processIdentifier())))
        except Exception:
            log.debug("Не удалось определить активное окно", exc_info=True)
            return None

    @staticmethod
    def _front_title(pid: int) -> str:
        try:
            import Quartz

            opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
                if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
                    return str(w.get("kCGWindowName") or "")
        except Exception:
            pass
        return ""


class X11ActiveWindow(ActiveWindowProbe):
    def __init__(self) -> None:
        from Xlib import X, display  # python-xlib

        self._X = X
        self._disp = display.Display()
        self._root = self._disp.screen().root
        self._a_active = self._disp.intern_atom("_NET_ACTIVE_WINDOW")
        self._a_name = self._disp.intern_atom("_NET_WM_NAME")
        self._a_pid = self._disp.intern_atom("_NET_WM_PID")
        self._a_utf8 = self._disp.intern_atom("UTF8_STRING")

    def active(self) -> WindowInfo | None:
        try:
            prop = self._root.get_full_property(self._a_active, self._X.AnyPropertyType)
            if not prop or not prop.value or not prop.value[0]:
                return WindowInfo()
            win = self._disp.create_resource_object("window", prop.value[0])
            wm_class = win.get_wm_class() or ("", "")
            name_prop = win.get_full_property(self._a_name, self._a_utf8)
            title = ""
            if name_prop and name_prop.value:
                v = name_prop.value
                title = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            pid_prop = win.get_full_property(self._a_pid, self._X.AnyPropertyType)
            proc = _process_name(int(pid_prop.value[0])) if pid_prop and pid_prop.value else ""
            return WindowInfo(app=f"{proc} {' '.join(wm_class)}".strip(), title=title)
        except Exception:
            log.debug("Не удалось определить активное окно", exc_info=True)
            return None


class NullActiveWindow(ActiveWindowProbe):
    """Wayland не позволяет программам узнавать чужие окна — так задумано ради безопасности."""

    supported = False

    def active(self) -> WindowInfo | None:
        return None

