"""Точка входа: запускает интерфейс, движок записи и иконку в трее."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# mss нужно импортировать до всего остального: на Windows он включает правильную работу
# с масштабированием экрана (DPI), иначе координаты мониторов и курсора не совпадут.
import mss  # noqa: F401

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from glimpsy import APP_NAME, __version__, paths
from glimpsy.config import load_settings, save_settings
from glimpsy.platform import build_services
from glimpsy.recorder.engine import RecorderEngine, find_unfinished_sessions
from glimpsy.ui.icons import state_icon
from glimpsy.ui.tray import TrayController

log = logging.getLogger("glimpsy")


def setup_logging() -> None:
    handler = RotatingFileHandler(paths.log_dir() / "glimpsy.log", maxBytes=2_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr:
        root.addHandler(logging.StreamHandler())
    _setup_crash_reports()


_crash_file = None


def _setup_crash_reports() -> None:
    """Чтобы любое падение оставило след в журнале, даже если программа закрылась сама:
      • ошибки Python — в glimpsy.log;
      • предупреждения и ошибки Qt (видео, звук, графика) — туда же;
      • аварийное падение внутри библиотек — стек в crash.log рядом."""
    global _crash_file
    import faulthandler

    try:
        _crash_file = open(paths.log_dir() / "crash.log", "a", encoding="utf-8")
        _crash_file.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {APP_NAME} {__version__} ===\n")
        _crash_file.flush()
        faulthandler.enable(_crash_file, all_threads=True)
    except OSError:
        pass

    def excepthook(kind, value, tb) -> None:
        logging.getLogger("glimpsy.crash").critical("Необработанная ошибка", exc_info=(kind, value, tb))

    sys.excepthook = excepthook
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    qt_log = logging.getLogger("qt")
    levels = {QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def qt_handler(mode, context, message) -> None:
        level = levels.get(mode)
        if level is not None:
            qt_log.log(level, "%s: %s", context.category or "qt", message)

    qInstallMessageHandler(qt_handler)


def main() -> None:
    paths.clean_child_environment()
    setup_logging()
    log.info("%s %s запускается", APP_NAME, __version__)
    # Linux/X11: OpenGL программе не нужен (кадры рисуются обычным способом). А если он есть,
    # Qt пытается через него готовить кадры видео в редакторе и на некоторых системах
    # (например, Steam Deck) падает целиком: «Could not initialize GLX». Выключаем.
    disable_gl = sys.platform.startswith("linux") and "QT_XCB_GL_INTEGRATION" not in os.environ
    if disable_gl:
        os.environ["QT_XCB_GL_INTEGRATION"] = "none"
    app = QApplication(sys.argv)
    if disable_gl:
        os.environ.pop("QT_XCB_GL_INTEGRATION", None)   # запущенным программам (файловому менеджеру) — как было
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)     # программа живёт в трее
    app.setWindowIcon(state_icon("recording"))

    # Программа уже запущена? Тогда просим её показаться и выходим — две копии не нужны
    if _notify_running_instance(b"editor" if "--editor" in sys.argv else b"show"):
        sys.exit(0)

    ffmpeg = paths.find_executable("ffmpeg")
    if not ffmpeg:
        QMessageBox.critical(None, APP_NAME, "Не найден FFmpeg.\n\nВ готовой сборке он встроен. При запуске из "
                                             "исходников выполните: python scripts/fetch_ffmpeg.py")
        sys.exit(1)

    from glimpsy import migrate
    if migrate.run():
        log.info("Данные Worklapse перенесены в Glimpsy")
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
    from glimpsy import autostart

    autostart.refresh()      # программу могли перенести в другую папку — обновим путь
    settings.launch_at_login = autostart.is_enabled()

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
        if not engine.running:          # когда запись уже идёт, уведомление попало бы в ролик
            tray.show_message("Glimpsy запущен", "Значок — в трее возле часов.")

    if "--editor" in sys.argv:        # ярлык «Glimpsy — редактор»
        tray.open_editor()
    if "--open" in sys.argv[:-1]:      # открыть проект сразу (для проверки): --open ПАПКА_ПРОЕКТА
        QTimer.singleShot(500, lambda: tray._open_project(Path(sys.argv[sys.argv.index("--open") + 1])))

    # Ctrl+C в терминале корректно закрывает программу (удобно при разработке)
    signal.signal(signal.SIGINT, lambda *_: tray.quit())
    timer = QTimer()
    timer.start(300)
    timer.timeout.connect(lambda: None)

    sys.exit(app.exec())


def _instance_name() -> str:
    import getpass

    return f"{APP_NAME}-{getpass.getuser()}"


def _notify_running_instance(command: bytes = b"show") -> bool:
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if not sock.waitForConnected(300):
        return False
    sock.write(command)
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def _listen_for_second_launch(tray: TrayController):
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())   # хвост от аварийно закрытой копии
    if not server.listen(_instance_name()):
        log.warning("Не удалось включить защиту от второй копии: %s", server.errorString())
    def on_connection() -> None:
        conn = server.nextPendingConnection()
        conn.waitForReadyRead(300)
        if bytes(conn.readAll()) == b"editor":
            tray.open_editor()
        else:
            tray.show_already_running()

    server.newConnection.connect(on_connection)
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
