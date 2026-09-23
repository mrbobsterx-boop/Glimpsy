"""Wayland: захват экрана и горячие клавиши через «порталы» xdg-desktop-portal.

На Wayland программы не могут просто так смотреть на чужие окна и слушать клавиатуру —
это защита. Вместо этого система показывает окно «Разрешить запись экрана?», а потом
отдаёт видеопоток через PipeWire. Мы открываем этот поток программой GStreamer
(gst-launch-1.0) и передаём кадры в FFmpeg.

Общение с порталом идёт по D-Bus через чистую Python-библиотеку jeepney.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import threading
from dataclasses import dataclass
from typing import Callable

from worklapse.platform import hotkey_format
from worklapse.platform.base import CaptureBackend, CaptureInput, HotkeyBackend, Monitor

log = logging.getLogger(__name__)

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"


class PortalError(RuntimeError):
    pass


class _Portal:
    """Маленькая обёртка: вызвать метод портала и дождаться ответа (сигнал Response)."""

    def __init__(self) -> None:
        from jeepney.io.blocking import open_dbus_connection

        self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        self.sender = self.conn.unique_name.lstrip(":").replace(".", "_")

    def addr(self, interface: str):
        from jeepney import DBusAddress

        return DBusAddress(PATH, bus_name=BUS, interface=interface)

    def call(self, interface: str, method: str, signature: str, body: tuple):
        from jeepney import MessageType, new_method_call

        reply = self.conn.send_and_get_reply(new_method_call(self.addr(interface), method, signature, body))
        if reply.header.message_type == MessageType.error:
            raise PortalError(f"{interface}.{method}: {reply.body}")
        return reply.body

    def request(self, interface: str, method: str, signature: str, make_body: Callable[[str], tuple],
                timeout: float = 120) -> dict:
        """Вызов с ответом через объект Request (так устроены почти все методы порталов)."""
        from jeepney import MatchRule
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import Proxy

        token = "worklapse_" + secrets.token_hex(4)
        req_path = f"{PATH}/request/{self.sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=req_path)
        Proxy(message_bus, self.conn).AddMatch(rule)
        with self.conn.filter(rule) as queue:
            self.call(interface, method, signature, make_body(token))
            msg = self.conn.recv_until_filtered(queue, timeout=timeout)
        code, results = msg.body
        if code != 0:
            raise PortalError("Пользователь отменил запрос" if code == 1 else f"Портал вернул код {code}")
        return {k: v[1] for k, v in results.items()}  # варианты приходят как (сигнатура, значение)

    def property(self, interface: str, name: str):
        from jeepney import DBusAddress, MessageType, new_method_call

        props = DBusAddress(PATH, bus_name=BUS, interface="org.freedesktop.DBus.Properties")
        reply = self.conn.send_and_get_reply(new_method_call(props, "Get", "ss", (interface, name)))
        if reply.header.message_type == MessageType.error:
            raise PortalError(str(reply.body))
        return reply.body[0][1]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


@dataclass
class _Stream:
    node_id: int
    width: int
    height: int
    x: int
    y: int


class WaylandPortalCapture(CaptureBackend):
    """Захват экрана на Wayland. Монитор выбирается в системном окне разрешения.

    Сессия портала живёт, пока открыто D-Bus-соединение, поэтому храним объект всё время.
    """

    name = "wayland-portal"
    IFACE = "org.freedesktop.portal.ScreenCast"

    def __init__(self, restore_token: str = "", on_token: Callable[[str], None] | None = None) -> None:
        if not shutil.which("gst-launch-1.0"):
            raise PortalError("Не найден GStreamer (gst-launch-1.0). Установите пакеты gstreamer1.0-tools "
                              "и gstreamer1.0-pipewire (названия зависят от дистрибутива).")
        self._restore_token = restore_token
        self._on_token = on_token
        self._portal: _Portal | None = None
        self._streams: list[_Stream] = []
        self._fd: int | None = None
        self._lock = threading.Lock()

    def _ensure_session(self) -> None:
        if self._portal is not None:
            return
        p = _Portal()
        try:
            session = p.request(self.IFACE, "CreateSession", "a{sv}", lambda t: ({
                "handle_token": ("s", t), "session_handle_token": ("s", "worklapse_s" + secrets.token_hex(4)),
            },))["session_handle"]

            try:
                cursor_modes = int(p.property(self.IFACE, "AvailableCursorModes"))
            except Exception:
                cursor_modes = 1
            opts = {
                "types": ("u", 1),              # 1 = мониторы
                "multiple": ("b", False),       # один монитор: мышь на Wayland отследить нельзя
                "persist_mode": ("u", 2),       # запомнить выбор до отзыва пользователем
            }
            if cursor_modes & 2:
                opts["cursor_mode"] = ("u", 2)  # курсор встроен в картинку
            if self._restore_token:
                opts["restore_token"] = ("s", self._restore_token)

            p.request(self.IFACE, "SelectSources", "oa{sv}",
                      lambda t: (session, {**opts, "handle_token": ("s", t)}))
            res = p.request(self.IFACE, "Start", "osa{sv}",
                            lambda t: (session, "", {"handle_token": ("s", t)}), timeout=600)
            token = res.get("restore_token")
            if token and self._on_token:
                self._on_token(token)
            streams = []
            for node_id, props in res.get("streams", []):
                props = {k: v[1] for k, v in props.items()}
                w, h = props.get("size", (1920, 1080))
                x, y = props.get("position", (0, 0))
                streams.append(_Stream(int(node_id), int(w), int(h), int(x), int(y)))
            if not streams:
                raise PortalError("Портал не вернул ни одного экрана")
            fd_obj = p.call(self.IFACE, "OpenPipeWireRemote", "oa{sv}", (session, {}))[0]
            self._fd = fd_obj.to_raw_fd()
            self._streams = streams
            self._portal = p
        except Exception:
            p.close()
            raise

    def monitors(self) -> list[Monitor]:
        with self._lock:
            self._ensure_session()
            return [Monitor(i, s.x, s.y, s.width, s.height) for i, s in enumerate(self._streams, start=1)]

    def input_for(self, monitor: Monitor, fps: int) -> CaptureInput:
        with self._lock:
            self._ensure_session()
            s = self._streams[min(monitor.index, len(self._streams)) - 1]
            w, h = s.width - s.width % 2, s.height - s.height % 2
            producer = [
                "gst-launch-1.0", "-q",
                "pipewiresrc", f"fd={self._fd}", f"path={s.node_id}", "do-timestamp=true",
                "keepalive-time=1000", "always-copy=true",
                "!", "videoconvert", "!", "videoscale", "!", "videorate",
                "!", f"video/x-raw,format=BGRx,width={w},height={h},framerate={fps}/1",
                "!", "fdsink", "fd=1", "sync=false",
            ]
            return CaptureInput(
                args=["-f", "rawvideo", "-pix_fmt", "bgr0", "-video_size", f"{w}x{h}",
                      "-framerate", str(fps), "-i", "pipe:0"],
                width=w, height=h, producer=producer, producer_pass_fds=(self._fd,),
            )

    def close(self) -> None:
        with self._lock:
            if self._portal:
                self._portal.close()
            self._portal = None

    def reset(self) -> None:
        self._restore_token = ""   # в следующий раз система снова покажет окно выбора экрана
        self.close()


class PortalHotkeys(HotkeyBackend):
    """Глобальные горячие клавиши через портал GlobalShortcuts (KDE Plasma, GNOME 48+, Hyprland…)."""

    IFACE = "org.freedesktop.portal.GlobalShortcuts"

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._portal: _Portal | None = None
        self._stop = threading.Event()

    @classmethod
    def available(cls) -> bool:
        try:
            p = _Portal()
            try:
                p.property(cls.IFACE, "version")
                return True
            finally:
                p.close()
        except Exception:
            return False

    def bind(self, bindings: dict[str, tuple[str, Callable[[], None]]]) -> list[str]:
        self.stop()
        errors: list[str] = []
        shortcuts = []
        callbacks: dict[str, Callable[[], None]] = {}
        descriptions = {"important": "Worklapse: важный момент", "pause": "Worklapse: пауза",
                        "finish": "Worklapse: собрать ролик"}
        for sid, (combo, fn) in bindings.items():
            try:
                trigger = hotkey_format.to_portal(combo)
            except hotkey_format.HotkeyError as e:
                errors.append(str(e))
                continue
            shortcuts.append((sid, {"description": ("s", descriptions.get(sid, sid)),
                                    "preferred_trigger": ("s", trigger)}))
            callbacks[sid] = fn
        try:
            p = _Portal()
            session = p.request(self.IFACE, "CreateSession", "a{sv}", lambda t: ({
                "handle_token": ("s", t), "session_handle_token": ("s", "worklapse_k" + secrets.token_hex(4)),
            },))["session_handle"]
            p.request(self.IFACE, "BindShortcuts", "oa(sa{sv})sa{sv}",
                      lambda t: (session, shortcuts, "", {"handle_token": ("s", t)}), timeout=600)
        except Exception as e:
            log.exception("GlobalShortcuts не сработал")
            return errors + [f"Портал горячих клавиш не ответил: {e}"]
        self._portal = p
        self._stop.clear()
        self._thread = threading.Thread(target=self._listen, args=(p, callbacks), daemon=True,
                                        name="portal-hotkeys")
        self._thread.start()
        return errors

    def _listen(self, p: _Portal, callbacks: dict[str, Callable[[], None]]) -> None:
        from collections import deque

        from jeepney import MatchRule
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import Proxy

        rule = MatchRule(type="signal", interface=self.IFACE, member="Activated", path=PATH)
        try:
            Proxy(message_bus, p.conn).AddMatch(rule)
            with p.conn.filter(rule, queue=deque(maxlen=32)) as queue:
                while not self._stop.is_set():
                    try:
                        msg = p.conn.recv_until_filtered(queue, timeout=1.0)
                    except TimeoutError:
                        continue
                    sid = msg.body[1]
                    fn = callbacks.get(sid)
                    if fn:
                        try:
                            fn()
                        except Exception:
                            log.exception("Ошибка в обработчике горячей клавиши")
        except Exception:
            if not self._stop.is_set():
                log.exception("Слушатель горячих клавиш портала остановился")

    def stop(self) -> None:
        self._stop.set()
        if self._portal:
            self._portal.close()
            self._portal = None
