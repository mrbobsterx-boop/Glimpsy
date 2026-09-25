"""Главное окно редактора.

Раскладка как в CapCut:
  ┌──────────── панель инструментов ────────────┐
  │            просмотр            │  свойства  │
  │  ▶  00:12.3 / 01:00.0          │            │
  ├──────────────── лента ──────────────────────┤
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QSizePolicy, QSlider, QSplitter, QStackedWidget, QStyle,
    QTextEdit, QToolBar, QVBoxLayout, QWidget,
)

from worklapse import paths
from worklapse.editor import keys
from worklapse.editor.export import (
    ExportCancelled, default_output, export_project, render_overlay_layers, render_text_layers,
)
from worklapse.editor.inspector import Inspector
from worklapse.editor.media import IMAGE_EXT, VIDEO_EXT, MediaError, Thumbnailer, is_supported, probe
from worklapse.editor.player import TimelinePlayer
from worklapse.editor.preview import PreviewWidget
from worklapse.editor.project import ASPECTS, DEFAULT_FRAME, Clip, History, Project, cover_zoom, new_id
from worklapse.editor.text import TextItem, effective_style, load_custom_fonts
from worklapse.editor.text_panel import TextPanel
from worklapse.editor.overlay import OverlayItem
from worklapse.editor.overlay_panel import OverlayPanel
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
        load_custom_fonts()                 # свои шрифты, добавленные раньше
        self.player = TimelinePlayer(self.project)
        self._export_cancel: threading.Event | None = None

        self.setWindowTitle(f"Worklapse — {self.project.name}")
        self.resize(1280, 820)

        # --- виджеты ---
        self.preview = PreviewWidget()
        self.preview.set_aspect(self.project.aspect)
        self.timeline = TimelineWidget(self.project, self.thumbs)
        self.inspector = Inspector()
        self.text_panel = TextPanel()
        self.overlay_panel = OverlayPanel()
        self.side = QStackedWidget()        # справа: свойства фрагмента, текста или наложения
        self.side.addWidget(self.inspector)
        self.side.addWidget(self.text_panel)
        self.side.addWidget(self.overlay_panel)
        self._ov_images: dict[str, QImage] = {}

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
        top.addWidget(self.side)
        top.setStretchFactor(0, 1)
        top.setSizes([930, 350])
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
        self._frame_start: dict = {}
        self.preview.edit_started.connect(self._on_frame_edit_start)
        self.preview.frame_delta.connect(self._on_frame_delta)
        self.preview.edit_finished.connect(self._changed)
        self.preview.wheel_zoom.connect(self._on_wheel_zoom)
        self.timeline.text_selected.connect(self._on_text_select)
        self.text_panel.edited.connect(self._on_text_edit)
        self.preview.text_pressed.connect(self._on_text_pressed)
        self.preview.text_moved.connect(self._on_text_moved)
        self.timeline.overlay_selected.connect(self._on_overlay_select)
        self.timeline.overlay_files_dropped.connect(self.add_overlays)
        self.overlay_panel.edited.connect(self._on_overlay_edit)
        self.preview.overlay_pressed.connect(self._on_overlay_pressed)
        self.preview.overlay_changed.connect(self._on_overlay_changed)
        self.preview.overlay_wheel.connect(self._on_overlay_wheel)
        self.thumbs.ready.connect(self._sync_preview)     # кадры видео-наложений подгружаются в фоне

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
        a_text = QAction("T  Текст", self, triggered=self.add_text, toolTip="Добавить текст в месте курсора (Ctrl+T)")
        a_overlay = QAction("▣  Наложение", self, triggered=self.add_overlay_dialog,
                            toolTip="Картинка или видео поверх ролика (логотип, макет, съёмка с телефона)")
        self.a_split = QAction("✂ Разрезать", self, triggered=self.split, toolTip="Ctrl+B — по курсору")
        self.a_delete = QAction("🗑 Удалить", self, triggered=self.delete_selected, toolTip="Delete")
        for a in (self.a_undo, self.a_redo):
            tb.addAction(a)
        tb.addSeparator()
        for a in (a_add, a_text, a_overlay, self.a_split, self.a_delete):
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
        self._sync_preview()

    def _sync_preview(self) -> None:
        """Окну просмотра — кадрирование показанного фрагмента и можно ли его править."""
        c = self._shown_clip()
        if c is None:
            self.preview.set_frame(DEFAULT_FRAME, False)
        else:
            self.preview.set_frame(c.frame_for(self.project.aspect), c.id in self.timeline.selection)
        t = self.player.t
        visible = [x for x in self.project.texts if x.start <= t < x.end or x.id == self.timeline.selected_text]
        self.preview.set_texts([(x, effective_style(x, self.project.text_style)) for x in visible], t,
                               self.timeline.selected_text)
        sel_ov = self.timeline.selected_overlay
        shown = [o for o in self.project.overlays if o.start <= t < o.end or o.id == sel_ov]
        self.preview.set_overlays([(o, self._overlay_frame(o, t)) for o in shown], sel_ov)

    def _overlay_frame(self, o, t: float):
        """Картинка для показа наложения в просмотре (для видео — кадр в нужный момент)."""
        path = self.project.dir / o.src
        if o.kind == "image":
            img = self._ov_images.get(str(path))
            if img is None:
                img = self._ov_images[str(path)] = QImage(str(path))
            return img
        local = max(0.0, min(o.duration, t - o.start))
        return self.thumbs.get(path, float(round(o.in_s + local)), 360)

    def _shown_clip(self) -> Clip | None:
        idx = self.player.idx
        return self.project.clips[idx] if idx is not None and 0 <= idx < len(self.project.clips) else None

    def _selected_clips(self) -> list[Clip]:
        ids = set(self.timeline.selection)
        return [c for c in self.project.clips if c.id in ids]

    def _refresh_inspector(self) -> None:
        sel = self.project.index_of(self.timeline.selected) if self.timeline.selected else -1
        self.inspector.set_clip(self.project.clips[sel] if sel >= 0 else None, self.project.aspect,
                                max(1, len(self.timeline.selection)))

    def _on_select(self, _clip_id) -> None:
        self._refresh_inspector()
        self._sync_preview()
        self._update_actions()

    def _changed(self) -> None:
        """После любой правки: перерисовать, обновить просмотр, автосохранение."""
        self.timeline.prune_selection()
        self.timeline.update()
        self.player.refresh()
        self._refresh_inspector()
        item = self.project.text_by_id(self.timeline.selected_text) if self.timeline.selected_text else None
        if item is not None:
            self.text_panel.set_item(item, effective_style(item, self.project.text_style))
        ov = self.project.overlay_by_id(self.timeline.selected_overlay) if self.timeline.selected_overlay else None
        if ov is not None:
            self.overlay_panel.set_item(ov, self.project.aspect)
        self._sync_preview()
        self._update_actions()
        self._save_timer.start()

    def _save(self) -> None:
        try:
            self.project.save()
        except OSError:
            log.exception("Не удалось сохранить проект")

    def _on_inspector(self, clip_id: str, what: str, value) -> None:
        if what == "delete":
            self.delete_selected()
            return
        targets = self._selected_clips() or [c for c in self.project.clips if c.id == clip_id]
        if not targets:
            return
        aspect = self.project.aspect
        W, H = ASPECTS[aspect]
        if what == "frame_all":
            src = next((c for c in self.project.clips if c.id == clip_id), None)
            if src is None:
                return
            self.history.push(self.project.to_dict())
            for c in self.project.clips:
                c.set_frame(aspect, *src.frame_for(aspect))
            self.statusBar().showMessage("Кадрирование применено ко всем фрагментам", 3000)
            self._changed()
            return
        self.history.push(self.project.to_dict(), key=f"{what}:{','.join(c.id for c in targets)}")
        for c in targets:
            idx = self.project.index_of(c.id)
            z, x, y = c.frame_for(aspect)
            if what == "speed" and c.kind == "video":
                self.project.set_speed(idx, value)
            elif what == "muted" and c.kind == "video":
                c.muted = bool(value)
            elif what == "in_s" and c.id == clip_id:
                self.project.trim(idx, value, c.out_s)
            elif what == "out_s" and c.id == clip_id:
                self.project.trim(idx, c.in_s, value)
            elif what == "photo_duration" and c.kind == "image":
                c.out_s = c.in_s + float(value)
            elif what == "frame_zoom":
                c.set_frame(aspect, value, x, y)
            elif what == "frame_x":
                c.set_frame(aspect, z, value, y)
            elif what == "frame_y":
                c.set_frame(aspect, z, x, value)
            elif what == "frame_fit":
                c.set_frame(aspect, *DEFAULT_FRAME)
            elif what == "frame_fill":
                c.set_frame(aspect, cover_zoom(c.width or W, c.height or H, W, H), 0.0, 0.0)
        self._changed()

    # ---------- кадрирование мышью в окне просмотра ----------

    def _on_frame_edit_start(self) -> None:
        shown = self._shown_clip()
        if shown is None:
            return
        self.player.pause()
        if shown.id not in self.timeline.selection:
            self.timeline.select(shown.id)       # щелчок по кадру выбирает этот фрагмент
        self.history.push(self.project.to_dict())
        self._frame_start = {c.id: c.frame_for(self.project.aspect) for c in self._selected_clips()}
        self._sync_preview()

    def _on_frame_delta(self, zm: float, dx: float, dy: float) -> None:
        aspect = self.project.aspect
        for c in self._selected_clips():
            z0, x0, y0 = self._frame_start.get(c.id, c.frame_for(aspect))
            c.set_frame(aspect, z0 * zm, x0 + dx, y0 + dy)
        self._refresh_inspector()
        self._save_timer.start()

    def _on_wheel_zoom(self, factor: float) -> None:
        shown = self._shown_clip()
        if shown is None:
            return
        if shown.id not in self.timeline.selection:
            self.timeline.select(shown.id)
        targets = self._selected_clips()
        self.history.push(self.project.to_dict(), key="wheel:" + ",".join(c.id for c in targets))
        aspect = self.project.aspect
        for c in targets:
            z, x, y = c.frame_for(aspect)
            c.set_frame(aspect, z * factor, x, y)
        self._refresh_inspector()
        self._sync_preview()
        self._save_timer.start()

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

    # ---------- тексты ----------

    def add_text(self) -> None:
        self.player.pause()
        total = self.project.total
        start = min(self.player.t, max(0.0, total - 0.5))
        item = TextItem(new_id(), "Текст", round(start, 2), round(min(2.5, max(0.5, total - start)), 2))
        self.history.push(self.project.to_dict())
        self.project.texts.append(item)
        self.timeline.select_text(item.id)
        self._text_changed()
        self.text_panel.focus_text()

    def _on_text_select(self, text_id) -> None:
        item = self.project.text_by_id(text_id) if text_id else None
        if item is None:
            self.side.setCurrentWidget(self.inspector)
        else:
            self.player.pause()
            if not (item.start <= self.player.t < item.end):
                self.player.seek(item.start + min(0.5, item.duration / 2))
            self.text_panel.set_item(item, effective_style(item, self.project.text_style))
            self.side.setCurrentWidget(self.text_panel)
        self._sync_preview()
        self._update_actions()

    def _text_changed(self) -> None:
        """Правка текста — без перемотки видео, только перерисовка и сохранение."""
        self.timeline.prune_selection()
        self.timeline.update()
        item = self.project.text_by_id(self.timeline.selected_text) if self.timeline.selected_text else None
        if item is not None:
            self.text_panel.set_item(item, effective_style(item, self.project.text_style))
        self._sync_preview()
        self._update_actions()
        self._save_timer.start()

    def _on_text_edit(self, text_id: str, what: str, value) -> None:
        item = self.project.text_by_id(text_id)
        if item is None:
            return
        if what == "delete":
            self.history.push(self.project.to_dict())
            self.project.texts.remove(item)
            self.timeline.select_text(None)
            self._text_changed()
            return
        self.history.push(self.project.to_dict(), key=f"text-{what}:{text_id}")
        if what == "text":
            item.text = value
        elif what == "start":
            item.start = max(0.0, float(value))
        elif what == "duration":
            item.duration = max(0.2, float(value))
        elif what == "own_style":
            item.style = dict(effective_style(item, self.project.text_style)) if value else None
        elif what == "pos_preset":
            item.set_pos(self.project.aspect, 0.5, float(value))
        else:
            # свой стиль — меняем только этот текст, иначе общий стиль всех текстов
            target = item.style if item.style is not None else self.project.text_style
            target[what] = value
        self._text_changed()

    def _on_text_pressed(self, text_id: str) -> None:
        self.timeline.select_text(text_id)
        self.history.push(self.project.to_dict())

    def _on_text_moved(self, text_id: str, x: float, y: float) -> None:
        item = self.project.text_by_id(text_id)
        if item is not None:
            item.set_pos(self.project.aspect, x, y)
            self._sync_preview()
            self._save_timer.start()

    # ---------- наложения ----------

    def add_overlay_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT | VIDEO_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Картинка или видео поверх ролика", str(Path.home()),
                                                f"Картинки и видео ({exts});;Все файлы (*)")
        if files:
            self.add_overlays(files, self.player.t)

    def add_overlays(self, files: list, start: float) -> None:
        self.player.pause()
        total = self.project.total
        start = max(0.0, min(float(start), max(0.0, total - 0.5)))
        items, errors = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for f in files:
                path = Path(f)
                if not path.is_file() or not is_supported(path):
                    errors.append(f"{path.name}: этот тип файла не поддерживается")
                    continue
                try:
                    info = probe(self.ffmpeg, path)
                    clip = self.project.import_file(path, info)      # копия файла в папку проекта
                except (MediaError, OSError) as e:
                    errors.append(f"{path.name}: {e}")
                    continue
                room = max(0.5, total - start)
                dur = 3.0 if info.is_image else info.duration
                o = OverlayItem(new_id(), "image" if info.is_image else "video", clip.src, round(start, 2),
                                round(min(dur, room), 2), width=info.width, height=info.height,
                                src_duration=0.0 if info.is_image else info.duration,
                                has_audio=info.has_audio, label=path.name)
                o.set_layout(self.project.aspect, 0.5, 0.5, 0.45)
                items.append(o)
        finally:
            QApplication.restoreOverrideCursor()
        if items:
            self.history.push(self.project.to_dict())
            self.project.overlays.extend(items)
            self.timeline.select_overlay(items[-1].id)
            self._layer_changed()
        if errors:
            QMessageBox.warning(self, "Worklapse", "Не всё удалось добавить:\n\n" + "\n".join(errors))

    def _on_overlay_select(self, oid) -> None:
        o = self.project.overlay_by_id(oid) if oid else None
        if o is None:
            if self.side.currentWidget() is self.overlay_panel:
                self.side.setCurrentWidget(self.inspector)
        else:
            self.player.pause()
            if not (o.start <= self.player.t < o.end):
                self.player.seek(o.start + min(0.5, o.duration / 2))
            self.overlay_panel.set_item(o, self.project.aspect)
            self.side.setCurrentWidget(self.overlay_panel)
        self._sync_preview()
        self._update_actions()

    def _layer_changed(self) -> None:
        """Правка текста или наложения — без перемотки видео."""
        self._text_changed()
        o = self.project.overlay_by_id(self.timeline.selected_overlay) if self.timeline.selected_overlay else None
        if o is not None:
            self.overlay_panel.set_item(o, self.project.aspect)

    def _on_overlay_edit(self, oid: str, what: str, value) -> None:
        o = self.project.overlay_by_id(oid)
        if o is None:
            return
        if what == "delete":
            self.history.push(self.project.to_dict())
            self.project.overlays.remove(o)
            self.timeline.select_overlay(None)
            self._layer_changed()
            return
        self.history.push(self.project.to_dict(), key=f"ov-{what}:{oid}")
        aspect = self.project.aspect
        cx, cy, sc = o.layout_for(aspect)
        if what == "start":
            o.start = max(0.0, float(value))
        elif what == "duration":
            o.duration = max(0.2, float(value))
        elif what == "scale":
            o.set_layout(aspect, cx, cy, float(value))
        elif what == "place":
            o.set_layout(aspect, value[0], value[1], sc)
        elif what == "fill":
            W, H = ASPECTS[aspect]
            ar = (o.height / o.width) if o.width and o.height else 9 / 16
            o.set_layout(aspect, 0.5, 0.5, max(1.0, (H / W) / ar))   # закрыть весь кадр
        elif what == "muted":
            o.muted = bool(value)
        elif what in ("opacity", "radius", "shadow"):
            setattr(o, what, value)
        self._layer_changed()

    def _on_overlay_pressed(self, oid: str) -> None:
        self.player.pause()
        self.timeline.select_overlay(oid)
        self.history.push(self.project.to_dict())

    def _on_overlay_changed(self, oid: str, cx: float, cy: float, scale: float) -> None:
        o = self.project.overlay_by_id(oid)
        if o is not None:
            o.set_layout(self.project.aspect, cx, cy, scale)
            self._sync_preview()
            self.overlay_panel.set_item(o, self.project.aspect)
            self._save_timer.start()

    def _on_overlay_wheel(self, oid: str, factor: float) -> None:
        o = self.project.overlay_by_id(oid)
        if o is None:
            return
        self.history.push(self.project.to_dict(), key=f"ov-wheel:{oid}")
        cx, cy, sc = o.layout_for(self.project.aspect)
        o.set_layout(self.project.aspect, cx, cy, sc * factor)
        self._layer_changed()

    def delete_selected(self) -> None:
        if self.timeline.selected_overlay:
            self._on_overlay_edit(self.timeline.selected_overlay, "delete", None)
            return
        if self.timeline.selected_text:
            self._on_text_edit(self.timeline.selected_text, "delete", None)
            return
        ids = set(self.timeline.selection)
        indices = [i for i, c in enumerate(self.project.clips) if c.id in ids]
        if not indices:
            return
        self.history.push(self.project.to_dict())
        for i in reversed(indices):
            self.project.delete(i)
        first = indices[0]
        nxt = self.project.clips[min(first, len(self.project.clips) - 1)].id if self.project.clips else None
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
            if letter == "t":
                self.add_text()
                return True
            if letter == "a" and not typing:
                self.timeline.select_all()
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
        # тексты рисуются в основном потоке (так надёжнее для шрифтов), дальше — FFmpeg в фоне
        layers_dir = paths.temp_root() / f"text_{int(time.time())}"
        text_layers = render_text_layers(snapshot, layers_dir)
        overlay_layers = render_overlay_layers(snapshot, layers_dir)

        def work() -> None:
            try:
                enc = self.encoder_getter()
                path = export_project(self.ffmpeg, snapshot, out, enc, text_layers=text_layers,
                                      overlay_layers=overlay_layers,
                                      progress=lambda f, t: bridge.progress.emit(f, t), cancel=cancel)
                bridge.done.emit(str(path))
            except ExportCancelled:
                bridge.failed.emit("")
            except Exception as e:
                log.exception("Экспорт не удался")
                bridge.failed.emit(str(e))
            finally:
                shutil.rmtree(layers_dir, ignore_errors=True)

        threading.Thread(target=work, daemon=True, name="export").start()
        dlg.show()

    def closeEvent(self, e) -> None:
        self._save()
        QApplication.instance().removeEventFilter(self)
        self.player.shutdown()
        super().closeEvent(e)
