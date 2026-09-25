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
    QAbstractSpinBox, QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QScrollArea, QToolButton, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QSizePolicy, QSlider, QSplitter, QStackedWidget, QStyle,
    QTextEdit, QToolBar, QVBoxLayout, QWidget,
)

from glimpsy import paths
from glimpsy.ui import theme
from glimpsy.ui.icons import app_logo
from glimpsy.editor import keys, motion
from glimpsy.editor.export import (
    ExportCancelled, default_output, export_project, render_overlay_layers, render_text_layers,
)
from glimpsy.editor.inspector import Inspector
from glimpsy.editor.media import IMAGE_EXT, VIDEO_EXT, MediaError, Thumbnailer, is_supported, probe
from glimpsy.editor.player import TimelinePlayer
from glimpsy.editor.preview import PreviewWidget
from glimpsy.editor.project import ASPECTS, DEFAULT_FRAME, Clip, History, Project, cover_zoom, new_id
from glimpsy.editor.text import TextItem, effective_style, load_custom_fonts
from glimpsy.editor.text_panel import TextPanel
from glimpsy.editor.overlay import OverlayItem
from glimpsy.editor.overlay_panel import OverlayPanel
from glimpsy.editor.music import AUDIO_EXT, MusicTrack, probe_audio
from glimpsy.editor.music_panel import MusicPanel
from glimpsy.editor import subtitles as subs
from glimpsy.editor.timeline import TimelineWidget, fmt_time
from glimpsy.recorder.encoder import Encoder

log = logging.getLogger(__name__)

ASPECT_CHOICES = [("16:9", "16:9 — YouTube"), ("9:16", "9:16 — Reels, TikTok, Shorts")]


class _ExportBridge(QObject):
    progress = Signal(float, str)
    done = Signal(str)
    failed = Signal(str)


EDITOR_QSS = f"""
QFrame#topbar {{ background: {theme.SURFACE}; border-bottom: 1px solid {theme.BORDER}; }}
QFrame#rail {{ background: {theme.SURFACE}; border-right: 1px solid {theme.BORDER}; }}
QFrame#previewPanel, QFrame#sidePanel, QFrame#timelinePanel {{ background: {theme.SURFACE};
    border: 1px solid {theme.BORDER}; border-radius: 12px; }}
QFrame#segmented {{ background: {theme.RAISED}; border: 1px solid {theme.BORDER}; border-radius: 9px; }}
QPushButton#segButton {{ background: transparent; border: none; border-radius: 7px; padding: 5px 12px;
    color: {theme.MUTED}; font-weight: 500; }}
QPushButton#segButton:hover {{ color: {theme.TEXT}; }}
QPushButton#segButton:checked {{ background: {theme.HOVER}; color: {theme.TEXT}; }}
QFrame[role="vdivider"] {{ background: {theme.BORDER}; border: none; }}
QLabel#timecode {{ color: {theme.MUTED}; font-size: 12px; font-family: "{theme.FONT}"; }}
QScrollArea {{ background: transparent; }}
"""


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

        self.setWindowTitle(f"Glimpsy — {self.project.name}")
        self.resize(1280, 820)

        # --- виджеты ---
        self.preview = PreviewWidget()
        self.preview.set_aspect(self.project.aspect)
        self.timeline = TimelineWidget(self.project, self.thumbs)
        self.inspector = Inspector()
        self.text_panel = TextPanel()
        self.overlay_panel = OverlayPanel()
        self.music_panel = MusicPanel()
        self.side = QStackedWidget()        # справа: свойства фрагмента, текста, наложения или музыки
        self.side.addWidget(self.inspector)
        self.side.addWidget(self.text_panel)
        self.side.addWidget(self.overlay_panel)
        self.side.addWidget(self.music_panel)
        self._ov_images: dict[str, QImage] = {}
        self._motion_cache: dict = {}

        self._build_ui()

        # --- связи ---
        self.player.frame.connect(self.preview.set_image)
        self.player.position.connect(self._on_position)
        self.player.playing_changed.connect(
            lambda on: self.play_btn.setIcon(self._icon_pause if on else self._icon_play))
        self.player.set_volume(0.8)
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
        self.timeline.music_selected.connect(self._on_music_select)
        self.music_panel.edited.connect(self._on_music_edit)
        self.thumbs.ready.connect(self._sync_preview)     # кадры видео-наложений подгружаются в фоне

        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self._save)
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self._initial)

    def _initial(self) -> None:
        self.timeline.fit()
        self.player.seek(0.0)
        self._update_actions()

    # ---------- раскладка окна ----------

    def _build_ui(self) -> None:
        """Сверху — логотип, формат и «Экспорт»; слева — инструменты; в центре — просмотр;
        справа — свойства; внизу — лента с её кнопками."""
        from glimpsy.editor.shortcuts_panel import ShortcutsPanel

        # --- действия (одни и те же для кнопок и горячих клавиш) ---
        self.a_undo = QAction(theme.icon("undo-2"), "Отменить", self, triggered=self.undo, toolTip="Отменить (Ctrl+Z)")
        self.a_redo = QAction(theme.icon("redo-2"), "Повторить", self, triggered=self.redo,
                              toolTip="Повторить (Ctrl+Shift+Z / Ctrl+Y)")
        self.a_split = QAction(theme.icon("scissors"), "Разрезать", self, triggered=self.split,
                               toolTip="Разрезать по курсору (S)")
        self.a_delete = QAction(theme.icon("trash-2"), "Удалить", self, triggered=self.delete_selected,
                                toolTip="Удалить выбранное (Delete)")

        # --- верхняя полоса ---
        logo = QLabel()
        logo.setPixmap(app_logo(26))
        brand = theme.mark(QLabel("Glimpsy"), "title")
        name = theme.mark(QLabel(self.project.name), "muted")
        self.aspect_group = QButtonGroup(self)
        seg = QFrame()
        seg.setObjectName("segmented")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(2)
        for value, label, ic, tip in (("16:9", "16:9", "monitor", "Горизонтальный — YouTube"),
                                      ("9:16", "9:16", "smartphone", "Вертикальный — Reels, TikTok, Shorts")):
            b = QPushButton(theme.icon(ic, size=16), f" {label}")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setProperty("aspect", value)
            b.setObjectName("segButton")
            self.aspect_group.addButton(b)
            sl.addWidget(b)
            b.setChecked(value == self.project.aspect)
        self.aspect_group.buttonClicked.connect(lambda b: self._on_aspect(b.property("aspect")))
        self.montage_btn = theme.mark(QPushButton(theme.icon("wand-sparkles", theme.ACCENT_HOVER, 16), "  Автомонтаж"),
                                      "ghost")
        self.montage_btn.setToolTip("Автозум, клики, темп, наезды, 9:16 и склейки под музыку — одной кнопкой")
        self.montage_btn.clicked.connect(self.auto_montage)
        self.export_btn = theme.mark(QPushButton(theme.icon("download", "#FFFFFF", 16), "  Экспорт"), "primary")
        self.export_btn.setToolTip("Сохранить готовый ролик (Ctrl+E)")
        self.export_btn.clicked.connect(self.export)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(14, 8, 12, 8)
        tl.setSpacing(10)
        tl.addWidget(logo)
        tl.addWidget(brand)
        tl.addWidget(theme.mark(QLabel("·"), "muted"))
        tl.addWidget(name)
        tl.addStretch(1)
        tl.addWidget(self.montage_btn)
        tl.addSpacing(6)
        tl.addWidget(seg)
        tl.addSpacing(6)
        tl.addWidget(self.export_btn)

        # --- левая колонка инструментов ---
        rail = QFrame()
        rail.setObjectName("rail")
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(6, 8, 6, 8)
        rl.setSpacing(4)

        def tool(ic: str, text: str, tip: str, slot=None, checkable: bool = False) -> QToolButton:
            b = QToolButton()
            b.setIcon(theme.icon(ic, theme.MUTED, 22))
            b.setIconSize(theme.icon_size(22))
            b.setText(text)
            b.setToolTip(tip)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setFixedWidth(76)
            b.setCheckable(checkable)
            theme.mark(b, "rail")
            if slot is not None:
                b.clicked.connect(slot)
            rl.addWidget(b)
            return b

        tool("image-plus", "Медиа", "Добавить видео или фото (M, Ctrl+V)", self.add_media_dialog)
        tool("type", "Текст", "Добавить текст в месте курсора (T)", self.add_text)
        tool("captions", "Субтитры", "Автосубтитры: распознать речь (на этом компьютере)", self.auto_subtitles)
        tool("layers", "Наложение", "Картинка или видео поверх ролика", self.add_overlay_dialog)
        tool("music", "Музыка", "Фоновая музыка на весь ролик", self.add_music_dialog)
        rl.addStretch(1)
        tool("chart-column", "Статистика", "Сколько работали и где — только для вас", self.show_stats)
        self.shortcuts = ShortcutsPanel()
        keys_btn = tool("keyboard", "Клавиши", "Все горячие клавиши", checkable=True)
        keys_btn.setChecked(self.shortcuts.is_open())
        keys_btn.toggled.connect(self.shortcuts.set_open)

        # --- просмотр и управление под ним ---
        self.play_btn = theme.mark(QToolButton(), "play")
        self._icon_play = theme.icon("play", theme.BG, 18, 2.2)
        self._icon_pause = theme.icon("pause", theme.BG, 18, 2.2)
        self.play_btn.setIcon(self._icon_play)
        self.play_btn.setIconSize(theme.icon_size(18))
        self.play_btn.setToolTip("Пуск / пауза (Пробел)")
        self.play_btn.clicked.connect(self.player.toggle)
        self.time_lbl = QLabel()
        self.time_lbl.setObjectName("timecode")
        vol_icon = QLabel()
        vol_icon.setPixmap(theme.pixmap("volume-2", theme.MUTED, 18))
        vol = QSlider(Qt.Orientation.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(80)
        vol.setFixedWidth(90)
        vol.valueChanged.connect(lambda v: self.player.set_volume(v / 100))
        transport = QHBoxLayout()
        transport.setContentsMargins(12, 6, 12, 8)
        transport.addWidget(self.time_lbl)
        transport.addStretch(1)
        transport.addWidget(self.play_btn)
        transport.addStretch(1)
        transport.addWidget(vol_icon)
        transport.addWidget(vol)
        center = theme.mark(QFrame(), "panel")
        center.setObjectName("previewPanel")
        cl = QVBoxLayout(center)
        cl.setContentsMargins(6, 6, 6, 0)
        cl.addWidget(self.preview, 1)
        cl.addLayout(transport)

        right = theme.mark(QFrame(), "panel")
        right.setObjectName("sidePanel")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 4, 4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.side)
        rv.addWidget(scroll)
        right.setMinimumWidth(330)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.setHandleWidth(6)
        top.addWidget(center)
        top.addWidget(right)
        top.setStretchFactor(0, 1)
        top.setSizes([900, 360])

        # --- лента и её кнопки ---
        def tbtn(action: QAction) -> QToolButton:
            b = QToolButton()
            b.setDefaultAction(action)
            b.setIconSize(theme.icon_size(18))
            return b

        zoom_out, zoom_in = QToolButton(), QToolButton()
        zoom_out.setIcon(theme.icon("zoom-out", size=18))
        zoom_in.setIcon(theme.icon("zoom-in", size=18))
        zoom_out.setToolTip("Уменьшить ленту (Ctrl+колёсико)")
        zoom_in.setToolTip("Увеличить ленту (Ctrl+колёсико)")
        zoom_fit = theme.mark(QPushButton("Вся лента"), "ghost")
        zoom_out.clicked.connect(lambda: self.timeline.zoom(1 / 1.4))
        zoom_in.clicked.connect(lambda: self.timeline.zoom(1.4))
        zoom_fit.clicked.connect(self.timeline.fit)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 2)
        bar.setSpacing(2)
        for a in (self.a_undo, self.a_redo):
            bar.addWidget(tbtn(a))
        sep = theme.mark(QFrame(), "vdivider")
        sep.setFixedSize(1, 18)
        bar.addSpacing(6)
        bar.addWidget(sep)
        bar.addSpacing(6)
        for a in (self.a_split, self.a_delete):
            bar.addWidget(tbtn(a))
        bar.addStretch(1)
        bar.addWidget(zoom_out)
        bar.addWidget(zoom_fit)
        bar.addWidget(zoom_in)
        bottom = theme.mark(QFrame(), "panel")
        bottom.setObjectName("timelinePanel")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addLayout(bar)
        bl.addWidget(self.timeline, 1)

        root = QSplitter(Qt.Orientation.Vertical)
        root.setHandleWidth(6)
        root.addWidget(top)
        root.addWidget(bottom)
        root.setStretchFactor(0, 1)
        root.setStretchFactor(1, 0)
        root.setSizes([600, 190])

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 8, 8)
        body.setSpacing(6)
        body.addWidget(rail)
        body.addWidget(self.shortcuts)
        body.addWidget(root, 1)
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(topbar)
        cv.addLayout(body, 1)
        self.setCentralWidget(central)
        self.setStyleSheet(EDITOR_QSS)

    def _update_actions(self) -> None:
        self.a_undo.setEnabled(self.history.can_undo)
        self.a_redo.setEnabled(self.history.can_redo)
        self.a_delete.setEnabled(self.timeline.selected is not None or self.timeline.music_active)
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
        self.preview.set_src_crop(self._motion_crop(c))
        self.preview.set_ripples(self._ripples(c))
        t = self.player.t
        visible = self.project.texts_at(t)
        sel = self.project.text_by_id(self.timeline.selected_text) if self.timeline.selected_text else None
        if sel is not None and not visible:
            visible = [sel]          # выбранный текст виден для правки, если он не мешает другим
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

    def _ripples(self, c: Clip | None) -> list:
        """Круги кликов, которые видны сейчас (в долях исходного кадра)."""
        if c is None or not c.clicks_shown():
            return []
        idx, local = self.project.locate(self.player.t)
        if idx is None or self.project.clips[idx].id != c.id:
            return []
        from glimpsy.editor.clicks import active
        return active(c.clicks, c.in_s + local * c.speed, c.speed)

    def _motion_crop(self, c: Clip | None) -> tuple[float, float, float, float]:
        """Какую часть кадра показать сейчас (автозум или слежение за курсором)."""
        full = (0.0, 0.0, 1.0, 1.0)
        if c is None:
            return full
        mode = c.motion_for(self.project.aspect)
        if mode == "none":
            return full
        idx, local = self.project.locate(self.player.t)
        if idx is None or self.project.clips[idx].id != c.id:
            return full
        t_src = c.in_s + local * c.speed
        key = (c.id, mode, c.zoom_strength, c.src_duration, len(c.cursor), len(c.clicks), c.width, c.height,
               c.in_s, c.out_s)
        track = self._motion_cache.get(key)
        if track is None:
            track = motion.track_for(mode, c.cursor, c.clicks, c.src_duration, c.zoom_strength,
                                     c.in_s, c.out_s, c.width, c.height)
            self._motion_cache[key] = track
        if motion.is_zoom(mode):
            z, cx, cy = motion.value_at(track, t_src)
            return cx - 0.5 / z, cy - 0.5 / z, 1 / z, 1 / z
        return motion.follow_crop(track, t_src, c.width, c.height)

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
        if self.project.music is None and self.timeline.music_active:
            self.timeline.select_music(False)
        elif self.project.music is not None:
            self.music_panel.set_track(self.project.music)
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
            elif what == "motion" and c.kind == "video":
                c.set_motion(aspect, value)
            elif what == "zoom_strength" and c.kind == "video":
                c.zoom_strength = float(value)
            elif what == "click_fx" and c.kind == "video":
                c.click_fx = bool(value)
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

    def _on_aspect(self, value: str) -> None:
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
        for b in self.aspect_group.buttons():
            b.setChecked(b.property("aspect") == self.project.aspect)
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
        if what == "delete_auto":
            self.history.push(self.project.to_dict())
            self.project.texts = [t for t in self.project.texts if not t.auto]
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
            for t in self._text_group(item):
                t.set_pos(self.project.aspect, 0.5, float(value))
        else:
            # свой стиль — меняем только этот текст, иначе общий стиль всех текстов
            target = item.style if item.style is not None else self.project.text_style
            target[what] = value
        self._text_changed()

    def _text_group(self, item: TextItem) -> list[TextItem]:
        return [t for t in self.project.texts if t.auto] if item.auto else [item]

    def _on_text_pressed(self, text_id: str) -> None:
        self.timeline.select_text(text_id)
        self.history.push(self.project.to_dict())

    def _on_text_moved(self, text_id: str, x: float, y: float) -> None:
        item = self.project.text_by_id(text_id)
        if item is not None:
            for t in self._text_group(item):      # автосубтитры двигаются все вместе
                t.set_pos(self.project.aspect, x, y)
            self._sync_preview()
            self._save_timer.start()

    # ---------- автосубтитры ----------

    def auto_subtitles(self) -> None:
        from glimpsy.editor.subtitles_dialog import SubtitlesDialog

        self.player.pause()
        if not subs.whisper_exe():
            QMessageBox.warning(self, "Автосубтитры", "В этой сборке нет программы распознавания речи. "
                                "Скачайте свежую версию Glimpsy.")
            return
        if not subs.has_speech_audio(self.project):
            QMessageBox.information(self, "Автосубтитры",
                                    "В ролике нет звука, из которого можно сделать субтитры.\n\n"
                                    "Запись экрана идёт без звука. Субтитры получатся, если вставить видео "
                                    "со своим голосом (например, с телефона или веб-камеры) — фрагментом "
                                    "или наложением — и не выключать у него звук.")
            return
        has_auto = any(t.auto for t in self.project.texts)
        dlg = SubtitlesDialog(self.project.aspect, has_auto, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        replace = has_auto and dlg.replace.isChecked()
        self._save()
        snapshot = Project(self.project.dir, self.project.name, fps=self.project.fps,
                           source_video=self.project.source_video)
        snapshot.restore(self.project.to_dict())
        prog = QProgressDialog("Подготовка…", "Отмена", 0, 1000, self)
        prog.setWindowTitle("Автосубтитры")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        bridge = _ExportBridge(self)
        cancel = threading.Event()
        prog.canceled.connect(cancel.set)
        bridge.progress.connect(lambda f, t: (prog.setValue(int(f * 1000)), prog.setLabelText(t)))
        result: dict = {}

        def done(_msg: str) -> None:
            prog.close()
            self._apply_subtitles(result.get("segs", []), replace)

        def failed(msg: str) -> None:
            prog.close()
            if msg:
                QMessageBox.warning(self, "Автосубтитры", msg)

        bridge.done.connect(done)
        bridge.failed.connect(failed)
        work = paths.temp_root() / f"subs_{int(time.time())}"
        model, lang, chars = dlg.model_key, dlg.language, dlg.max_chars

        def run() -> None:
            try:
                segs, detected = subs.transcribe(self.ffmpeg, snapshot, model, lang, chars, work,
                                                 progress=lambda f, t: bridge.progress.emit(f, t), cancel=cancel)
                log.info("Автосубтитры: %s фраз, язык %s", len(segs), detected)
                result["segs"] = segs
                bridge.done.emit("")
            except subs.Cancelled:
                bridge.failed.emit("")
            except Exception as e:
                log.exception("Автосубтитры не получились")
                bridge.failed.emit(str(e))
            finally:
                shutil.rmtree(work, ignore_errors=True)

        threading.Thread(target=run, daemon=True, name="subtitles").start()
        prog.show()

    def _apply_subtitles(self, segs: list, replace: bool) -> None:
        items = subs.make_texts(segs, self.project.total)
        if not items:
            QMessageBox.information(self, "Автосубтитры", "Речь в ролике не найдена.")
            return
        self.history.push(self.project.to_dict())
        if replace:
            self.project.texts = [t for t in self.project.texts if not t.auto]
        self.project.texts.extend(items)
        self.timeline.select_text(items[0].id)
        self._text_changed()
        self.statusBar().showMessage(f"Добавлено субтитров: {len(items)}. Стиль меняется у любого из них — "
                                     f"сразу для всех.", 8000)

    # ---------- наложения ----------

    def add_overlay_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT | VIDEO_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Картинка или видео поверх ролика", str(Path.home()),
                                                f"Картинки и видео ({exts});;Все файлы (*)")
        if files:
            self.add_overlays(files, self.player.t)

    def add_overlays(self, files: list, start: float) -> None:
        files = self._take_music(files)
        if not files:
            return
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
            QMessageBox.warning(self, "Glimpsy", "Не всё удалось добавить:\n\n" + "\n".join(errors))

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

    # ---------- музыка ----------

    def add_music_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXT))
        f, _ = QFileDialog.getOpenFileName(self, "Фоновая музыка", str(Path.home()),
                                           f"Музыка ({exts});;Все файлы (*)")
        if f:
            self.set_music(Path(f))

    def set_music(self, path: Path) -> bool:
        self.player.pause()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            duration = probe_audio(self.ffmpeg, path)
            rel = self.project.copy_media(path)
        except (ValueError, OSError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Glimpsy", f"Не удалось добавить музыку «{path.name}»: {e}")
            return False
        QApplication.restoreOverrideCursor()
        self.history.push(self.project.to_dict())
        old = self.project.music
        track = MusicTrack(rel, duration, label=path.name)
        if old is not None:          # замена трека — настройки сохраняем, кроме места начала
            track.volume, track.fade_in, track.fade_out = old.volume, old.fade_in, old.fade_out
            track.loop, track.duck = old.loop, old.duck
        self.project.music = track
        self.timeline.select_music(True)
        self._music_changed()
        return True

    def _on_music_select(self, on: bool) -> None:
        if on and self.project.music is not None:
            self.player.pause()
            self.music_panel.set_track(self.project.music)
            self.side.setCurrentWidget(self.music_panel)
        elif self.side.currentWidget() is self.music_panel:
            self.side.setCurrentWidget(self.inspector)
        self._update_actions()

    def _on_music_edit(self, what: str, value) -> None:
        m = self.project.music
        if m is None:
            return
        if what == "replace":
            self.add_music_dialog()
            return
        if what == "remove":
            self.history.push(self.project.to_dict())
            self.project.music = None
            self.timeline.select_music(False)
            self._music_changed()
            return
        self.history.push(self.project.to_dict(), key=f"music-{what}")
        if what in ("volume", "in_s", "fade_in", "fade_out"):
            setattr(m, what, max(0.0, float(value)))
        elif what in ("loop", "duck"):
            setattr(m, what, bool(value))
        self._music_changed()

    def _music_changed(self) -> None:
        self.timeline.update()
        self.player.refresh()
        if self.project.music is not None:
            self.music_panel.set_track(self.project.music)
        self._update_actions()
        self._save_timer.start()

    def delete_selected(self) -> None:
        if self.timeline.music_active:
            self._on_music_edit("remove", None)
            return
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

    def _take_music(self, files: list) -> list:
        """Музыкальные файлы среди перетащенных/вставленных — ставим фоновой музыкой, остальные возвращаем."""
        audio = [f for f in files if Path(f).suffix.lower() in AUDIO_EXT]
        if audio:
            self.set_music(Path(audio[-1]))
        return [f for f in files if Path(f).suffix.lower() not in AUDIO_EXT]

    def insert_files(self, files: list[str], index: int) -> None:
        files = self._take_music(files)
        if not files:
            return
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
            QMessageBox.warning(self, "Glimpsy", "Не всё удалось добавить:\n\n" + "\n".join(errors))

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

    def auto_montage(self) -> None:
        from glimpsy.editor import automontage
        from glimpsy.editor.automontage_dialog import AutomontageDialog

        if not self.project.clips:
            return
        dlg = AutomontageDialog(self.project.music is not None, self)
        if not dlg.exec():
            return
        opts = dlg.options()
        self.player.pause()
        beats, period = [], 0.0
        if opts.beats and self.project.music is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                beats, period = automontage.music_beats(self.ffmpeg, self.project)
            except Exception:
                log.exception("Не удалось найти доли музыки")
            finally:
                QApplication.restoreOverrideCursor()
        self.history.push(self.project.to_dict())
        report = automontage.apply(self.project, opts, beats, period)
        self._changed()
        text = "Автомонтаж: " + report.summary()
        if report.notes:
            text += " (" + "; ".join(report.notes) + ")"
        self.statusBar().showMessage(text + ". Отменить — Ctrl+Z", 10000)

    def show_stats(self) -> None:
        from glimpsy.editor.stats_dialog import StatsDialog
        StatsDialog(self.project, self).exec()

    # ---------- клавиши-переключатели ----------

    def _key_target(self) -> Clip | None:
        """Фрагмент для клавиши: выбранный, а если ничего не выбрано — тот, что под курсором."""
        sel = self._selected_clips()
        if sel:
            return sel[-1]
        idx, _ = self.project.locate(min(self.player.t, max(0.0, self.project.total - 1e-3)))
        c = self.project.clips[idx] if idx is not None else None
        if c is not None:
            self.timeline.select(c.id)
        return c

    def toggle_autozoom(self) -> None:
        c = self._key_target()
        targets = self._selected_clips()
        if c is None or not any(x.kind == "video" and x.cursor for x in targets):
            self.statusBar().showMessage("Автозум — только для записей экрана: в них сохранено, где был курсор", 4000)
            return
        on = not all(x.motion_raw(self.project.aspect) == "autozoom" for x in targets if x.cursor)
        self._on_inspector(c.id, "motion", "autozoom" if on else "none")
        self.statusBar().showMessage(f"Автозум {'включён' if on else 'выключен'} ({len(targets)} фр.)", 2500)

    def toggle_click_fx(self) -> None:
        c = self._key_target()
        targets = self._selected_clips()
        if c is None or not any(x.clicks for x in targets):
            self.statusBar().showMessage("В этих фрагментах нет записанных кликов", 3000)
            return
        on = not all(x.click_fx for x in targets if x.clicks)
        self._on_inspector(c.id, "click_fx", on)
        self.statusBar().showMessage(f"Подсветка кликов {'включена' if on else 'выключена'}", 2500)

    def _frame_key(self, what: str) -> None:
        c = self._key_target()
        if c is not None:
            self._on_inspector(c.id, what, None)

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
            if letter == "e" and not (ev.modifiers() & Qt.KeyboardModifier.AltModifier):
                if self.export_btn.isEnabled():
                    self.export()
                return True
            if letter == "a" and not typing:
                self.timeline.select_all()
                return True
            if letter == "v" and not typing:
                self.paste()
                return True
            return False
        # в числовых полях буквы не вводятся — там буквенные клавиши работают как обычно
        text_entry = isinstance(focus, (QLineEdit, QPlainTextEdit, QTextEdit))
        if not text_entry and letter and \
                not ev.modifiers() & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            action = {"z": self.toggle_autozoom, "c": self.toggle_click_fx, "t": self.add_text,
                      "f": lambda: self._frame_key("frame_fit"), "g": lambda: self._frame_key("frame_fill"),
                      "m": self.add_media_dialog, "s": self.split}.get(letter or "")
            if action is not None and not keys.has_shift(ev):
                action()
                return True
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
