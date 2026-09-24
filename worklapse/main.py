"""Точка входа: запускает интерфейс, движок записи и иконку в трее."""

from __future__ import annotations

import logging
import shutil
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

# mss нужно импортировать до всего остального: на Windows он включает правильную работу
# с масштабированием экрана (DPI), иначе координаты мониторов и курсора не совпадут.
import mss  # noqa: F401

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from worklapse import APP_NAME, __version__, paths
from worklapse.config import load_settings, save_settings
from worklapse.platform import build_services
from worklapse.recorder.engine import RecorderEngine, find_unfinished_sessions
from worklapse.ui.icons import state_icon
from worklapse.ui.tray import TrayController

log = logging.getLogger("worklapse")


def setup_logging() -> None:
    handler = RotatingFileHandler(paths.log_dir() / "worklapse.log", maxBytes=2_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr:
        root.addHandler(logging.StreamHandler())


def main() -> None:
    setup_logging()
    log.info("%s %s запускается", APP_NAME, __version__)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)     # программа живёт в трее
    app.setWindowIcon(state_icon("recording"))

    # Программа уже запущена? Тогда просим её показаться и выходим — две копии не нужны
    if _notify_running_instance():
        sys.exit(0)

    ffmpeg = paths.find_executable("ffmpeg")
    if not ffmpeg:
        QMessageBox.critical(None, APP_NAME, "Не найден FFmpeg.\n\nВ готовой сборке он встроен. При запуске из "
                                             "исходников выполните: python scripts/fetch_ffmpeg.py")
        sys.exit(1)

    settings = load_settings()

    def remember_wayland_token(token: str) -> None:
        settings.wayland_restore_token = token
        save_settings(settings)

    try:
        services = build_services(ffmpeg, settings.capture_backend, settings.wayland_restore_token,
                                  remember_wayland_token)
    except Exception as e:
        log.exception("Платформа не поддерживается")
        QMessageBox.critical(None, APP_NAME, f"Не удалось подготовить запись экрана:\n{e}")
        sys.exit(1)

    engine = RecorderEngine(settings, services, ffmpeg)
    tray = TrayController(app, settings, services, engine)
    server = _listen_for_second_launch(tray)  # noqa: F841 — держим ссылку, пока программа работает

    # Один раз показываем, что на этой системе ограничено и почему
    new_limits = [x for x in services.limitations if x not in settings.shown_limitations]
    if new_limits:
        QMessageBox.information(None, APP_NAME, "Особенности вашей системы:\n\n• " + "\n\n• ".join(new_limits))
        settings.shown_limitations = settings.shown_limitations + new_limits
        save_settings(settings)

    if not _handle_unfinished(engine) and settings.autostart_recording:
        engine.start_session()

    if not settings.welcome_shown:
        tray.show_welcome()
        settings.welcome_shown = True
        save_settings(settings)
    else:
        tray.show_message("Worklapse запущен", "Значок — в трее возле часов. Запись идёт в фоне.")

    # Ctrl+C в терминале корректно закрывает программу (удобно при разработке)
    signal.signal(signal.SIGINT, lambda *_: tray.quit())
    timer = QTimer()
    timer.start(300)
    timer.timeout.connect(lambda: None)

    sys.exit(app.exec())


def _instance_name() -> str:
    import getpass

    return f"{APP_NAME}-{getpass.getuser()}"


def _notify_running_instance() -> bool:
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if not sock.waitForConnected(300):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def _listen_for_second_launch(tray: TrayController):
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())   # хвост от аварийно закрытой копии
    if not server.listen(_instance_name()):
        log.warning("Не удалось включить защиту от второй копии: %s", server.errorString())
    server.newConnection.connect(lambda: (server.nextPendingConnection(), tray.show_already_running()))
    return server


def _handle_unfinished(engine: RecorderEngine) -> bool:
    """Если остались фрагменты с прошлого раза — спрашиваем, что с ними сделать."""
    sessions = find_unfinished_sessions()
    if not sessions:
        return False
    last = sessions[-1]
    when = time.strftime("%d.%m %H:%M", time.localtime(last.stat().st_mtime))
    box = QMessageBox()
    box.setWindowTitle(APP_NAME)
    box.setText(f"Найдена незавершённая сессия ({when}).\nЧто с ней сделать?")
    b_assemble = box.addButton("Собрать ролик", QMessageBox.ButtonRole.AcceptRole)
    b_continue = box.addButton("Продолжить запись", QMessageBox.ButtonRole.ActionRole)
    box.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
    box.exec()
    clicked = box.clickedButton()
    if clicked == b_assemble:
        engine.assemble_existing(last)
        return True
    if clicked == b_continue:
        engine.start_session(existing_dir=last)
        return True
    for d in sessions:   # «Удалить» — убираем все недособранные черновики
        shutil.rmtree(d, ignore_errors=True)
    return False

if __name__ == "__main__":
    main()
