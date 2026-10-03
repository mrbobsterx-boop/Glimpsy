"""Иконка в трее — главный «пульт управления» программой."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication, QLabel, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from glimpsy import paths
from glimpsy.config import Settings, save_settings
from glimpsy.platform.base import PlatformServices
from glimpsy.recorder.engine import RecorderEngine, State
from glimpsy.ui import theme
from glimpsy.ui.icons import app_logo, state_icon
from glimpsy.ui.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


class TrayController(QObject):
    _prompter_cmd = Signal(str)          # горячие клавиши суфлёра (из потока клавиатуры) → поток интерфейса

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
        self._picker = None
        self._stream_next = 1
        self._prompter = None
        self._prompter_cmd.connect(self._prompter_run)

        self.menu = QMenu()
        ic = theme.icon
        self.a_update = self.menu.addAction(ic("download", theme.ACCENT_HOVER, 16), "Обновить Glimpsy — есть новая версия",
                                            lambda: self.updates.show_dialog())
        self.a_update.setVisible(False)
        self.a_status = self.menu.addAction("…")
        self.a_status.setEnabled(False)
        self.a_counts = self.menu.addAction("")
        self.a_counts.setEnabled(False)
        self.menu.addSeparator()
        self.a_pause = self.menu.addAction(ic("pause", size=16), "Пауза", self.engine.toggle_pause)
        self.a_important = self.menu.addAction(ic("star", "#F5C518", 16), "Отметить важный момент",
                                               self.engine.mark_important)
        self.a_finish = self.menu.addAction(ic("film", theme.ACCENT_HOVER, 16), "Завершить и собрать ролик",
                                            self._finish)
        self.a_start = self.menu.addAction(ic("circle", theme.DANGER, 16), "Начать запись", self.engine.start_session)
        self.menu.addSeparator()
        self.streams_menu = self.menu.addMenu(ic("app-window", size=16), "Что записывать")
        self.streams_menu.aboutToShow.connect(self._fill_streams_menu)
        self.monitor_menu = self.menu.addMenu(ic("monitor", size=16), "Монитор")
        self.monitor_menu.aboutToShow.connect(self._fill_monitor_menu)
        self.menu.addAction(ic("type", size=16), "Суфлёр…", lambda: self.prompter().toggle_visible())
        self.menu.addAction(ic("film", size=16), "Редактор роликов…", self.open_editor)
        self.menu.addAction(ic("folder-open", size=16), "Папка с роликами",
                            lambda: paths.open_in_file_manager(Path(self.s.output_dir)))
        self.menu.addAction(ic("settings", size=16), "Настройки…", self.open_settings)
        self.menu.addAction(ic("trash-2", size=16), "Очистить кэш…", self.clear_cache)
        self.menu.addAction(ic("rotate-ccw", size=16), "Проверить обновления…", lambda: self.updates.check(manual=True))
        self.menu.addSeparator()
        self.menu.addAction(ic("power", size=16), "Выход", self.quit)

        self.tray: QSystemTrayIcon | None = None
        self.window: QWidget | None = None
        self._muted: list[tuple[str, str]] = []
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(state_icon(State.STOPPED))
            self.tray.setContextMenu(self.menu)
            self.tray.setToolTip("Glimpsy")
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
        from glimpsy.ui.update import UpdateManager

        self.updates = UpdateManager(self)
        self.updates.available.connect(lambda _rel: self.a_update.setVisible(True))

    # ---------- горячие клавиши ----------

    def bind_hotkeys(self) -> None:
        errors = self.services.hotkeys.bind({
            "important": (self.s.hotkey_important, self.engine.mark_important),
            "pause": (self.s.hotkey_pause, self.engine.toggle_pause),
            "finish": (self.s.hotkey_finish, self.engine.finish),
            # суфлёр: горячие клавиши приходят из другого потока — выполняем в потоке интерфейса
            **{k: (getattr(self.s, f"hotkey_prompter_{k[3:]}"), self._prompter_call(k[3:]))
               for k in ("pr_show", "pr_play", "pr_slower", "pr_faster", "pr_back", "pr_lock", "pr_top")},
        })
        for e in errors:
            self.show_message("Горячие клавиши", e)

    # ---------- состояние ----------

    def _on_status(self, st: dict) -> None:
        self.status = st
        state = st.get("state", State.STOPPED)
        label = st.get("label", "")
        if st.get("stream") and state == State.RECORDING:
            label += f" — «{st['stream']}»"
        elif st.get("monitor") and state == State.RECORDING:
            label += f" — {st['monitor']}"
        self.a_status.setText(label)
        n, need, imp = st.get("candidates", 0), st.get("needed", 0), st.get("important", 0)
        self.a_counts.setText(f"Фрагментов: {n} (нужно ~{need})" + (f", важных: {imp}" if imp else ""))
        active = state not in (State.STOPPED, State.ASSEMBLING)
        self.a_pause.setText("Продолжить" if state == State.PAUSED else "Пауза")
        self.a_pause.setIcon(theme.icon("play" if state == State.PAUSED else "pause", size=16))
        for a in (self.a_pause, self.a_important, self.a_finish):
            a.setVisible(active)
        self.a_start.setVisible(state == State.STOPPED)
        tip = f"Glimpsy — {label}\n{self.a_counts.text()}"
        if st.get("parallel"):
            tip += "\nПараллельно пишутся: " + ", ".join(st["parallel"])
        if st.get("voice"):
            tip += "\n🔴 Пишется голос"
        if st.get("error"):
            tip += f"\n{st['error'][:200]}"
        if self.tray:
            self.tray.setIcon(state_icon(state, voice=st.get("voice", False)))
            self.tray.setToolTip(tip)
        if self.window:
            self._win_status.setText(tip)
            self.window.setWindowIcon(state_icon(state))
        if not self._quiet():
            QTimer.singleShot(0, self._flush_muted)

    def _flash_star(self) -> None:
        if self.tray:
            self.tray.setIcon(state_icon("important"))
            QTimer.singleShot(1500, lambda: self.tray and self.tray.setIcon(
                state_icon(self.status.get("state", State.STOPPED), voice=self.status.get("voice", False))))

    def _flush_muted(self) -> None:
        if not self._muted:
            return
        items, self._muted = self._muted[-5:], []
        if len(items) == 1:
            self.show_message(*items[0], force=True)
        else:
            self.show_message("Пока шла запись", "\n".join(f"• {t}: {x}" for t, x in items), force=True)

    def _on_progress(self, frac: float, text: str) -> None:
        tip = f"Glimpsy — собираю ролик: {int(frac * 100)}% ({text})"
        if self.tray:
            self.tray.setToolTip(tip)
        if self.window:
            self._win_status.setText(tip)

    def _on_done(self, path: str) -> None:
        files = [Path(p) for p in path.split("\n") if p]
        if not files:
            return
        self._last_video = files[0]
        if len(files) == 1:
            self.show_message("🎬 Ролик готов!", f"{files[0].name}\nНажмите, чтобы открыть папку.")
        else:
            self.show_message(f"🎬 Готово роликов: {len(files)}",
                              "\n".join(f.name for f in files) + "\nНажмите, чтобы открыть папку.")

    def _open_last_video(self) -> None:
        if self._last_video:
            paths.open_in_file_manager(self._last_video.parent)

    QUIET_STATES = (State.STARTING, State.RECORDING, State.IDLE, State.PRIVATE, State.WAITING)

    def _quiet(self) -> bool:
        """Идёт запись — всплывающие уведомления попали бы в ролик, поэтому молчим."""
        return self.engine.running and self.status.get("state") in self.QUIET_STATES

    def show_message(self, title: str, text: str, force: bool = False) -> None:
        log.info("%s: %s", title, text)
        if not force and self._quiet():
            if "Важный момент отмечен" in text:
                self._flash_star()                  # вместо уведомления — звёздочка на значке
            else:
                self._muted.append((title, text))   # покажем, когда запись встанет на паузу или закончится
            return
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
        from PySide6.QtWidgets import QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

        from glimpsy import autostart

        dlg = QDialog()
        dlg.setWindowTitle("Glimpsy")
        dlg.setMinimumWidth(460)
        logo = QLabel()
        logo.setPixmap(app_logo(64))
        title = theme.mark(QLabel("Glimpsy записывает экран в фоне"), "h1")
        title.setWordWrap(True)
        sub = theme.mark(QLabel(f"Большого окна нет — только значок {where} Нажмите на него: там пауза, "
                                "настройки, редактор и сборка ролика."), "muted")
        sub.setWordWrap(True)
        keys = QGridLayout()
        keys.setHorizontalSpacing(12)
        keys.setVerticalSpacing(8)
        for i, (combo, text) in enumerate(((self.s.hotkey_important, "важный момент — точно попадёт в ролик"),
                                           (self.s.hotkey_pause, "пауза / продолжить"),
                                           (self.s.hotkey_finish, "завершить и собрать ролик"))):
            keys.addWidget(theme.mark(QLabel(combo), "kbd"), i, 0)
            keys.addWidget(QLabel(text), i, 1)
        keys.setColumnStretch(1, 1)
        login = QCheckBox("Запускать Glimpsy вместе с компьютером")
        login.setChecked(True)
        b_settings = theme.mark(QPushButton("Настройки"), "ghost")
        b_ok = theme.mark(QPushButton("Понятно"), "primary")
        b_ok.setDefault(True)
        opened = {"settings": False}
        b_ok.clicked.connect(dlg.accept)
        b_settings.clicked.connect(lambda: (opened.__setitem__("settings", True), dlg.accept()))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(b_settings)
        buttons.addWidget(b_ok)
        head = QHBoxLayout()
        head.setSpacing(16)
        head.addWidget(logo, alignment=Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(6)
        text.addWidget(title)
        text.addWidget(sub)
        head.addLayout(text, 1)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(14)
        lay.addLayout(head)
        lay.addWidget(theme.mark(QLabel("Горячие клавиши"), "section"))
        lay.addLayout(keys)
        lay.addWidget(login)
        lay.addLayout(buttons)
        dlg.exec()
        err = autostart.set_enabled(login.isChecked())
        self.s.launch_at_login = autostart.is_enabled()
        if err:
            self.show_message("Автозапуск", f"Не удалось включить автозапуск: {err}")
        if opened["settings"]:
            self.open_settings()

    def show_already_running(self) -> None:
        """Пользователь запустил программу ещё раз — показываем, что она уже работает."""
        self.show_message("Glimpsy уже запущен", "Значок — в трее возле часов. Открываю настройки.")
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
        ans = QMessageBox.question(None, "Glimpsy", "Завершить сессию и собрать ролик?")
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

    # ---------- что записывать: весь экран или только выбранные окна ----------

    def _fill_streams_menu(self) -> None:
        from glimpsy.recorder.streams import MAX_STREAMS
        from glimpsy.ui.stream_picker import describe

        m = self.streams_menu
        m.clear()
        specs = self.engine.streams
        whole = QAction("Обычная запись — весь экран", m, checkable=True)
        whole.setChecked(not specs)
        whole.triggered.connect(lambda: self.engine.set_streams([]))
        m.addAction(whole)
        if specs:
            m.addSeparator()
            for sp in specs:
                sub = m.addMenu(theme.icon("app-window", theme.ACCENT, 16), describe(sp))
                sub.addAction(theme.icon("x", size=16), "Больше не записывать",
                              lambda _=False, i=sp.id: self._remove_stream(i))
        m.addSeparator()
        add = m.addAction(theme.icon("plus", size=16),
                          "Только одно окно…" if not specs else "Ещё одно окно (свой ролик)…", self._add_stream)
        add.setEnabled(len(specs) < MAX_STREAMS and self.services.active_window.supported)
        taken = {sp.monitor for sp in specs if sp.is_screen}
        for mon in self._monitor_list():
            if mon.index in taken:
                continue
            name = "Весь экран" if len(self._monitor_list()) == 1 else f"Экран {mon.label}"
            a = m.addAction(theme.icon("monitor", size=16), f"{name} — параллельно, свой ролик",
                            lambda _=False, mn=mon: self._add_screen_stream(mn))
            a.setEnabled(len(specs) < MAX_STREAMS)
        hint = m.addAction(f"До {MAX_STREAMS} записей сразу, каждая — в свой ролик. Экраны пишутся всё "
                           "время; окно — пока оно впереди. Звук и камера — только у окна (или у "
                           "экрана под курсором, если окон нет).")
        hint.setEnabled(False)

    def _monitor_list(self) -> list:
        mons = list(self.engine._monitors)
        if not mons:
            try:
                mons = self.services.capture.monitors()
            except Exception:
                log.debug("Список мониторов недоступен", exc_info=True)
        return mons

    def _add_screen_stream(self, mon) -> None:
        from glimpsy.recorder.streams import StreamSpec

        sid = self._stream_next
        self._stream_next += 1
        name = "Весь экран" if len(self._monitor_list()) == 1 else f"Экран {mon.label}"
        self.engine.set_streams([*self.engine.streams, StreamSpec(sid, name, mode="screen", monitor=mon.index)])
        if not self.engine.running:
            self.engine.start_session()

    # ---------- суфлёр ----------

    def prompter(self):
        from glimpsy.ui.prompter import Prompter

        if self._prompter is None:
            def speaking() -> bool:
                a = self.engine.audio
                return a is not None and a.voice.speaking().speaking
            self._prompter = Prompter(is_speaking=speaking)
            self._prompter.masks_changed.connect(self.engine.set_masks)
        return self._prompter

    def _prompter_call(self, what: str):
        return lambda: self._prompter_cmd.emit(what)

    def _prompter_run(self, what: str) -> None:
        p = self.prompter()
        if what == "show":
            p.toggle_visible()
            return
        if not p.isVisible():
            p.show_prompter()
        {"play": p.toggle_play, "slower": p.slower, "faster": p.faster, "back": p.back,
         "lock": p.toggle_locked, "top": p.to_top}[what]()

    def _add_stream(self) -> None:
        from glimpsy.ui.stream_picker import StreamPicker

        if self._picker is not None:
            self._picker.showNormal()
            self._picker.raise_()
            return
        sid = self._stream_next
        self._stream_next += 1

        def done(spec) -> None:
            self.engine.set_streams([*self.engine.streams, spec])
            if not self.engine.running:
                self.engine.start_session()

        self._picker = StreamPicker(self.services.active_window, sid, done, first=not self.engine.streams)
        self._picker.destroyed.connect(lambda: setattr(self, "_picker", None))
        self._picker.show()

    def _remove_stream(self, sid: int) -> None:
        self.engine.set_streams([sp for sp in self.engine.streams if sp.id != sid])

    def _set_monitor(self, mode: str, index: int) -> None:
        import copy

        s = copy.deepcopy(self.s)
        s.monitor_mode, s.manual_monitor = mode, index
        self._apply(s)

    def open_editor(self) -> None:
        """Список сессий → выбранная открывается в редакторе."""
        from glimpsy.editor.sessions import SessionsDialog

        if self._sessions is None:
            self._sessions = SessionsDialog(self.engine.ffmpeg, self._open_project)
        else:
            self._sessions.reload()
        self._sessions.show()
        self._sessions.raise_()
        self._sessions.activateWindow()

    def _open_project(self, project_dir: Path) -> None:
        from glimpsy.editor.window import EditorWindow

        for w in list(self._editors):          # уже открыт — просто показываем
            if w.project.dir == project_dir and w.isVisible():
                w.raise_()
                w.activateWindow()
                return
        self._editors = [w for w in self._editors if w.isVisible()]
        try:
            w = EditorWindow(project_dir, self.engine.ffmpeg, self._encoder_for_export, Path(self.s.output_dir),
                             on_sessions=self.open_editor, on_open=self._open_project)
        except Exception as e:
            log.exception("Редактор не открылся")
            QMessageBox.warning(None, "Glimpsy", f"Не удалось открыть проект:\n{e}")
            return
        w.setWindowIcon(state_icon(State.RECORDING))
        self._editors.append(w)
        w.show()
        if self._sessions:
            self._sessions.close()

    def clear_cache(self) -> None:
        """Удалить фрагменты проектов и временные файлы. Готовые ролики остаются."""
        from glimpsy import cache

        keep = self.engine.session_dir if self.engine.running else None
        report = cache.scan(keep, Path(self.s.output_dir))
        if report.total_bytes == 0:
            QMessageBox.information(None, "Glimpsy", "Кэш уже пуст.")
            return
        box = QMessageBox()
        box.setWindowTitle("Очистить кэш")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Освободится {cache.human(report.total_bytes)}.")
        box.setInformativeText(
            f"• Проекты редактора: {report.projects} (фрагменты и правки, {cache.human(report.project_bytes)}). "
            f"После очистки их нельзя будет открыть в редакторе.\n"
            f"• Миниатюры и временные файлы: {cache.human(report.other_bytes)}.\n\n"
            f"Готовые и экспортированные ролики в папке «{self.s.output_dir}» останутся."
            + ("\n\nИдущая сейчас запись не затрагивается." if keep else ""))
        b_ok = box.addButton("Очистить", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() != b_ok:
            return
        for w in self._editors:        # открытые проекты сначала закрываем (файлы заняты плеером)
            w.close()
        self._editors = []
        if self._sessions is not None:
            self._sessions.close()
        freed = cache.clear(report)
        self.show_message("Кэш очищен", f"Освобождено {cache.human(freed)}.")

    def _encoder_for_export(self):
        from glimpsy.recorder.encoder import pick_encoder

        return self.engine.encoder or pick_encoder(self.engine.ffmpeg, self.s.encoder, self.s.fps)

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        try:
            dlg = SettingsDialog(self.s, self.services, list(self.engine._monitors), self.status,
                                 on_reselect_screen=self.engine.reselect_screen, ffmpeg=self.engine.ffmpeg)
            dlg.setWindowIcon(state_icon(self.status.get("state", State.STOPPED)))
            dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            if dlg.exec():
                self._apply(dlg.result_settings())
        finally:
            self._settings_open = False

    def _apply(self, new: Settings) -> None:
        hotkeys_changed = (new.hotkey_important, new.hotkey_pause, new.hotkey_finish) != (
            self.s.hotkey_important, self.s.hotkey_pause, self.s.hotkey_finish)
        from glimpsy import autostart

        if new.launch_at_login != autostart.is_enabled():
            err = autostart.set_enabled(new.launch_at_login)
            if err:
                self.show_message("Автозапуск", f"Не удалось изменить автозапуск: {err}")
        # обновляем объект настроек «на месте», чтобы все ссылки видели новые значения
        self.s.__dict__.update(new.__dict__)
        save_settings(self.s)
        self.engine.apply_settings(self.s)
        self.updates.set_enabled(self.s.check_updates)
        if hotkeys_changed:
            self.bind_hotkeys()

    def exporting(self) -> bool:
        return any(getattr(w, "exporting", False) for w in self._editors)

    def quit(self, force: bool = False) -> None:
        """force — без вопросов (перезапуск после обновления): уже записанное сохранится."""
        if self.engine.candidate_count and not force:
            ans = QMessageBox.question(
                None, "Glimpsy",
                "Выйти без сборки ролика?\nСохранённые фрагменты останутся — при следующем запуске "
                "можно будет собрать ролик или продолжить запись.")
            if ans != QMessageBox.StandardButton.Yes:
                return
        for w in list(self._editors):        # сохранить открытые проекты
            w.close()
        self.services.hotkeys.stop()
        if self._prompter is not None:
            self._prompter.hide()
        self.engine.shutdown()
        self.services.capture.close()
        if self.tray:
            self.tray.hide()
        self.app.quit()

    # ---------- запасное окно без трея ----------

    def _fallback_window(self) -> QWidget:
        w = QWidget()
        w.setWindowTitle("Glimpsy")
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Системный трей недоступен, поэтому Glimpsy показывает это окно.\n"
                           "Его можно свернуть — запись продолжится."))
        self._win_status = QLabel()
        self._win_status.setWordWrap(True)
        v.addWidget(self._win_status)
        for text, fn in (("⏸ Пауза / продолжить", self.engine.toggle_pause),
                         ("⭐ Важный момент", self.engine.mark_important),
                         ("🎬 Собрать ролик", self._finish),
                         ("▶ Начать запись", self.engine.start_session),
                         ("🎞 Редактор роликов…", self.open_editor),
                         ("🧹 Очистить кэш…", self.clear_cache),
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
