"""Иконка в трее — главный «пульт управления» программой."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication, QLabel, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from worklapse import paths
from worklapse.config import Settings, save_settings
from worklapse.platform.base import PlatformServices
from worklapse.recorder.engine import RecorderEngine, State
from worklapse.ui.icons import state_icon
from worklapse.ui.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


class TrayController(QObject):
    def __init__(self, app: QApplication, settings: Settings, services: PlatformServices,
                 engine: RecorderEngine) -> None:
        super().__init__()
        self.app = app
        self.s = settings
        self.services = services
        self.engine = engine
        self.status: dict = {"state": State.STOPPED, "label": "Запись остановлена",
                             "needed": engine.plan.clips_needed}
        self._last_video: Path | None = None
        self._settings_open = False
        self._sessions = None
        self._editors: list = []

        self.menu = QMenu()
        self.a_status = self.menu.addAction("…")
        self.a_status.setEnabled(False)
        self.a_counts = self.menu.addAction("")
        self.a_counts.setEnabled(False)
        self.menu.addSeparator()
        self.a_pause = self.menu.addAction("Пауза", self.engine.toggle_pause)
        self.a_important = self.menu.addAction("⭐ Отметить важный момент", self.engine.mark_important)
        self.a_finish = self.menu.addAction("🎬 Завершить и собрать ролик", self._finish)
        self.a_start = self.menu.addAction("▶ Начать запись", self.engine.start_session)
        self.menu.addSeparator()
        self.monitor_menu = self.menu.addMenu("Монитор")
        self.monitor_menu.aboutToShow.connect(self._fill_monitor_menu)
        self.menu.addAction("Настройки…", self.open_settings)
        self.menu.addAction("Открыть папку с роликами", lambda: paths.open_in_file_manager(Path(self.s.output_dir)))
        self.menu.addAction("🎞 Редактор роликов…", self.open_editor)
        self.menu.addSeparator()
        self.menu.addAction("Выход", self.quit)

        self.tray: QSystemTrayIcon | None = None
        self.window: QWidget | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(state_icon(State.STOPPED))
            self.tray.setContextMenu(self.menu)
            self.tray.setToolTip("Worklapse")
            self.tray.messageClicked.connect(self._open_last_video)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()
        else:
            # Например, GNOME без расширения AppIndicator — показываем маленькое окно-пульт
            self.window = self._fallback_window()
            self.window.show()

        engine.status_changed.connect(self._on_status)
        engine.notify.connect(self.show_message)
        engine.assembly_progress.connect(self._on_progress)
        engine.assembly_done.connect(self._on_done)
        engine.assembly_failed.connect(lambda msg: self.show_message("Не удалось собрать ролик", msg))
        self._on_status(self.status)
        self.bind_hotkeys()

    # ---------- горячие клавиши ----------

    def bind_hotkeys(self) -> None:
        errors = self.services.hotkeys.bind({
            "important": (self.s.hotkey_important, self.engine.mark_important),
            "pause": (self.s.hotkey_pause, self.engine.toggle_pause),
            "finish": (self.s.hotkey_finish, self.engine.finish),
        })
        for e in errors:
            self.show_message("Горячие клавиши", e)

    # ---------- состояние ----------

    def _on_status(self, st: dict) -> None:
        self.status = st
        state = st.get("state", State.STOPPED)
        label = st.get("label", "")
        if st.get("monitor") and state == State.RECORDING:
            label += f" — {st['monitor']}"
        self.a_status.setText(label)
        n, need, imp = st.get("candidates", 0), st.get("needed", 0), st.get("important", 0)
        self.a_counts.setText(f"Фрагментов: {n} (нужно ~{need})" + (f", важных: {imp}" if imp else ""))
        active = state not in (State.STOPPED, State.ASSEMBLING)
        self.a_pause.setText("▶ Продолжить" if state == State.PAUSED else "⏸ Пауза")
        for a in (self.a_pause, self.a_important, self.a_finish):
            a.setVisible(active)
        self.a_start.setVisible(state == State.STOPPED)
        tip = f"Worklapse — {label}\n{self.a_counts.text()}"
        if st.get("error"):
            tip += f"\n{st['error'][:200]}"
        if self.tray:
            self.tray.setIcon(state_icon(state))
            self.tray.setToolTip(tip)
        if self.window:
            self._win_status.setText(tip)
            self.window.setWindowIcon(state_icon(state))

    def _on_progress(self, frac: float, text: str) -> None:
        tip = f"Worklapse — собираю ролик: {int(frac * 100)}% ({text})"
        if self.tray:
            self.tray.setToolTip(tip)
        if self.window:
            self._win_status.setText(tip)

    def _on_done(self, path: str) -> None:
        self._last_video = Path(path)
        self.show_message("🎬 Ролик готов!", f"{Path(path).name}\nНажмите, чтобы открыть папку.")

    def _open_last_video(self) -> None:
        if self._last_video:
            paths.open_in_file_manager(self._last_video.parent)

    def show_message(self, title: str, text: str) -> None:
        log.info("%s: %s", title, text)
        if self.tray and self.tray.supportsMessages():
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 6000)
        elif self.window:
            self._win_status.setText(f"{title}\n{text}")

    def show_welcome(self) -> None:
        """Первый запуск: объясняем, что программа живёт в трее и где её искать."""
        where = ("в правом нижнем углу, возле часов. Если его не видно — нажмите стрелку «^» "
                 "рядом с часами. Чтобы значок был виден всегда, перетащите его из этого "
                 "меню на панель задач." if paths.IS_WINDOWS else
                 "в строке меню вверху экрана." if paths.IS_MAC else "в системном трее.")
        box = QMessageBox()
        box.setWindowTitle("Worklapse")
        box.setIconPixmap(state_icon(State.RECORDING).pixmap(48, 48))
        box.setText("<b>Worklapse запущен и записывает экран в фоне.</b>")
        box.setInformativeText(
            f"У программы нет большого окна — только значок {where}\n\n"
            "Нажмите на значок правой кнопкой: там пауза, настройки и сборка ролика.\n\n"
            f"Горячие клавиши:\n"
            f"  {self.s.hotkey_important} — важный момент\n"
            f"  {self.s.hotkey_pause} — пауза / продолжить\n"
            f"  {self.s.hotkey_finish} — завершить и собрать ролик")
        b_settings = box.addButton("Открыть настройки", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Понятно", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() == b_settings:
            self.open_settings()

    def show_already_running(self) -> None:
        """Пользователь запустил программу ещё раз — показываем, что она уже работает."""
        self.show_message("Worklapse уже запущен", "Значок — в трее возле часов. Открываю настройки.")
        if self.window:
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
        else:
            self.open_settings()

    # ---------- действия ----------

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    def _finish(self) -> None:
        ans = QMessageBox.question(None, "Worklapse", "Завершить сессию и собрать ролик?")
        if ans == QMessageBox.StandardButton.Yes:
            self.engine.finish()

    def _fill_monitor_menu(self) -> None:
        self.monitor_menu.clear()
        group = QActionGroup(self.monitor_menu)
        auto = QAction("Автоматически (под курсором)", self.monitor_menu, checkable=True)
        auto.setChecked(self.s.monitor_mode == "auto")
        auto.setEnabled(self.services.cursor.supported)
        auto.triggered.connect(lambda: self._set_monitor("auto", self.s.manual_monitor))
        group.addAction(auto)
        self.monitor_menu.addAction(auto)
        for m in self.engine._monitors:
            a = QAction(m.label, self.monitor_menu, checkable=True)
            a.setChecked(self.s.monitor_mode == "manual" and self.s.manual_monitor == m.index)
            a.triggered.connect(lambda _=False, i=m.index: self._set_monitor("manual", i))
            group.addAction(a)
            self.monitor_menu.addAction(a)
        if not self.services.cursor.supported:
            self.monitor_menu.addSeparator()
            self.monitor_menu.addAction("Выбрать экран заново…", self.engine.reselect_screen)

    def _set_monitor(self, mode: str, index: int) -> None:
        import copy

        s = copy.deepcopy(self.s)
        s.monitor_mode, s.manual_monitor = mode, index
        self._apply(s)

    def open_editor(self) -> None:
        """Список сессий → выбранная открывается в редакторе."""
        from worklapse.editor.sessions import SessionsDialog

        if self._sessions is None:
            self._sessions = SessionsDialog(self.engine.ffmpeg, self._open_project)
        else:
            self._sessions.reload()
        self._sessions.show()
        self._sessions.raise_()
        self._sessions.activateWindow()

    def _open_project(self, project_dir: Path) -> None:
        from worklapse.editor.window import EditorWindow

        for w in list(self._editors):          # уже открыт — просто показываем
            if w.project.dir == project_dir and w.isVisible():
                w.raise_()
                w.activateWindow()
                return
        self._editors = [w for w in self._editors if w.isVisible()]
        try:
            w = EditorWindow(project_dir, self.engine.ffmpeg, self._encoder_for_export, Path(self.s.output_dir))
        except Exception as e:
            log.exception("Редактор не открылся")
            QMessageBox.warning(None, "Worklapse", f"Не удалось открыть проект:\n{e}")
            return
        w.setWindowIcon(state_icon(State.RECORDING))
        self._editors.append(w)
        w.show()
        if self._sessions:
            self._sessions.close()

    def _encoder_for_export(self):
        from worklapse.recorder.encoder import pick_encoder

        return self.engine.encoder or pick_encoder(self.engine.ffmpeg, self.s.encoder, self.s.fps)

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        try:
            dlg = SettingsDialog(self.s, self.services, list(self.engine._monitors), self.status,
                                 on_reselect_screen=self.engine.reselect_screen)
            dlg.setWindowIcon(state_icon(self.status.get("state", State.STOPPED)))
            dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            if dlg.exec():
                self._apply(dlg.result_settings())
        finally:
            self._settings_open = False

    def _apply(self, new: Settings) -> None:
        hotkeys_changed = (new.hotkey_important, new.hotkey_pause, new.hotkey_finish) != (
            self.s.hotkey_important, self.s.hotkey_pause, self.s.hotkey_finish)
        # обновляем объект настроек «на месте», чтобы все ссылки видели новые значения
        self.s.__dict__.update(new.__dict__)
        save_settings(self.s)
        self.engine.apply_settings(self.s)
        if hotkeys_changed:
            self.bind_hotkeys()

    def quit(self) -> None:
        if self.engine.pool and self.engine.pool.count:
            ans = QMessageBox.question(
                None, "Worklapse",
                "Выйти без сборки ролика?\nСохранённые фрагменты останутся — при следующем запуске "
                "можно будет собрать ролик или продолжить запись.")
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.services.hotkeys.stop()
        self.engine.shutdown()
        self.services.capture.close()
        if self.tray:
            self.tray.hide()
        self.app.quit()

    # ---------- запасное окно без трея ----------

    def _fallback_window(self) -> QWidget:
        w = QWidget()
        w.setWindowTitle("Worklapse")
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Системный трей недоступен, поэтому Worklapse показывает это окно.\n"
                           "Его можно свернуть — запись продолжится."))
        self._win_status = QLabel()
        self._win_status.setWordWrap(True)
        v.addWidget(self._win_status)
        for text, fn in (("⏸ Пауза / продолжить", self.engine.toggle_pause),
                         ("⭐ Важный момент", self.engine.mark_important),
                         ("🎬 Собрать ролик", self._finish),
                         ("▶ Начать запись", self.engine.start_session),
                         ("🎞 Редактор роликов…", self.open_editor),
                         ("Настройки…", self.open_settings),
                         ("Выход", self.quit)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            v.addWidget(b)

        def close_event(e) -> None:  # крестик сворачивает окно, а не закрывает программу
            e.ignore()
            w.showMinimized()

        w.closeEvent = close_event  # type: ignore[method-assign]
        return w
