"""Главное окно редактора.

Раскладка как в CapCut:
  ┌──────────── панель инструментов ────────────┐
  │            просмотр            │  свойства  │
  │  ▶  00:12.3 / 01:00.0          │            │
  ├──────────────── лента ──────────────────────┤
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QSizePolicy, QSlider, QSplitter, QStyle,
    QTextEdit, QToolBar, QVBoxLayout, QWidget,
)

from worklapse import paths
from worklapse.editor import keys
from worklapse.editor.export import ExportCancelled, default_output, export_project
from worklapse.editor.inspector import Inspector
from worklapse.editor.media import IMAGE_EXT, VIDEO_EXT, MediaError, Thumbnailer, is_supported, probe
from worklapse.editor.player import TimelinePlayer
from worklapse.editor.preview import PreviewWidget
from worklapse.editor.project import History, Project
from worklapse.editor.timeline import TimelineWidget, fmt_time
from worklapse.recorder.encoder import Encoder

log = logging.getLogger(__name__)

ASPECT_CHOICES = [("16:9", "16:9 — YouTube"), ("9:16", "9:16 — Reels, TikTok, Shorts")]


class _ExportBridge(QObject):
    progress = Signal(float, str)
    done = Signal(str)
    failed = Signal(str)


class EditorWindow(QMainWindow):
    def __init__(self, project_dir: Path, ffmpeg: str, encoder_getter: Callable[[], Encoder],
                 fallback_output: Path) -> None:
        super().__init__()
        self.ffmpeg = ffmpeg
        self.encoder_getter = encoder_getter
        self.fallback_output = fallback_output
        self.project = Project.load(project_dir)
        self.history = History()
        self.thumbs = Thumbnailer(ffmpeg)
        self.player = TimelinePlayer(self.project)
        self._export_cancel: threading.Event | None = None

        self.setWindowTitle(f"Worklapse — {self.project.name}")
        self.resize(1280, 820)

        # --- виджеты ---
        self.preview = PreviewWidget()
        self.preview.set_aspect(self.project.aspect)
        self.timeline = TimelineWidget(self.project, self.thumbs)
        self.inspector = Inspector()

        self.play_btn = QPushButton()
        self.play_btn.setFixedWidth(44)
        self._icon_play = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self._icon_pause = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        self.play_btn.setIcon(self._icon_play)
        self.play_btn.setToolTip("Пуск / пауза (Пробел)")
        self.play_btn.clicked.connect(self.player.toggle)
        self.time_lbl = QLabel()
        self.time_lbl.setMinimumWidth(150)
        vol = QSlider(Qt.Orientation.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(80)
        vol.setFixedWidth(90)
        vol.valueChanged.connect(lambda v: self.player.set_volume(v / 100))
        self.player.set_volume(0.8)
        zoom_out, zoom_fit, zoom_in = QPushButton("−"), QPushButton("Вся лента"), QPushButton("+")
        for b in (zoom_out, zoom_in):
            b.setFixedWidth(32)
        zoom_out.clicked.connect(lambda: self.timeline.zoom(1 / 1.4))
        zoom_in.clicked.connect(lambda: self.timeline.zoom(1.4))
        zoom_fit.clicked.connect(self.timeline.fit)

        transport = QHBoxLayout()
        transport.addWidget(self.play_btn)
        transport.addWidget(self.time_lbl)
        transport.addStretch(1)
        transport.addWidget(QLabel("🔊"))
        transport.addWidget(vol)
        transport.addSpacing(16)
        transport.addWidget(QLabel("Масштаб:"))
        transport.addWidget(zoom_out)
        transport.addWidget(zoom_fit)
        transport.addWidget(zoom_in)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.preview, 1)
        lv.addLayout(transport)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(left)
        top.addWidget(self.inspector)
        top.setStretchFactor(0, 1)
        top.setSizes([960, 320])
        root = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(top)
        root.addWidget(self.timeline)
        root.setStretchFactor(0, 1)
        root.setStretchFactor(1, 0)
        root.setSizes([640, 150])
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(8, 4, 8, 8)
        cv.addWidget(root)
        self.setCentralWidget(central)

        self._build_toolbar()

        # --- связи ---
        self.player.frame.connect(self.preview.set_image)
        self.player.position.connect(self._on_position)
        self.player.playing_changed.connect(
            lambda on: self.play_btn.setIcon(self._icon_pause if on else self._icon_play))
        self.timeline.seek_requested.connect(self.player.seek)
        self.timeline.selection_changed.connect(self._on_select)
        self.timeline.about_to_change.connect(lambda key: self.history.push(self.project.to_dict(), key))
        self.timeline.changed.connect(self._changed)
        self.timeline.files_dropped.connect(self.insert_files)
        self.inspector.edited.connect(self._on_inspector)

        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self._save)
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self._initial)

    def _initial(self) -> None:
        self.timeline.fit()
        self.player.seek(0.0)
        self._update_actions()

    # ---------- панель инструментов ----------

    def _build_toolbar(self) -> None:
        tb = QToolBar("Инструменты")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(tb)
        self.a_undo = QAction("↶ Отменить", self, triggered=self.undo, toolTip="Ctrl+Z")
        self.a_redo = QAction("↷ Повторить", self, triggered=self.redo, toolTip="Ctrl+Shift+Z / Ctrl+Y")
        a_add = QAction("＋ Добавить медиа", self, triggered=self.add_media_dialog, toolTip="Видео или фото (Ctrl+V)")
        self.a_split = QAction("✂ Разрезать", self, triggered=self.split, toolTip="Ctrl+B — по курсору")
        self.a_delete = QAction("🗑 Удалить", self, triggered=self.delete_selected, toolTip="Delete")
        for a in (self.a_undo, self.a_redo):
            tb.addAction(a)
        tb.addSeparator()
        for a in (a_add, self.a_split, self.a_delete):
            tb.addAction(a)
        tb.addSeparator()
        tb.addWidget(QLabel(" Формат: "))
        self.aspect_box = QComboBox()
        for value, label in ASPECT_CHOICES:
            self.aspect_box.addItem(label, value)
        self.aspect_box.setCurrentIndex(max(0, self.aspect_box.findData(self.project.aspect)))
        self.aspect_box.currentIndexChanged.connect(self._on_aspect)
        tb.addWidget(self.aspect_box)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.export_btn = QPushButton("  Экспорт  ")
        self.export_btn.setStyleSheet("QPushButton { background: #1f6f78; color: white; font-weight: 600;"
                                      " padding: 6px 14px; border-radius: 6px; }")
        self.export_btn.clicked.connect(self.export)
        tb.addWidget(self.export_btn)

    def _update_actions(self) -> None:
        self.a_undo.setEnabled(self.history.can_undo)
        self.a_redo.setEnabled(self.history.can_redo)
        self.a_delete.setEnabled(self.timeline.selected is not None)
        self.a_split.setEnabled(bool(self.project.clips))
        self.export_btn.setEnabled(bool(self.project.clips))

    # ---------- события ----------

    def _on_position(self, t: float) -> None:
        self.timeline.set_playhead(t, follow=self.player.playing)
        self.time_lbl.setText(f"{fmt_time(t, True)} / {fmt_time(self.project.total, True)}")

    def _on_select(self, clip_id) -> None:
        idx = self.project.index_of(clip_id) if clip_id else -1
        self.inspector.set_clip(self.project.clips[idx] if idx >= 0 else None)
        self._update_actions()

    def _changed(self) -> None:
        """После любой правки: перерисовать, обновить просмотр, автосохранение."""
        if self.timeline.selected and self.project.index_of(self.timeline.selected) < 0:
            self.timeline.select(None)
        self.timeline.update()
        self.player.refresh()
        sel = self.project.index_of(self.timeline.selected) if self.timeline.selected else -1
        self.inspector.set_clip(self.project.clips[sel] if sel >= 0 else None)
        self._update_actions()
        self._save_timer.start()

    def _save(self) -> None:
        try:
            self.project.save()
        except OSError:
            log.exception("Не удалось сохранить проект")

    def _on_inspector(self, clip_id: str, what: str, value) -> None:
        idx = self.project.index_of(clip_id)
        if idx < 0:
            return
        if what == "delete":
            self.timeline.select(clip_id)
            self.delete_selected()
            return
        self.history.push(self.project.to_dict(), key=f"{what}:{clip_id}")
        c = self.project.clips[idx]
        if what == "speed":
            self.project.set_speed(idx, value)
        elif what == "muted":
            c.muted = bool(value)
        elif what == "in_s":
            self.project.trim(idx, value, c.out_s)
        elif what == "out_s":
            self.project.trim(idx, c.in_s, value)
        elif what == "photo_duration":
            c.out_s = c.in_s + float(value)
        self._changed()

    def _on_aspect(self) -> None:
        value = self.aspect_box.currentData()
        if value == self.project.aspect:
            return
        self.history.push(self.project.to_dict(), key="aspect")
        self.project.aspect = value
        self.preview.set_aspect(value)
        self._changed()

    # ---------- команды ----------

    def undo(self) -> None:
        state = self.history.undo(self.project.to_dict())
        if state is not None:
            self._restore(state)

    def redo(self) -> None:
        state = self.history.redo(self.project.to_dict())
        if state is not None:
            self._restore(state)

    def _restore(self, state: dict) -> None:
        self.project.restore(state)
        self.preview.set_aspect(self.project.aspect)
        self.aspect_box.blockSignals(True)
        self.aspect_box.setCurrentIndex(max(0, self.aspect_box.findData(self.project.aspect)))
        self.aspect_box.blockSignals(False)
        self._changed()

    def split(self) -> None:
        snapshot = self.project.to_dict()
        right = self.project.split(self.player.t)
        if right is None:
            self.statusBar().showMessage("Здесь не разрезать: слишком близко к краю фрагмента", 3000)
            return
        self.history.push(snapshot)
        self.timeline.select(self.project.clips[right].id)
        self._changed()

    def delete_selected(self) -> None:
        idx = self.project.index_of(self.timeline.selected) if self.timeline.selected else -1
        if idx < 0:
            return
        self.history.push(self.project.to_dict())
        self.project.delete(idx)
        nxt = self.project.clips[min(idx, len(self.project.clips) - 1)].id if self.project.clips else None
        self.timeline.select(nxt)
        self._changed()

    def _insert_index(self) -> int:
        if self.timeline.selected:
            idx = self.project.index_of(self.timeline.selected)
            if idx >= 0:
                return idx + 1
        idx, _ = self.project.locate(self.player.t)
        return len(self.project.clips) if idx is None else idx + 1

    def add_media_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT | VIDEO_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Добавить видео или фото", str(Path.home()),
                                                f"Видео и фото ({exts});;Все файлы (*)")
        if files:
            self.insert_files(files, self._insert_index())

    def insert_files(self, files: list[str], index: int) -> None:
        clips, errors = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for f in files:
                path = Path(f)
                if not path.is_file() or not is_supported(path):
                    errors.append(f"{path.name}: этот тип файла не поддерживается")
                    continue
                try:
                    info = probe(self.ffmpeg, path)
                    clips.append(self.project.import_file(path, info))
                except (MediaError, OSError) as e:
                    errors.append(f"{path.name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        if clips:
            self.history.push(self.project.to_dict())
            self.project.insert(index, clips)
            self.timeline.select(clips[0].id)
            self._changed()
            self.player.seek(self.project.start_of(index))
        if errors:
            QMessageBox.warning(self, "Worklapse", "Не всё удалось добавить:\n\n" + "\n".join(errors))

    def paste(self) -> None:
        """Ctrl+V: файлы, скопированные в Проводнике/Finder, или картинка из буфера обмена."""
        data = QApplication.clipboard().mimeData()
        if data.hasUrls():
            files = [u.toLocalFile() for u in data.urls() if u.isLocalFile()]
            if files:
                self.insert_files(files, self._insert_index())
                return
        if data.hasImage():
            img = QApplication.clipboard().image()
            if not img.isNull():
                media = self.project.dir / "media"
                media.mkdir(parents=True, exist_ok=True)
                path = media / time.strftime("paste_%Y%m%d_%H%M%S.png")
                img.save(str(path))
                self.insert_files([str(path)], self._insert_index())
                return
        text = data.text().strip().strip('"')
        if text and Path(text).is_file():
            self.insert_files([text], self._insert_index())
            return
        self.statusBar().showMessage("В буфере обмена нет видео или картинки", 3000)

    def step(self, seconds: float) -> None:
        self.player.pause()
        self.player.seek(self.player.t + seconds)

    # ---------- клавиши (в любой раскладке) ----------

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() != QEvent.Type.KeyPress or not self.isActiveWindow():
            return False
        assert isinstance(ev, QKeyEvent)
        focus = QApplication.focusWidget()
        typing = isinstance(focus, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit))
        letter = keys.latin_letter(ev)
        if keys.has_ctrl(ev):
            if letter == "z":
                self.redo() if keys.has_shift(ev) else self.undo()
                return True
            if letter == "y":
                self.redo()
                return True
            if letter == "b":
                self.split()
                return True
            if letter == "v" and not typing:
                self.paste()
                return True
            return False
        if typing:
            return False
        k = ev.key()
        fps = self.project.fps or 30
        if k == Qt.Key.Key_Space:
            self.player.toggle()
        elif k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        elif k == Qt.Key.Key_Left:
            self.step(-1.0 if keys.has_shift(ev) else -1.0 / fps)
        elif k == Qt.Key.Key_Right:
            self.step(1.0 if keys.has_shift(ev) else 1.0 / fps)
        elif k == Qt.Key.Key_Home:
            self.player.seek(0.0)
        elif k == Qt.Key.Key_End:
            self.player.seek(self.project.total)
        else:
            return False
        return True

    # ---------- экспорт ----------

    def export(self) -> None:
        self.player.pause()
        self._save()
        out = default_output(self.project, self.fallback_output)
        dlg = QProgressDialog("Подготовка…", "Отмена", 0, 1000, self)
        dlg.setWindowTitle("Экспорт ролика")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        bridge = _ExportBridge(self)
        cancel = threading.Event()
        dlg.canceled.connect(cancel.set)
        bridge.progress.connect(lambda f, t: (dlg.setValue(int(f * 1000)), dlg.setLabelText(t)))

        def finished(path: str) -> None:
            dlg.close()
            box = QMessageBox(self)
            box.setWindowTitle("Готово")
            box.setText(f"Ролик сохранён:\n{path}")
            b_open = box.addButton("Открыть папку", QMessageBox.ButtonRole.ActionRole)
            box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() == b_open:
                paths.open_in_file_manager(Path(path).parent)

        def failed(msg: str) -> None:
            dlg.close()
            if msg:
                QMessageBox.warning(self, "Экспорт не удался", msg)

        bridge.done.connect(finished)
        bridge.failed.connect(failed)
        snapshot = Project(self.project.dir, self.project.name, fps=self.project.fps,
                           source_video=self.project.source_video)
        snapshot.restore(self.project.to_dict())    # копия — можно продолжать править во время экспорта

        def work() -> None:
            try:
                enc = self.encoder_getter()
                path = export_project(self.ffmpeg, snapshot, out, enc,
                                      progress=lambda f, t: bridge.progress.emit(f, t), cancel=cancel)
                bridge.done.emit(str(path))
            except ExportCancelled:
                bridge.failed.emit("")
            except Exception as e:
                log.exception("Экспорт не удался")
                bridge.failed.emit(str(e))

        threading.Thread(target=work, daemon=True, name="export").start()
        dlg.show()

    def closeEvent(self, e) -> None:
        self._save()
        QApplication.instance().removeEventFilter(self)
        self.player.shutdown()
        super().closeEvent(e)
