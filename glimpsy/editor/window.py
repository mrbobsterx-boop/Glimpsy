"""Главное окно редактора.

Раскладка как в CapCut:
  ┌──────────── панель инструментов ────────────┐
  │            просмотр            │  свойства  │
  │  ▶  00:12.3 / 01:00.0          │            │
  ├──────────────── лента ──────────────────────┤
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QEventLoop, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QMenu, QButtonGroup, QDialog, QFileDialog, QFrame, QScrollArea, QToolButton, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QSlider, QSplitter, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
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
from glimpsy.editor.project import (
    ASPECTS, DEFAULT_FRAME, TRACK_NAMES, Clip, History, Project, cover_zoom, new_id,
)
from glimpsy.editor.text import TextItem, effective_style, load_custom_fonts
from glimpsy.editor.text_panel import TextPanel
from glimpsy.editor import transcript as tr
from glimpsy.editor import translate as mt
from glimpsy.editor.overlay import OverlayItem
from glimpsy.editor.overlay_video import OverlayVideos
from glimpsy.editor.overlay_panel import OverlayPanel
from glimpsy.editor.music import AUDIO_EXT, MusicTrack, probe_audio
from glimpsy.editor.music_panel import MusicPanel
from glimpsy.editor import subtitles as subs
from glimpsy.editor.timeline import LANE_ICONS, TimelineWidget, fmt_time
from glimpsy.recorder.encoder import Encoder

log = logging.getLogger(__name__)

ASPECT_CHOICES = [("16:9", "16:9 — YouTube"), ("9:16", "9:16 — Reels, TikTok, Shorts")]


def vlog_apply(p: Project, plan, fmt: str, subs_on: bool) -> None:
    """План автомонтажа → пометки монтажа по тексту в проекте p (без пересборки кусков)."""
    from glimpsy.editor import vlog

    cuts = p.cuts
    for src, idx in plan.deleted.items():
        cuts.setdefault("deleted", {})[src] = sorted(set(cuts.get("deleted", {}).get(src, [])) | idx)
    cuts["broll"] = {src: [list(r) for r in rng] for src, rng in plan.broll.items()}
    cuts.update({"pause_cut": True, "mode": "auto"})
    aspect = vlog.FORMATS[fmt][1]
    p.aspect = aspect
    W, H = ASPECTS[aspect]
    for s in cuts.get("sources", []):
        w, h = int(s.get("width", 0)), int(s.get("height", 0))
        if aspect == "9:16" and w > h > 0:             # горизонтальное видео в вертикальный ролик — по центру
            s.setdefault("frames", {})["9:16"] = [round(cover_zoom(w, h, W, H), 4), 0.0, 0.0]
    if subs_on:
        cuts["subtitles"] = True


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
                 fallback_output: Path, on_sessions: Callable[[], None] | None = None,
                 on_open: Callable[[Path], None] | None = None) -> None:
        super().__init__()
        self.on_sessions = on_sessions      # «Все записи»: вернуться к списку сессий
        self.on_open = on_open              # открыть другой проект (вариант автомонтажа) в своём окне
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
        # видео поверх ролика (камера, медиа) в просмотре играет само — плавно, а не кадром в секунду
        self.ov_video = OverlayVideos(self)
        self._ov_redraw = QTimer(self, singleShot=True, interval=0)
        self._ov_redraw.timeout.connect(self._sync_preview)
        self.ov_video.frame_ready.connect(lambda: self.player.playing or self._ov_redraw.start())
        self._motion_cache: dict = {}

        self._build_ui()

        # --- связи ---
        self.player.frame.connect(self.preview.set_image)
        self.player.position.connect(self._on_position)
        self.player.playing_changed.connect(
            lambda on: self.play_btn.setIcon(self._icon_pause if on else self._icon_play))
        self.player.playing_changed.connect(lambda _on: self._sync_preview())
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
        self.preview.region_picked.connect(self._on_region_picked)
        self.timeline.music_selected.connect(self._on_music_select)
        self.music_panel.edited.connect(self._on_music_edit)
        self.thumbs.ready.connect(self._sync_preview)     # кадры видео-наложений подгружаются в фоне

        self._save_timer = QTimer(self, singleShot=True, interval=500)
        self._save_timer.timeout.connect(self._save)
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self._initial)

    def _initial(self) -> None:
        self._restore_left_panel()
        self.timeline.fit()
        self.player.seek(0.0)
        self._update_actions()

    # ---------- раскладка окна ----------

    def _build_ui(self) -> None:
        """Сверху — логотип, формат и «Экспорт»; слева — инструменты; в центре — просмотр;
        справа — свойства; внизу — лента с её кнопками."""
        from glimpsy.editor.library_panel import LibraryPanel
        from glimpsy.editor.subtitles_panel import SubtitlesPanel
        from glimpsy.editor.transcript_panel import TranscriptPanel
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
        self.export_btn.setToolTip("Сохранить готовый ролик (Ctrl+E — в текущем формате)")
        export_menu = QMenu(self.export_btn)
        export_menu.addAction(theme.icon("monitor", size=16), "16:9 — YouTube", lambda: self.export(["16:9"]))
        export_menu.addAction(theme.icon("smartphone", size=16), "9:16 — Reels, TikTok, Shorts",
                              lambda: self.export(["9:16"]))
        export_menu.addSeparator()
        export_menu.addAction(theme.icon("layers", size=16), "Оба формата сразу (два файла)",
                              lambda: self.export(["16:9", "9:16"]))
        self.export_btn.setMenu(export_menu)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(14, 8, 12, 8)
        tl.setSpacing(10)
        if self.on_sessions is not None:
            back = theme.mark(QPushButton(theme.icon("layout-grid", theme.MUTED, 16), "  Все записи"), "ghost")
            back.setToolTip("Вернуться к списку всех записей (проект сохраняется сам)")
            back.clicked.connect(self.back_to_sessions)
            tl.addWidget(back)
            div = theme.mark(QFrame(), "vdivider")
            div.setFixedSize(1, 18)
            tl.addWidget(div)
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

        # панели слева: открывается одна, повторный щелчок по кнопке — свернуть.
        # Свой «миниатюрщик» у файловых панелей — сотни файлов не задерживают кадры на ленте.
        lib_thumbs = Thumbnailer(self.ffmpeg)
        self.library = LibraryPanel(lib_thumbs, self.fallback_output, mode="files")
        self.media_lib = LibraryPanel(lib_thumbs, self.fallback_output, mode="media", open_file=self.add_media_dialog)
        self.overlay_lib = LibraryPanel(lib_thumbs, self.fallback_output, mode="overlay",
                                        open_file=self.add_overlay_dialog)
        self.music_lib = LibraryPanel(lib_thumbs, self.fallback_output, mode="music", open_file=self.add_music_dialog)
        self.subs_panel = SubtitlesPanel()
        self.library.add_requested.connect(lambda files: self.add_overlays(files, self.player.t, "media"))
        self.media_lib.add_requested.connect(lambda files: self.add_overlays(files, self.player.t, "media"))
        self.overlay_lib.add_requested.connect(lambda files: self.add_overlays(files, self.player.t, "overlay"))
        self.music_lib.add_requested.connect(lambda files: files and self.set_music(Path(files[-1])))
        self.subs_panel.recognize_requested.connect(self.auto_subtitles)
        self.subs_panel.add_requested.connect(self.add_subtitle)
        self.subs_panel.selected.connect(self.timeline.select_text)
        self.subs_panel.text_edited.connect(lambda tid, text: self._on_text_edit(tid, "text", text))
        self.subs_panel.delete_requested.connect(self._delete_texts)
        self._left: dict[str, tuple[QToolButton, QWidget]] = {}

        def left(name: str, ic: str, text: str, tip: str, panel) -> None:
            b = tool(ic, text, tip, checkable=True)
            b.toggled.connect(lambda on, n=name: self._toggle_left(n, on))
            panel.close_requested.connect(lambda: b.setChecked(False))
            self._left[name] = (b, panel)

        # монтаж по тексту — у проектов из своих видео
        self.text_panel_t = TranscriptPanel()
        if self.project.text_edit:
            left("text", "captions", "Текст", "Монтаж по тексту: расшифровка речи, вырезать словами и паузы",
                 self.text_panel_t)
            self._setup_text_edit()
        left("files", "folder-open", "Файлы", "Файлы с компьютера: видео, фото и музыка под рукой", self.library)
        left("media", "image-plus", "Медиа", "Видео и фото на дорожку «Медиа» — на весь кадр, с места курсора (M). "
             "Вставить между фрагментами видео — перетащите файл на дорожку видео или Ctrl+V", self.media_lib)
        tool("type", "Текст", "Добавить текст в месте курсора (T)", self.add_text)
        left("subtitles", "captions", "Субтитры", "Все субтитры списком: распознать речь, исправить, удалить",
             self.subs_panel)
        left("overlay", "layers", "Наложение", "Картинка или видео поверх ролика", self.overlay_lib)
        left("music", "music", "Музыка", "Фоновая музыка на весь ролик", self.music_lib)
        tool("square-split-horizontal", "До/после", "Вставка «Было → стало»: шторка, таймлапс или стоп-кадр",
             self.before_after)
        tool("mic", "Запись", "Записать голос, камеру или то и другое — прямо в ролик, с места курсора",
             self.record)
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
        add_track = theme.mark(QPushButton(theme.icon("plus", size=16), " Дорожка"), "ghost")
        add_track.setToolTip("Добавить дорожку — сколько угодно; правый щелчок по дорожке — ещё действия")
        track_menu = QMenu(add_track)
        for kind in ("text", "subtitles", "overlay", "media", "camera", "voice"):
            track_menu.addAction(theme.icon(LANE_ICONS[kind], size=16), TRACK_NAMES[kind],
                                 lambda _=False, k=kind: self.timeline.add_track(k))
        add_track.setMenu(track_menu)
        bar.addSpacing(6)
        bar.addWidget(add_track)
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
        # дорожек может быть сколько угодно — лента прокручивается по высоте
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline_scroll.setWidget(self.timeline)
        bl.addWidget(self.timeline_scroll, 1)

        root = QSplitter(Qt.Orientation.Vertical)
        root.setHandleWidth(6)
        root.addWidget(top)
        root.addWidget(bottom)
        root.setStretchFactor(0, 1)
        root.setStretchFactor(1, 0)
        root.setSizes([560, 240])

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 8, 8)
        body.setSpacing(6)
        body.addWidget(rail)
        for _b, panel in self._left.values():
            body.addWidget(panel)
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

    # ---------- панели слева ----------

    def _restore_left_panel(self) -> None:
        store = QSettings("Glimpsy", "editor")
        name = store.value("left_panel", "", type=str)
        if not name and store.value("library_open", False, type=bool):
            name = "files"                    # раньше была только панель «Файлы»
        if self.project.text_edit:
            name = "text"                     # проект из своих видео — сразу текст
        if name in self._left:
            self._left[name][0].setChecked(True)

    def _toggle_left(self, name: str, on: bool) -> None:
        btn, panel = self._left[name]
        if on:
            for other, (b, _p) in self._left.items():
                if other != name and b.isChecked():
                    b.setChecked(False)
            panel.set_open(True)
            if panel is self.subs_panel:
                self.subs_panel.refresh(self.project, self.timeline.selected_text)
            QSettings("Glimpsy", "editor").setValue("left_panel", name)
        else:
            panel.set_open(False)
            if not any(b.isChecked() for b, _p in self._left.values()):
                QSettings("Glimpsy", "editor").setValue("left_panel", "")

    def toggle_left(self, name: str) -> None:
        b = self._left[name][0]
        b.setChecked(not b.isChecked())

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
        if self.project.text_edit and self.text_panel_t.isVisible():
            at = tr.source_time(self.project, t)
            if at is not None:
                self.text_panel_t.view.set_current(self._src_index(at[0]), at[1], self.player.playing)

    # ---------- монтаж по тексту ----------

    def _setup_text_edit(self) -> None:
        self.tstore = tr.TranscriptStore(self.project.dir)
        # расшифровки, сделанные раньше, могли содержать подписи звуков («*звук*») — убираем,
        # пометки монтажа переносим на новые номера слов
        cleaned = False
        for s in self.project.cuts.get("sources", []):
            mapping = self.tstore.clean_noise(s["src"])
            if mapping is not None:
                tr.remap_cuts(self.project.cuts, s["src"], mapping)
                cleaned = True
        if cleaned:
            QTimer.singleShot(0, self._recut)
        elif any(self.tstore.get(s["src"]) for s in self.project.cuts.get("sources", [])):
            tr.rebuild_clips(self.project, self.tstore)   # куски — по текущим правилам; края можно тянуть
        self._tr_cancel: threading.Event | None = None
        p = self.text_panel_t
        p.view.clicked.connect(self._on_text_token)
        p.view.delete_keys.connect(self._cut_tokens)
        p.view.play_toggle.connect(self.player.toggle)
        p.view.context.connect(self._text_menu)
        p.view.edit_word.connect(self._edit_word)
        p.view.selection_menu.connect(self._selection_menu)
        p.selection_action.connect(lambda a: self._selection_do(a, p.view.keys_in_selection()))
        p.subtitles_toggled.connect(self._toggle_text_subs)
        p.subs_lang_changed.connect(self._on_subs_lang)
        self._mt_cache = mt.Cache(self.project.dir)
        self._voice_job = False
        self._trim_timer = QTimer(self, singleShot=True, interval=500)
        self._trim_timer.timeout.connect(self._trims_done)
        if self.project.cuts.get("subtitles") and not any(t.auto for t in self.project.texts):
            QTimer.singleShot(0, self._recut)          # проект от автомонтажа — субтитры ещё не созданы
        self._mt_busy = False
        self._mt_todo: set[str] = set()
        p.settings_changed.connect(self._on_cut_settings)
        p.pause_length.connect(self._on_pause_length)
        p.fillers_changed.connect(self._set_fillers)
        p.fillers_cut.connect(lambda ws: self._cut_fillers(ws, True))
        p.fillers_restore.connect(lambda ws: self._cut_fillers(ws, False))
        self._sel_pause: tuple[int, int] | None = None
        p.transcribe_requested.connect(self.transcribe)
        p.cancel_requested.connect(lambda: self._tr_cancel is not None and self._tr_cancel.set())
        self._tr_build()

    def _sources(self) -> list[dict]:
        return self.project.cuts.get("sources", [])

    def _src_index(self, src: str) -> int:
        return next((i for i, s in enumerate(self._sources()) if s["src"] == src), -1)

    def _tr_build(self, keep_scroll: bool = False) -> None:
        """Заново показать текст всех видео (после расшифровки, смены порога пауз или длины паузы)."""
        cuts = tr.cuts_of(self.project)
        items = []
        for s in self._sources():
            sw = self.tstore.get(s["src"])
            plist = []
            if sw is not None:
                marks = tr.pause_marks(cuts, s["src"])
                plist = [(i, g, marks[i] if marks.get(i, 0) > 0 else None,
                          tr.broll_in_gap(cuts, s["src"], *self._gap_bounds(sw, i)))
                         for i, g in tr.shown_pauses(sw, cuts, marks)]
            items.append((s["src"], s.get("label", Path(s["src"]).name), sw, plist,
                          cuts.get("word_text", {}).get(s["src"], {})))
        view = self.text_panel_t.view
        self.text_panel_t.set_settings(cuts)
        view.build(items, view.verticalScrollBar().value() if keep_scroll else None)
        self.text_panel_t.set_needs_transcript(sum(1 for it in items if it[2] is None))
        self._tr_states()

    def _tr_states(self) -> None:
        """Что вырезано — зачёркнуто, укороченные паузы и найденные слова-паразиты — своим цветом."""
        if not self.project.text_edit:
            return
        cuts = tr.cuts_of(self.project)
        fillers = cuts.get("mode") == "fillers"
        counts: dict[str, tuple[int, int]] = {}
        states: dict[tuple, str] = {}
        for si, s in enumerate(self._sources()):
            sw = self.tstore.get(s["src"])
            if sw is None:
                continue
            gone = set(cuts.get("deleted", {}).get(s["src"], []))
            if fillers:
                for f, hits in tr.find_fillers(sw.words, self._fillers()).items():
                    n, g = counts.get(f, (0, 0))
                    counts[f] = (n + len(hits), g + sum(1 for i in hits if i in gone))
                    for i in hits:
                        states[("w", si, i)] = "filler"
            for i in tr.respoken(cuts, s["src"]):
                states[("w", si, i)] = "voice"
            for field, flag in (("muted", "mute"), ("hidden", "hide")):
                for i in cuts.get(field, {}).get(s["src"], []):
                    k = ("w", si, i)
                    states[k] = flag if states.get(k, "keep") in ("keep", "filler") else states[k] + " " + flag
            for i in gone:
                states[("w", si, i)] = "cut"
            marks = tr.pause_marks(cuts, s["src"])
            for i, gap in tr.shown_pauses(sw, cuts, marks):
                st = tr.pause_state(i, gap, cuts, marks)
                if st == "cut" and tr.broll_in_gap(cuts, s["src"], *self._gap_bounds(sw, i)) > 0.05:
                    st = "short"                           # в вырезанной паузе оставлены кадры автомонтажа
                if st != "keep":
                    states[("p", si, i)] = st
        self.text_panel_t.view.apply_states(states)
        if fillers:
            self.text_panel_t.set_fillers(self._fillers(), counts)
        self.text_panel_t.set_stats(sum(float(s["duration"]) for s in self._sources()), self.project.total)

    def _recut(self) -> None:
        """Пометки изменились — пересобрать фрагменты из расшифровки (и субтитры из текста, если включены)."""
        tr.rebuild_clips(self.project, self.tstore)
        self._place_respeak()
        if self.project.cuts.get("subtitles"):
            self._make_text_subs()
        self._changed()

    # ---------- звук и картинка отдельно ----------

    def _selection_do(self, action: str, keys: list) -> None:
        """С выделенным текстом: вырезать всё, убрать звук, убрать картинку или вернуть всё."""
        words = [(si, i) for kind, si, i in keys if kind == "w"]
        if not words:
            self.statusBar().showMessage("Сначала выделите слова в тексте", 3000)
            return
        if action == "cut":
            self._cut_tokens(keys)
            return
        if action == "respeak":
            self._respeak(words)
            return
        self.history.push(self.project.to_dict())
        if action == "restore":
            for si, i in words:
                src = self._sources()[si]["src"]
                for field in ("deleted", "muted", "hidden"):
                    self._mark(field, src, i, False)
        else:
            field = "muted" if action == "mute" else "hidden"
            # всё выделенное уже помечено — значит, вернуть; иначе — пометить
            on = not all(i in self.project.cuts.get(field, {}).get(self._sources()[si]["src"], [])
                         for si, i in words)
            for si, i in words:
                self._mark(field, self._sources()[si]["src"], i, on)
        self._recut()

    def _selection_menu(self, keys: list, pos) -> None:
        m = QMenu(self)
        m.addAction("Вырезать (звук и картинку)", lambda: self._selection_do("cut", keys))
        m.addAction("Убрать / вернуть только звук", lambda: self._selection_do("mute", keys))
        m.addAction("Убрать / вернуть только картинку", lambda: self._selection_do("hide", keys))
        m.addSeparator()
        m.addAction("Переозвучить своим голосом…", lambda: self._selection_do("respeak", keys))
        m.addAction("Сохранить голос из этого куска…", lambda: self._save_voice(keys))
        m.addAction("Вернуть всё", lambda: self._selection_do("restore", keys))
        m.exec(pos)

    # ---------- переозвучка своим голосом ----------

    def _respeak_entry(self, src: str, i: int) -> dict | None:
        return next((r for r in self.project.cuts.get("respeak", {}).get(src, []) if r["i"] <= i <= r["j"]), None)

    def _respeak(self, words: list[tuple[int, int]]) -> None:
        """Выделенную фразу исправить и озвучить своим голосом."""
        from glimpsy.editor import voice

        si = words[0][0]
        idx = sorted(i for s_, i in words if s_ == si)
        i, j = idx[0], idx[-1]
        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        if sw is None:
            return
        if any(self._respeak_entry(src, k) for k in range(i, j + 1)):
            QMessageBox.information(self, "Переозвучка", "Эта фраза уже переозвучена. Чтобы озвучить иначе, "
                                    "сначала верните исходную (правая кнопка по зелёному слову).")
            return
        if self._voice_job:
            QMessageBox.information(self, "Переозвучка", "Предыдущая фраза ещё озвучивается — подождите.")
            return
        if not voice.supported():
            QMessageBox.warning(self, "Переозвучка", "На этом компьютере голосовой модуль не работает "
                                "(нужен Linux или Windows на x86-64 либо Mac на Apple Silicon).")
            return
        if j - i > 40:
            QMessageBox.information(self, "Переозвучка", "Выделите фразу покороче (до 40 слов).")
            return
        from glimpsy.editor.voices_dialog import RespeakDialog

        cuts = self.project.cuts
        old = " ".join(t for k in range(i, j + 1) if (t := tr.word_text(cuts, src, k, sw.words[k].text)))
        ref = voice.reference_spans(sw.words, i, j, set(cuts.get("deleted", {}).get(src, [])))
        dlg = RespeakDialog(old, sum(b - a for a, b in ref), self.ffmpeg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = " ".join(dlg.text.text().split())
        if not text:
            return
        if not voice.installed() and not self._install_voice():
            return
        self._start_respeak(si, i, j, text, dlg.voice_id)

    def _save_voice(self, keys: list) -> None:
        """Выделенная речь → голос в библиотеке (для переозвучки в любых проектах)."""
        from PySide6.QtWidgets import QInputDialog

        from glimpsy.editor import voice
        from glimpsy.editor.voices_dialog import save_voice_async

        words = [(si, i) for kind, si, i in keys if kind == "w"]
        if not words:
            return
        si = words[0][0]
        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        if sw is None:
            return
        gone = set(self.project.cuts.get("deleted", {}).get(src, []))
        idx = sorted(i for s_, i in words if s_ == si and i not in gone)
        spans: list[tuple[float, float]] = []
        for k in idx:
            a, b = max(0.0, sw.words[k].start - 0.05), sw.words[k].end + 0.05
            if spans and a - spans[-1][1] < 0.35:
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
        total = sum(b - a for a, b in spans)
        if total < voice.MIN_SAMPLE_S:
            QMessageBox.information(self, "Голос", f"Выделено {total:.1f} с речи, а для голоса нужно хотя бы "
                                    f"{voice.MIN_SAMPLE_S:.0f} с (лучше 10–20). Выделите кусок побольше.")
            return
        name, ok = QInputDialog.getText(self, "Сохранить голос",
                                        "Как назвать голос (например, «Мой голос», «Саша»):", text="Мой голос")
        name = " ".join(name.split())
        if not ok or not name:
            return

        def done(msg: str) -> None:
            if msg.startswith("ok:"):
                self.statusBar().showMessage(f"Голос «{name}» сохранён — его можно выбрать при переозвучке "
                                             f"в любом проекте.", 8000)
            else:
                QMessageBox.warning(self, "Голос", msg)

        save_voice_async(self.ffmpeg, name, Path(src), spans, self, done)

    def _install_voice(self) -> bool:
        """Спросить и поставить голосовой модуль (один раз; долго)."""
        from glimpsy.editor import voice

        ask = QMessageBox.question(
            self, "Голосовой модуль",
            f"Для озвучки вашим голосом нужен голосовой модуль — нейросеть Chatterbox (бесплатная, лицензия MIT: "
            f"можно и для коммерческих роликов).\n\nОн скачается один раз (≈ {voice.SIZE_GB} ГБ, с интернета) и "
            f"дальше работает без интернета, прямо на компьютере. Установка займёт 10–30 минут — "
            f"смотря какой интернет.\n\nПоставить сейчас?")
        if ask != QMessageBox.StandardButton.Yes:
            return False
        prog = QProgressDialog("Подготовка…", "Отмена", 0, 1000, self)
        prog.setWindowTitle("Голосовой модуль")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        bridge = _ExportBridge(self)
        cancel = threading.Event()
        prog.canceled.connect(cancel.set)
        bridge.progress.connect(lambda f, t: (prog.setValue(int(f * 1000)), prog.setLabelText(t)))
        result = {"err": None}
        loop = QEventLoop(self)
        bridge.done.connect(lambda _m: loop.quit())
        bridge.failed.connect(lambda m: (result.update(err=m), loop.quit()))

        def run() -> None:
            try:
                voice.install(lambda f, t: bridge.progress.emit(f, t), cancel)
                bridge.done.emit("")
            except voice.Cancelled:
                bridge.failed.emit("")
            except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
                log.exception("Голосовой модуль не установился")
                bridge.failed.emit(str(e))

        threading.Thread(target=run, daemon=True, name="voice-install").start()
        prog.show()
        loop.exec()
        prog.close()
        if result["err"]:
            QMessageBox.warning(self, "Голосовой модуль", result["err"] + "\n\nПроверьте интернет и место на "
                                "диске и попробуйте ещё раз — уже скачанное повторно не качается.")
        return result["err"] is None

    def _start_respeak(self, si: int, i: int, j: int, text: str, voice_id: str = "") -> None:
        """Озвучить фразу в фоне; готово — встаёт на место исходной (одним шагом отмены)."""
        from glimpsy.editor import voice

        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        cuts = tr.cuts_of(self.project)
        env = self.tstore.envelope(src)
        span = tr.mark_spans(sw, set(range(i, j + 1)), cuts, env)[0]
        target = span[1] - span[0]
        lang = sw.language if sw.language in voice.LANGS else "ru"
        ref_spans = voice.reference_spans(sw.words, i, j, set(cuts.get("deleted", {}).get(src, [])))
        saved = voice.get_voice(voice_id) if voice_id else None
        rid = new_id()
        out_dir = self.project.dir / "voice"
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"respeak_{rid}.wav"
        self._voice_job = True
        panel = self.text_panel_t
        first = voice.speaker().proc is None
        panel.set_voice_status(f"Озвучиваю «{text}» " + (f"голосом «{saved.name}»… " if saved else "вашим голосом… ") +
                               ("Первая фраза — около минуты-двух (загружается нейросеть)." if first else
                                "Обычно 20–40 секунд."))
        bridge = _ExportBridge(self)
        result: dict = {}

        def done(_m: str) -> None:
            self._voice_job = False
            panel.set_voice_status("")
            self.history.push(self.project.to_dict())
            entry = {"id": rid, "i": i, "j": j, "text": text, "file": str(final.relative_to(self.project.dir)),
                     "dur": round(result["dur"], 3), "orig": round(target, 3),
                     "voice": saved.name if saved else ""}
            self.project.cuts.setdefault("respeak", {}).setdefault(src, []).append(entry)
            fixes = self.project.cuts.setdefault("word_text", {}).setdefault(src, {})
            fixes[str(i)] = text
            for k in range(i + 1, j + 1):
                fixes[str(k)] = ""
            self._recut()
            self._tr_build(keep_scroll=True)
            over = result["dur"] - target
            self.statusBar().showMessage(
                "Фраза переозвучена." + (f" Она на {over:.1f} с длиннее исходной — немного зайдёт на следующие "
                                         f"слова." if over > 0.3 else "") +
                " Вернуть исходную — правая кнопка по зелёному слову или Ctrl+Z.", 10000)

        def failed(msg: str) -> None:
            self._voice_job = False
            panel.set_voice_status("")
            if msg:
                QMessageBox.warning(self, "Переозвучка", msg)

        bridge.done.connect(done)
        bridge.failed.connect(failed)

        def run() -> None:
            raw = out_dir / f"respeak_{rid}_raw.wav"
            ref = out_dir / f"respeak_{rid}_ref.wav"
            try:
                if saved is not None:                      # сохранённый голос — образец уже готов
                    voice.speaker().say(text, lang, saved.sample, raw, conds=saved.conds)
                else:
                    voice.make_reference(self.ffmpeg, Path(src), ref_spans, ref)
                    voice.speaker().say(text, lang, ref, raw)
                result["dur"] = voice.fit_phrase(self.ffmpeg, raw, final, target, Path(src), span[0], span[1])
                bridge.done.emit("")
            except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
                log.exception("Переозвучка не удалась")
                bridge.failed.emit(str(e))
            finally:
                raw.unlink(missing_ok=True)
                ref.unlink(missing_ok=True)

        threading.Thread(target=run, daemon=True, name="respeak").start()

    def _unrespeak(self, src: str, rid: str) -> None:
        entries = self.project.cuts.get("respeak", {}).get(src, [])
        entry = next((r for r in entries if r["id"] == rid), None)
        if entry is None:
            return
        self.history.push(self.project.to_dict())
        entries.remove(entry)
        fixes = self.project.cuts.get("word_text", {}).get(src, {})
        for k in range(entry["i"], entry["j"] + 1):
            fixes.pop(str(k), None)
        self._recut()
        self._tr_build(keep_scroll=True)

    def _place_respeak(self) -> None:
        """Переозвученные фразы — на дорожку «Голос», туда, где сейчас в ролике исходная фраза."""
        self.project.overlays = [o for o in self.project.overlays if not o.auto]
        cuts = tr.cuts_of(self.project)
        track = None
        for src, entries in cuts.get("respeak", {}).items():
            sw = self.tstore.get(src)
            if sw is None:
                continue
            env = self.tstore.envelope(src)
            for r in entries:
                spans = tr.mark_spans(sw, set(range(r["i"], r["j"] + 1)), cuts, env)
                start = tr.output_time(self.project, src, spans[0][0]) if spans else None
                if start is None:
                    continue                               # фразу вырезали целиком
                track = track or self.project.track_for("voice").id
                self.project.overlays.append(OverlayItem(
                    new_id(), "audio", r["file"], round(start, 3), float(r["dur"]), src_duration=float(r["dur"]),
                    has_audio=True, label="Переозвучка: " + r["text"], track=track, auto=r["id"]))

    # ---------- субтитры из текста ----------

    def _make_text_subs(self) -> int:
        """Автосубтитры ← оставленные слова (во времени готового ролика). Сдвиги и стиль общие.
        Если выбран другой язык — переведённые строки (чего ещё нет в переводе — переводится в фоне)."""
        track = self.project.track_for("subtitles").id
        old = [t for t in self.project.texts if t.auto]
        pos = old[0].pos if old and all(t.pos == old[0].pos for t in old) else {}
        self.project.texts = [t for t in self.project.texts if not t.auto]
        words = tr.output_words(self.project, self.tstore)
        lang, src_lang = self._subs_langs()
        if lang and lang != src_lang:
            lines, missing = mt.translated_cues(words, self._mt_cache, self.project.cuts.get("subs_mt", "m2m"), lang,
                                                max_chars=self._subs_chars())
            if missing:
                self._translate_later(missing)
        else:
            lines = tr.cues(words, self._subs_chars())
        items = []
        for a, b, text in lines:
            items.append(TextItem(new_id(), text, round(a, 2), round(max(0.3, b - a), 2), pos=dict(pos),
                                  auto=True, track=track))
        self.project.texts.extend(items)
        self.subs_panel.refresh(self.project, self.timeline.selected_text)
        return len(items)

    def _subs_chars(self) -> int:
        """Длина строки субтитров: в вертикальном ролике — короче."""
        return 24 if self.project.aspect == "9:16" else 42

    def _subs_langs(self) -> tuple[str, str]:
        """(язык субтитров или "", язык речи)."""
        src = next((sw.language for s in self._sources() if (sw := self.tstore.get(s["src"])) and sw.language), "ru")
        return self.project.cuts.get("subs_lang") or "", src if src in mt.LANGS else "ru"

    def _on_subs_lang(self, lang: str) -> None:
        from glimpsy.editor.subtitles_dialog import TranslatorDialog

        key = self.project.cuts.get("subs_mt") or QSettings("Glimpsy", "editor").value("translate/model", "m2m")
        if lang:
            if not mt.available():
                QMessageBox.warning(self, "Перевод субтитров", "В этой сборке нет переводчика. "
                                    "Скачайте свежую версию Glimpsy.")
                self.text_panel_t.set_settings(tr.cuts_of(self.project))
                return
            if not mt.model_ready(key):
                dlg = TranslatorDialog(mt.LANGS[lang][0], self)
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    self.text_panel_t.set_settings(tr.cuts_of(self.project))
                    return
                key = dlg.model_key
        self.history.push(self.project.to_dict())
        self.project.cuts["subs_lang"] = lang
        self.project.cuts["subs_mt"] = key
        self.project.cuts["subtitles"] = True              # выбрали язык — значит, субтитры нужны
        self._make_text_subs()
        self.text_panel_t.set_settings(tr.cuts_of(self.project))
        self._changed()

    def _translate_later(self, sentences: list[str]) -> None:
        """Перевести предложения в фоне; когда готово — субтитры обновятся сами."""
        self._mt_todo.update(sentences)
        if self._mt_busy or not self._mt_todo:
            return
        lang, src = self._subs_langs()
        key = self.project.cuts.get("subs_mt", "m2m")
        if not mt.model_ready(key):
            self.text_panel_t.set_subs_status("Переводчик не скачан — выберите язык субтитров заново.")
            self._mt_todo.clear()
            return
        todo = sorted(self._mt_todo)
        self._mt_todo.clear()
        self._mt_busy = True
        cache = self._mt_cache
        bridge = _ExportBridge(self)
        panel = self.text_panel_t
        panel.set_subs_status(f"Перевожу субтитры… (0 из {len(todo)})")
        bridge.progress.connect(lambda f, t: panel.set_subs_status(t))

        def finished(msg: str) -> None:
            self._mt_busy = False
            panel.set_subs_status(msg)
            if not msg and self.project.cuts.get("subtitles") and self.project.cuts.get("subs_lang") == lang:
                self._make_text_subs()                     # строки с переводом вместо оригинала
                self.timeline.update()
                self._sync_preview()
                self._save_timer.start()
            if self._mt_todo:
                self._translate_later([])

        bridge.done.connect(finished)
        bridge.failed.connect(finished)

        def run() -> None:
            try:
                step = 16
                for i in range(0, len(todo), step):
                    part = todo[i:i + step]
                    cache.put(key, lang, dict(zip(part, mt.translate(part, src, lang, key))))
                    bridge.progress.emit(0.0, f"Перевожу субтитры… ({min(len(todo), i + step)} из {len(todo)})")
                bridge.done.emit("")
            except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
                log.exception("Перевод не удался")
                bridge.failed.emit(f"Перевод не удался: {e}")

        threading.Thread(target=run, daemon=True, name="translate").start()

    def _toggle_text_subs(self, on: bool) -> None:
        self.history.push(self.project.to_dict())
        self.project.cuts["subtitles"] = on
        if on:
            n = self._make_text_subs()
            self.statusBar().showMessage(f"Субтитров на видео: {n}. Они сами обновляются при вырезании; "
                                         f"вид меняется в панели «Субтитры» или у любого из них.", 8000)
        else:
            self.project.texts = [t for t in self.project.texts if not t.auto]
            self.subs_panel.refresh(self.project, self.timeline.selected_text)
        self._changed()

    def _edit_word(self, key: tuple) -> None:
        """Исправить слово, которое распознавание услышало неправильно (видео не меняется)."""
        from PySide6.QtWidgets import QInputDialog

        _kind, si, i = key
        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        if sw is None:
            return
        cur = tr.word_text(self.project.cuts, src, i, sw.words[i].text)
        text, ok = QInputDialog.getText(self, "Исправить слово", "Как правильно:", text=cur)
        text = " ".join(text.split())
        if not ok or not text or text == cur:
            return
        self.history.push(self.project.to_dict())
        fixes = self.project.cuts.setdefault("word_text", {}).setdefault(src, {})
        if text == sw.words[i].text:
            fixes.pop(str(i), None)
        else:
            fixes[str(i)] = text
        self._recut()
        self._tr_build(keep_scroll=True)

    def _mark(self, field: str, src: str, idx: int, on: bool) -> None:
        marks = self.project.cuts.setdefault(field, {})
        cur = set(marks.get(src, []))
        cur.add(idx) if on else cur.discard(idx)
        marks[src] = sorted(cur)

    # ---------- края кусков, растянутые на ленте ----------

    def _capture_trims(self) -> None:
        """Кусок растянули или укоротили на ленте — запомнить это в пометках монтажа, иначе при
        следующей правке текста куски пересоберутся и правка пропадёт."""
        keys = getattr(self.project, "edge_keys", {})
        moved = False
        for c in self.project.clips:
            k = keys.get(c.id)
            if k is None:
                continue
            src, raw_a, raw_b, a0, b0 = k
            if raw_a is not None and abs(c.in_s - a0) > 1e-3:
                tr.set_edge(self.project.cuts, src, raw_a, c.in_s)
                moved = True
            if raw_b is not None and abs(c.out_s - b0) > 1e-3:
                tr.set_edge(self.project.cuts, src, raw_b, c.out_s)
                moved = True
            if moved:
                keys[c.id] = (src, raw_a, raw_b, c.in_s, c.out_s)
        if moved:
            self._trim_timer.start()                   # когда отпустят мышь — пересобрать (субтитры, слияния)

    def _trims_done(self) -> None:
        if getattr(self.timeline, "_mode", None):      # ещё тянут — подождём
            self._trim_timer.start()
            return
        self._recut()

    # ---------- паузы ----------

    @staticmethod
    def _gap_bounds(sw, i: int) -> tuple[float, float]:
        """Где во времени исходника пауза после слова i (−1 — перед первым словом)."""
        w = sw.words
        if i < 0:
            return 0.0, w[0].start if w else sw.duration
        return w[i].end, (w[i + 1].start if i + 1 < len(w) else sw.duration)

    def _pause_gap(self, si: int, i: int) -> float | None:
        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        if sw is None:
            return None
        cuts = tr.cuts_of(self.project)
        return dict(tr.shown_pauses(sw, cuts, tr.pause_marks(cuts, src))).get(i)

    def _set_pause(self, si: int, i: int, value: float | None) -> None:
        """Пометка паузы: None — как велит общее правило, −1 — целиком, 0 — вырезать, >0 — укоротить."""
        src = self._sources()[si]["src"]
        kept = self.project.cuts.get("kept_pauses", {})
        if i in kept.get(src, []):                               # старая пометка «оставить» — в новую
            kept[src] = [x for x in kept[src] if x != i]
        marks = self.project.cuts.setdefault("pause_marks", {}).setdefault(src, {})
        if value is None:
            marks.pop(str(i), None)
        else:
            marks[str(i)] = round(float(value), 2)

    def _apply_pause(self, si: int, i: int, value: float | None, want: str) -> None:
        """Поставить паузе состояние want ("keep" | "cut" | "short") и пересобрать."""
        gap = self._pause_gap(si, i)
        if gap is None:
            return
        cuts = tr.cuts_of(self.project)
        src = self._sources()[si]["src"]
        before = tr.pause_state(i, gap, cuts, tr.pause_marks(cuts, src))
        auto_cut = bool(cuts["pause_cut"]) and gap > float(cuts["pause_min"])
        if want == "keep":
            value = -1.0 if auto_cut else None
        elif want == "cut":
            value = None if auto_cut else 0.0
        self.history.push(self.project.to_dict())
        self._set_pause(si, i, value)
        self._recut()
        after = tr.pause_state(i, gap, cuts, tr.pause_marks(tr.cuts_of(self.project), src))
        if "short" in (before, after):
            self._tr_build(keep_scroll=True)                    # подпись паузы поменялась («2,0 → 0,5 с»)
        self._select_pause(si, i)

    def _select_pause(self, si: int, i: int) -> None:
        gap = self._pause_gap(si, i)
        self._sel_pause = (si, i) if gap is not None else None
        if gap is not None:
            cuts = tr.cuts_of(self.project)
            self.text_panel_t.show_pause(gap, tr.pause_marks(cuts, self._sources()[si]["src"]).get(i))
        else:
            self.text_panel_t.show_pause(None)

    def _on_pause_length(self, value: float) -> None:
        if self._sel_pause is None:
            return
        si, i = self._sel_pause
        if value < 0:
            self._apply_pause(si, i, None, "keep")
        else:
            self._apply_pause(si, i, value, "short")

    def _text_menu(self, key: tuple, pos) -> None:
        kind, si, i = key
        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        if sw is None:
            return
        m = QMenu(self)
        if kind == "p":
            gap = self._pause_gap(si, i)
            if gap is None:
                return
            m.addAction("Вырезать паузу", lambda: self._apply_pause(si, i, None, "cut"))
            m.addAction(f"Оставить целиком ({gap:.1f} с)".replace(".", ","),
                        lambda: self._apply_pause(si, i, None, "keep"))
            m.addSeparator()
            for v in (0.3, 0.5, 1.0, 1.5):
                if v < gap:
                    m.addAction(f"Укоротить до {v:.1f} с".replace(".", ","),
                                lambda v=v: self._apply_pause(si, i, v, "short"))
            m.addAction("Своя длина…", lambda: self._custom_pause(si, i, gap))
            a, b = self._gap_bounds(sw, i)
            if tr.broll_in_gap(tr.cuts_of(self.project), src, a, b) > 0.05:
                m.addSeparator()
                m.addAction("Убрать кадры автомонтажа из этой паузы", lambda: self._drop_broll(src, a, b))
        else:
            word = sw.words[i].text
            entry = self._respeak_entry(src, i)
            if entry is not None:
                m.addAction(f"Вернуть исходную фразу (вместо «{entry['text']}»)",
                            lambda: self._unrespeak(src, entry["id"]))
                m.addSeparator()
            gone = i in set(tr.cuts_of(self.project).get("deleted", {}).get(src, []))
            m.addAction("Вернуть слово" if gone else "Вырезать слово",
                        lambda: self._cut_tokens([key]) if not gone else self._on_text_token(key))
            clean = tr.norm_word(word)
            if clean and not any(tr.norm_word(f) == clean for f in self._fillers()):
                m.addAction(f"Добавить «{word.strip('.,!?…:;')}» в слова-паразиты",
                            lambda: self._set_fillers(self._fillers() + [word.strip(".,!?…:;").lower()]))
        m.exec(pos)

    def _drop_broll(self, src: str, a: float, b: float) -> None:
        self.history.push(self.project.to_dict())
        rng = self.project.cuts.get("broll", {}).get(src, [])
        self.project.cuts["broll"][src] = [r for r in rng if not (r[0] < b and r[1] > a)]
        self._recut()
        self._tr_build(keep_scroll=True)

    def _custom_pause(self, si: int, i: int, gap: float) -> None:
        from PySide6.QtWidgets import QInputDialog

        v, ok = QInputDialog.getDouble(self, "Длина паузы", f"Пауза {gap:.1f} с. Сколько оставить, секунд:"
                                       .replace(".", ","), min(0.5, gap), 0.0, round(gap, 1), 1)
        if ok:
            self._apply_pause(si, i, v if v > 0 else None, "short" if v > 0 else "cut")

    # ---------- слова-паразиты ----------

    def _fillers(self) -> list[str]:
        raw = QSettings("Glimpsy", "editor").value("text/fillers", "")
        try:
            got = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            got = None
        return [str(x) for x in got] if isinstance(got, list) else list(tr.DEFAULT_FILLERS)

    def _set_fillers(self, fillers: list[str]) -> None:
        QSettings("Glimpsy", "editor").setValue("text/fillers", json.dumps(fillers, ensure_ascii=False))
        self._tr_states()

    def _cut_fillers(self, words: list[str], cut: bool) -> None:
        if not words:
            return
        self.history.push(self.project.to_dict())
        for s in self._sources():
            sw = self.tstore.get(s["src"])
            if sw is None:
                continue
            for hits in tr.find_fillers(sw.words, words).values():
                for i in hits:
                    self._mark("deleted", s["src"], i, cut)
        self._recut()

    # ---------- щелчки по тексту ----------

    def _on_text_token(self, key: tuple) -> None:
        kind, si, i = key
        src = self._sources()[si]["src"]
        sw = self.tstore.get(src)
        if sw is None:
            return
        cuts = tr.cuts_of(self.project)
        if kind == "p":                                    # пауза: вырезать ⇄ оставить (только её)
            gap = self._pause_gap(si, i)
            if gap is None:
                return
            st = tr.pause_state(i, gap, cuts, tr.pause_marks(cuts, src))
            self._apply_pause(si, i, None, "cut" if st == "keep" else "keep")
            return
        self.text_panel_t.show_pause(None)
        self._sel_pause = None
        if i in set(cuts.get("deleted", {}).get(src, [])):  # зачёркнутое слово — вернуть
            self.history.push(self.project.to_dict())
            self._mark("deleted", src, i, False)
            self._recut()
            return
        self.player.pause()
        self.player.seek(self._timeline_time(src, sw.words[i].start))

    def _timeline_time(self, src: str, t: float) -> float:
        """Время внутри исходника → время на ленте (если кусок вырезан — ближайшее оставленное дальше)."""
        acc, best = 0.0, None
        for c in self.project.clips:
            if c.src == src:
                if c.in_s <= t < c.out_s:
                    return acc + (t - c.in_s) / c.speed
                if c.in_s >= t and best is None:
                    best = acc
            acc += c.duration
        return best if best is not None else self.player.t

    def _cut_tokens(self, keys: list) -> None:
        if not keys:
            return
        self.history.push(self.project.to_dict())
        cuts = tr.cuts_of(self.project)
        for kind, si, i in keys:
            src = self._sources()[si]["src"]
            if kind == "w":
                self._mark("deleted", src, i, True)
            else:                                            # выделенную паузу — вырезать
                gap = self._pause_gap(si, i) or 0.0
                auto_cut = bool(cuts["pause_cut"]) and gap > float(cuts["pause_min"])
                self._set_pause(si, i, None if auto_cut else 0.0)
        self._recut()

    def _on_cut_settings(self, values: dict) -> None:
        self.history.push(self.project.to_dict(), key="cut-settings")
        rebuild_text = float(values["pause_min"]) != float(tr.cuts_of(self.project)["pause_min"])
        self.project.cuts.update(values)
        self._recut()
        if rebuild_text:
            self._tr_build(keep_scroll=True)                 # другие паузы видны в тексте

    def transcribe(self) -> None:
        """Расшифровать все ещё не расшифрованные видео проекта (в фоне)."""
        from glimpsy.editor.subtitles_dialog import SubtitlesDialog

        if self._tr_cancel is not None:
            return
        if not subs.whisper_exe():
            QMessageBox.warning(self, "Расшифровка", "В этой сборке нет программы распознавания речи.")
            return
        todo = [s for s in self._sources() if self.tstore.get(s["src"]) is None]
        if not todo:
            return
        dlg = SubtitlesDialog(self.project.aspect, False, self, words=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        model, lang = dlg.model_key, dlg.language
        bridge = _ExportBridge(self)
        cancel = self._tr_cancel = threading.Event()
        panel = self.text_panel_t
        panel.set_progress(0.0, "Подготовка…")
        bridge.progress.connect(lambda f, t: panel.set_progress(f, t))

        def finished(msg: str) -> None:
            self._tr_cancel = None
            panel.set_progress(None)
            self.tstore = tr.TranscriptStore(self.project.dir)
            self._recut()
            self._tr_build()
            if msg:
                QMessageBox.warning(self, "Расшифровка", msg)

        bridge.done.connect(finished)
        bridge.failed.connect(finished)

        def run() -> None:
            try:
                for n, s in enumerate(todo):
                    label = s.get("label", Path(s["src"]).name)
                    prefix = f"{label} ({n + 1} из {len(todo)}): " if len(todo) > 1 else ""

                    def prog(f: float, text: str, n=n, prefix=prefix) -> None:
                        bridge.progress.emit((n + f) / len(todo), prefix + text + f" {int(f * 100)}%")

                    tr.transcribe_source(self.ffmpeg, Path(s["src"]), s["src"], float(s["duration"]), model, lang,
                                         tr.TranscriptStore(self.project.dir), prog, cancel)
                bridge.done.emit("")
            except subs.Cancelled:
                bridge.done.emit("")
            except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
                log.exception("Расшифровка не удалась")
                bridge.failed.emit(str(e))

        threading.Thread(target=run, daemon=True, name="transcribe").start()

    def _sync_preview(self) -> None:
        """Окну просмотра — кадрирование показанного фрагмента и можно ли его править."""
        c = self._shown_clip()
        if c is None:
            self.preview.set_frame(DEFAULT_FRAME, False)
        else:
            self.preview.set_frame(c.frame_for(self.project.aspect), c.id in self.timeline.selection)
        self.preview.set_src_crop(self._motion_crop(c))
        self.preview.set_ripples(self._ripples(c))
        self.preview.set_cursor(self._cursor_at(c))
        t = self.player.t
        visible = self.project.texts_at(t)
        sel = self.project.text_by_id(self.timeline.selected_text) if self.timeline.selected_text else None
        if sel is not None and not visible:
            visible = [sel]          # выбранный текст виден для правки, если он не мешает другим
        self.preview.set_texts([(x, effective_style(x, self.project.text_style)) for x in visible], t,
                               self.timeline.selected_text)
        sel_ov = self.timeline.selected_overlay
        shown = [o for o in self.project.overlays_by_depth() if o.start <= t < o.end or o.id == sel_ov]
        self.ov_video.sync(shown, t, self.player.playing, self.project.dir)
        self.preview.set_overlays([(o, self._overlay_frame(o, t)) for o in shown], sel_ov)

    def _overlay_frame(self, o, t: float):
        """Картинка для показа наложения в просмотре (для видео — кадр в нужный момент)."""
        path = self.project.dir / o.src
        if o.kind == "image":
            img = self._ov_images.get(str(path))
            if img is None:
                img = self._ov_images[str(path)] = QImage(str(path))
            return img
        live = self.ov_video.frame(o.id)
        if live is not None:
            return live
        local = max(0.0, min(o.duration, t - o.start))          # плеер ещё открывает файл — пока миниатюра
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

    def _pick_region(self, clip_id: str) -> None:
        """«Зум на область»: обвести область мышью в просмотре."""
        idx = self.project.index_of(clip_id)
        if idx < 0:
            return
        self.player.pause()
        start = self.project.start_of(idx)
        if not start <= self.player.t < start + self.project.clips[idx].duration:
            self.player.seek(start + min(0.2, self.project.clips[idx].duration / 2))
        self._region_target = clip_id
        self.preview.start_region_pick()
        self.statusBar().showMessage("Обведите мышью в просмотре область, к которой приблизить камеру (Esc — отмена)")

    def _on_region_picked(self, region) -> None:
        cid = getattr(self, "_region_target", None)
        self._region_target = None
        self.statusBar().clearMessage()
        targets = [c for c in (self._selected_clips() or []) if c.kind == "video"]
        if cid and all(c.id != cid for c in targets):
            targets = [c for c in self.project.clips if c.id == cid]
        if region is None or not targets:
            self._sync_preview()
            return
        self.history.push(self.project.to_dict())
        for c in targets:
            c.region = list(region)
            c.set_motion(self.project.aspect, "region")
        self._changed()

    def _cursor_at(self, c: Clip | None):
        """Свой курсор в просмотре: (x, y, стиль, размер) или None."""
        style, size, show = self.project.cursor_style()
        if c is None or not (c.own_cursor and show and c.cursor and c.kind == "video"):
            return None
        idx, local = self.project.locate(self.player.t)
        if idx is None or self.project.clips[idx].id != c.id:
            return None
        from glimpsy.editor import cursor as cur

        key = ("cursor", c.id, len(c.cursor), c.src_duration)
        track = self._motion_cache.get(key)
        if track is None:
            track = self._motion_cache[key] = cur.smooth_track(c.cursor, c.src_duration)
        pos = cur.position_at(track, c.in_s + local * c.speed)
        return (pos[0], pos[1], style, size) if pos else None

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
               c.in_s, c.out_s, tuple(c.region))
        track = self._motion_cache.get(key)
        if track is None:
            track = motion.track_for(mode, c.cursor, c.clicks, c.src_duration, c.zoom_strength,
                                     c.in_s, c.out_s, c.width, c.height, c.region)
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
                                max(1, len(self.timeline.selection)), self.project.cursor_style())

    def _on_select(self, _clip_id) -> None:
        self._refresh_inspector()
        self._sync_preview()
        self._update_actions()

    def _changed(self) -> None:
        """После любой правки: перерисовать, обновить просмотр, автосохранение."""
        if self.project.text_edit:
            self._capture_trims()
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
        self.subs_panel.refresh(self.project, self.timeline.selected_text)
        if self.project.text_edit:
            self._tr_states()
        self._save_timer.start()

    def _save(self) -> None:
        self.history.settle(self.project.to_dict())
        self._update_actions()
        try:
            self.project.save()
        except OSError:
            log.exception("Не удалось сохранить проект")

    def _on_inspector(self, clip_id: str, what: str, value) -> None:
        if what == "delete":
            self.delete_selected()
            return
        if what == "region_pick":
            self._pick_region(clip_id)
            return
        if what in ("cursor_style", "cursor_size", "cursor_show"):
            # вид курсора — один на весь ролик
            self.history.push(self.project.to_dict(), key=what)
            self.project.cursor[what.split("_", 1)[1]] = value
            self._changed()
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
            self._frames_to_sources(self.project.clips)
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
                if value == "region" and not c.region and c.id == clip_id:
                    QTimer.singleShot(0, lambda cid=c.id: self._pick_region(cid))   # сначала — выбрать область
            elif what == "zoom_strength" and c.kind == "video":
                c.zoom_strength = float(value)
            elif what == "click_fx" and c.kind == "video":
                c.click_fx = bool(value)
            elif what == "frame_fit":
                c.set_frame(aspect, *DEFAULT_FRAME)
            elif what == "frame_fill":
                c.set_frame(aspect, cover_zoom(c.width or W, c.height or H, W, H), 0.0, 0.0)
        if what.startswith("frame"):
            self._frames_to_sources(targets)
        self._changed()

    def _frames_to_sources(self, clips: list) -> None:
        """Монтаж по тексту: кадрирование — у всего видео (куски пересобираются после каждого выреза)."""
        if not self.project.text_edit:
            return
        for c in clips:
            for s in self._sources():
                if s["src"] == c.src:
                    s["frames"] = {k: list(v) for k, v in c.frames.items()}
            for other in self.project.clips:
                if other.src == c.src and other is not c:
                    other.frames = {k: list(v) for k, v in c.frames.items()}

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
        self._frames_to_sources(self._selected_clips())
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
        self._frames_to_sources(targets)
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
        if self.project.text_edit:
            self._tr_build(keep_scroll=True)          # режим, порог и подписи пауз — как в восстановленном
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
        sel = self.project.text_by_id(self.timeline.selected_text) if self.timeline.selected_text else None
        tr = self.project.track_by_id(sel.track) if sel is not None else None
        item.track = tr.id if tr is not None and tr.kind == "text" else self.project.track_for("text").id
        self.history.push(self.project.to_dict())
        self.project.texts.append(item)
        self.timeline.select_text(item.id)
        self._text_changed()
        self.text_panel.focus_text()

    def add_subtitle(self) -> None:
        """Новый субтитр в месте курсора, на дорожку «Субтитры»."""
        self.player.pause()
        total = self.project.total
        start = min(self.player.t, max(0.0, total - 0.5))
        item = TextItem(new_id(), "Субтитр", round(start, 2), round(min(2.0, max(0.5, total - start)), 2),
                        auto=True, track=self.project.track_for("subtitles").id)
        self.history.push(self.project.to_dict())
        self.project.texts.append(item)
        self.timeline.select_text(item.id)
        self._text_changed()
        self.text_panel.focus_text()

    def _delete_texts(self, ids: list) -> None:
        if not ids:
            return
        self.history.push(self.project.to_dict())
        self.project.texts = [t for t in self.project.texts if t.id not in set(ids)]
        self.timeline.select_text(None)
        self._text_changed()

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
        self.subs_panel.show_current(text_id)
        self._sync_preview()
        self._update_actions()

    def _text_changed(self) -> None:
        """Правка текста — без перемотки видео, только перерисовка и сохранение."""
        self.subs_panel.refresh(self.project, self.timeline.selected_text)
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
        track = self.project.track_for("subtitles").id
        for it in items:
            it.track = track
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
            self.add_overlays(files, self.player.t, "overlay")

    def add_overlays(self, files: list, start: float, track: str = "") -> None:
        """Картинки и видео поверх ролика — на дорожку track (id или род: overlay / media / camera).
        Если такой дорожки ещё нет, она появляется; есть — файл добавляется в неё."""
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
                items.append(o)
        finally:
            QApplication.restoreOverrideCursor()
        if items:
            self.history.push(self.project.to_dict())
            tr = self.project.track_by_id(track) if track else None
            if tr is None or tr.kind in ("subtitles", "text"):
                tr = self.project.track_for(track if track in ("media", "camera") else "overlay")
            for o in items:
                o.track = tr.id
                if tr.kind == "media":
                    # медиа — на весь кадр (вписано), в обоих форматах
                    for aspect, (fw, fh) in ASPECTS.items():
                        ar = (o.width / o.height) if o.width and o.height else 16 / 9
                        o.set_layout(aspect, 0.5, 0.5, min(1.0, fh * ar / fw))
                else:
                    o.set_layout(self.project.aspect, 0.5, 0.5, 0.45)
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
            self.add_overlays(files, self.player.t, "media")

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
        if self.project.text_edit:
            self.vlog_montage()                 # свои видео (влог) — монтаж по речи и красивым кадрам
            return
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

    def vlog_montage(self) -> None:
        """Автомонтаж влога: речь — по расшифровке, кадры без речи — по картинке. Разные форматы."""
        from glimpsy.editor import vlog
        from glimpsy.editor.vlog_dialog import VlogDialog

        self.player.pause()
        missing = [s for s in self._sources() if self.tstore.get(s["src"]) is None]
        if missing:
            ask = QMessageBox.question(
                self, "Автомонтаж влога",
                "Речь в видео ещё не расшифрована — автомонтаж не поймёт, где вы говорите, и соберёт ролик "
                "только из красивых кадров.\n\nСначала расшифровать речь (кнопка в панели «Текст»)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if ask == QMessageBox.StandardButton.Yes:
                self.transcribe()
                return
            if ask != QMessageBox.StandardButton.No:
                return
        dlg = VlogDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        fmt, target, subs_on, separate = dlg.format, dlg.target_s, dlg.subs.isChecked(), dlg.separate.isChecked()
        sources = [dict(s) for s in self._sources()]
        store = tr.TranscriptStore(self.project.dir)
        cuts = tr.cuts_of(self.project)
        prog = QProgressDialog("Смотрю видео…", "Отмена", 0, 1000, self)
        prog.setWindowTitle("Автомонтаж влога")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        bridge = _ExportBridge(self)
        cancel = threading.Event()
        prog.canceled.connect(cancel.set)
        bridge.progress.connect(lambda f, t: (prog.setValue(int(f * 1000)), prog.setLabelText(t)))
        result: dict = {}

        def done(_m: str) -> None:
            prog.close()
            self._apply_vlog(result["plan"], fmt, subs_on, separate)

        def failed(msg: str) -> None:
            prog.close()
            if msg:
                QMessageBox.warning(self, "Автомонтаж влога", msg)

        bridge.done.connect(done)
        bridge.failed.connect(failed)

        def run() -> None:
            try:
                visuals = {}
                for n, s in enumerate(sources):
                    cache = store.dir / f"{tr.source_key(s['src'])}.vis.npz"
                    label = s.get("label", Path(s["src"]).name)
                    if cache.exists():
                        visuals[s["src"]] = vlog.Visual.load(cache)
                        continue
                    visuals[s["src"]] = vis = vlog.analyze_visual(
                        self.ffmpeg, Path(s["src"]), float(s["duration"]),
                        lambda f, n=n, label=label: bridge.progress.emit(
                            (n + f) / len(sources), f"Смотрю видео «{label}»… {int(f * 100)}%"), cancel)
                    store.dir.mkdir(parents=True, exist_ok=True)
                    vis.save(cache)
                bridge.progress.emit(1.0, "Собираю ролик…")
                result["plan"] = vlog.plan(
                    fmt, sources, lambda src: (sw.words if (sw := store.get(src)) else None), store.envelope,
                    visuals.get, lambda src: set(cuts.get("deleted", {}).get(src, [])), target)
                bridge.done.emit("")
            except Exception as e:                      # noqa: BLE001 — показываем человеку, что пошло не так
                if cancel.is_set():
                    bridge.failed.emit("")
                    return
                log.exception("Автомонтаж влога не удался")
                bridge.failed.emit(str(e))

        threading.Thread(target=run, daemon=True, name="vlog").start()
        prog.show()

    def _apply_vlog(self, plan, fmt: str, subs_on: bool, separate: bool) -> None:
        from glimpsy.editor import vlog

        if plan.length < 1.0:
            QMessageBox.information(self, "Автомонтаж влога", "Не нашлось, что оставить: в видео нет ни речи, "
                                    "ни подходящих кадров.")
            return
        if separate and self.on_open is not None:
            self._save()
            target = self._variant_project(fmt)
        else:
            self.history.push(self.project.to_dict())
            target = self.project
        vlog_apply(target, plan, fmt, subs_on)
        label = vlog.FORMATS[fmt][0].split(" — ")[0]
        msg = (f"{label}: {fmt_time(plan.length)} — речь {fmt_time(plan.talk)}, кадров без речи: {plan.shots}.")
        if target is self.project:
            self._recut()
            self.preview.set_aspect(self.project.aspect)
            for b in self.aspect_group.buttons():
                b.setChecked(b.property("aspect") == self.project.aspect)
            self._tr_build(keep_scroll=True)
            self.statusBar().showMessage(msg + " Отменить — Ctrl+Z", 12000)
        else:
            tr.rebuild_clips(target, tr.TranscriptStore(target.dir))
            target.save()
            self.statusBar().showMessage(msg + " Открыт отдельным проектом.", 12000)
            self.on_open(target.dir)

    def _variant_project(self, fmt: str) -> Project:
        """Копия проекта (расшифровка — тоже) для варианта автомонтажа; исходники не копируются."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        d = self.project.dir.with_name(f"{self.project.dir.name}_{fmt}_{stamp}")
        d.mkdir(parents=True)
        shutil.copy2(self.project.dir / "edit.json", d / "edit.json")
        if (self.project.dir / "transcript").exists():
            shutil.copytree(self.project.dir / "transcript", d / "transcript",
                            ignore=shutil.ignore_patterns("work"))
        if (self.project.dir / "voice").exists():
            shutil.copytree(self.project.dir / "voice", d / "voice")
        p = Project.load(d)
        p.name = f"{self.project.name} — {'короткий' if fmt == 'short' else 'влог'}"
        p.created = time.time()
        return p

    def before_after(self) -> None:
        from glimpsy.editor import before_after as ba
        from glimpsy.editor.before_after_dialog import BeforeAfterDialog

        if not any(c.kind == "video" for c in self.project.clips):
            QMessageBox.information(self, "Было → стало", "В проекте нет видеофрагментов.")
            return
        dlg = BeforeAfterDialog(self)
        if not dlg.exec():
            return
        self.player.pause()
        place = dlg.place.currentData()
        prog = QProgressDialog("Рисую вставку…", "Отмена", 0, 1000, self)
        prog.setWindowTitle("Было → стало")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(300)

        def progress(f: float) -> bool:
            prog.setValue(int(f * 1000))
            QApplication.processEvents()
            return not prog.wasCanceled()

        try:
            clip = ba.make_clip(self.ffmpeg, self.project, dlg.mode, dlg.seconds.value(), progress)
        except ba.BeforeAfterError as e:
            prog.close()
            if str(e):
                QMessageBox.warning(self, "Было → стало", str(e))
            return
        prog.close()
        if place == "start":
            index = 0
        elif place == "here":
            idx, local = self.project.locate(self.player.t)
            index = 0 if idx is None else idx + (1 if local > self.project.clips[idx].duration / 2 else 0)
        else:
            index = len(self.project.clips)
        self.history.push(self.project.to_dict())
        self.project.insert(index, [clip])
        self.timeline.select(clip.id)
        self._changed()
        self.player.seek(self.project.start_of(index))

    # ---------- запись голоса и камеры ----------

    record_mic_opener = None          # для проверок: «микрофон» и «камера» без устройств
    record_camera_input: list[str] | None = None

    _rec_dialog = None
    _rec_start = 0.0
    _rec_volume: float | None = None

    def record(self) -> None:
        """Окно записи — поверх редактора, но не мешает им пользоваться."""
        from glimpsy.editor.record_dialog import RecordDialog

        if self._rec_dialog is not None:
            self._rec_dialog.show()
            self._rec_dialog.raise_()
            return
        dlg = RecordDialog(self.ffmpeg, self.project.dir / "media", self, self.record_mic_opener,
                           self.record_camera_input)
        dlg.started.connect(self._on_record_started)
        dlg.recorded.connect(self._on_recorded)
        dlg.cancelled.connect(self._on_record_ended)
        dlg.destroyed.connect(lambda: setattr(self, "_rec_dialog", None))
        self._rec_dialog = dlg
        dlg.show()

    def _on_record_started(self) -> None:
        dlg = self._rec_dialog
        self._rec_start = self.player.t          # голос ляжет на ленту с этого места
        if dlg is not None and dlg.mute.isChecked():
            self._rec_volume = self.player.volume
            self.player.set_volume(0.0)
        if dlg is not None and dlg.play_along.isChecked() and not self.player.playing:
            self.player.play()

    def _on_record_ended(self) -> None:
        if self.player.playing:
            self.player.pause()
        if self._rec_volume is not None:
            self.player.set_volume(self._rec_volume)
            self._rec_volume = None
        self._rec_dialog = None

    def _on_recorded(self, rec) -> None:
        self._on_record_ended()
        self.add_recording(rec, self._rec_start)

    def add_recording(self, rec, start: float) -> None:
        """Записанное — на дорожки «Голос» и «Камера», с места start."""
        from glimpsy.editor.overlay import camera_item

        stamp = time.strftime("%H:%M")
        self.history.push(self.project.to_dict())
        last = None
        if rec.voice is not None:
            v = OverlayItem(new_id(), "audio", str(rec.voice.relative_to(self.project.dir)), round(start, 2),
                            round(rec.voice_s, 2), src_duration=rec.voice_s, has_audio=True,
                            label=f"Голос {stamp}", track=self.project.track_for("voice").id)
            self.project.overlays.append(v)
            last = v
        if rec.camera is not None and rec.camera_path is not None:
            c = rec.camera
            cam = camera_item(new_id(), str(rec.camera_path.relative_to(self.project.dir)),
                              start + rec.camera_offset, c.duration, c.width, c.height, f"Камера {stamp}")
            cam.track = self.project.track_for("camera").id
            self.project.overlays.append(cam)
            last = cam
        if last is not None:
            self.timeline.select_overlay(last.id)
        self._layer_changed()

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

    def _release_typing(self) -> None:
        """Перестать печатать: фокус уходит из поля — буквы снова работают как горячие клавиши."""
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)):
            focus.clearFocus()
            self.timeline.setFocus(Qt.FocusReason.MouseFocusReason)

    def jump_cut(self, direction: int) -> None:
        """Ctrl+← / Ctrl+→: к предыдущей / следующей склейке (началу фрагмента)."""
        cuts, t = [0.0], 0.0
        for c in self.project.clips:
            t += c.duration
            cuts.append(t)
        now, eps = self.player.t, 1e-3
        target = (next((x for x in cuts if x > now + eps), None) if direction > 0
                  else next((x for x in reversed(cuts) if x < now - eps), None))
        if target is None:
            return
        self.player.pause()
        self.player.seek(target)
        idx = cuts.index(target)
        if idx < len(self.project.clips):
            self.timeline.select(self.project.clips[idx].id)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget) and obj.window() is self:
            # щелчок мимо поля ввода — перестаём печатать (сам щелчок работает как обычно)
            focus = QApplication.focusWidget()
            if isinstance(focus, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)) and \
                    obj is not focus and not focus.isAncestorOf(obj):
                self._release_typing()
            return False
        # клавиши — только те, что пришли в это окно (открыто несколько редакторов или диалог — не наши)
        if ev.type() != QEvent.Type.KeyPress or not (isinstance(obj, QWidget) and obj.window() is self):
            return False
        assert isinstance(ev, QKeyEvent)
        focus = QApplication.focusWidget()
        typing = isinstance(focus, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit))
        if typing and ev.key() == Qt.Key.Key_Escape:
            self._release_typing()
            return True
        if self.preview._pick and ev.key() == Qt.Key.Key_Escape:
            self.preview._end_pick(None)           # отмена выбора области
            return True
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
            if ev.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and not typing:
                self.jump_cut(1 if ev.key() == Qt.Key.Key_Right else -1)
                return True
            return False
        # в числовых полях буквы не вводятся — там буквенные клавиши работают как обычно
        text_entry = isinstance(focus, (QLineEdit, QPlainTextEdit, QTextEdit))
        if not text_entry and letter and \
                not ev.modifiers() & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            action = {"z": self.toggle_autozoom, "c": self.toggle_click_fx, "t": self.add_text,
                      "f": lambda: self._frame_key("frame_fit"), "g": lambda: self._frame_key("frame_fill"),
                      "m": lambda: self.toggle_left("media"), "s": self.split}.get(letter or "")
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

    def export(self, aspects: list[str] | None = None) -> None:
        """Экспорт в текущем формате или сразу в нескольких (16:9 и 9:16 — два файла)."""
        self.player.pause()
        self._save()
        aspects = aspects or [self.project.aspect]
        dlg = QProgressDialog("Подготовка…", "Отмена", 0, 1000, self)
        dlg.setWindowTitle("Экспорт ролика" if len(aspects) == 1 else "Экспорт: 16:9 и 9:16")
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
            files = path.split("\n")
            box = QMessageBox(self)
            box.setWindowTitle("Готово")
            box.setText(("Ролик сохранён:\n" if len(files) == 1 else "Ролики сохранены:\n") + "\n".join(files))
            b_open = box.addButton("Открыть папку", QMessageBox.ButtonRole.ActionRole)
            box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() == b_open:
                paths.open_in_file_manager(Path(files[0]).parent)

        def failed(msg: str) -> None:
            dlg.close()
            if msg:
                QMessageBox.warning(self, "Экспорт не удался", msg)

        bridge.done.connect(finished)
        bridge.failed.connect(failed)
        # тексты и наложения рисуются в основном потоке (так надёжнее для шрифтов), дальше — FFmpeg в фоне
        layers_dir = paths.temp_root() / f"text_{int(time.time())}"
        jobs = []
        for aspect in aspects:
            snapshot = Project(self.project.dir, self.project.name, fps=self.project.fps,
                               source_video=self.project.source_video)
            snapshot.restore(self.project.to_dict())   # копия — можно продолжать править во время экспорта
            snapshot.aspect = aspect
            sub = layers_dir / aspect.replace(":", "x")
            jobs.append((snapshot, default_output(snapshot, self.fallback_output),
                         render_text_layers(snapshot, sub), render_overlay_layers(snapshot, sub)))

        def work() -> None:
            done = []
            try:
                enc = self.encoder_getter()
                for n, (snapshot, out, text_layers, overlay_layers) in enumerate(jobs):
                    prefix = f"{snapshot.aspect} ({n + 1} из {len(jobs)}): " if len(jobs) > 1 else ""

                    def prog(f: float, t: str, n=n, prefix=prefix) -> None:
                        bridge.progress.emit((n + f) / len(jobs), prefix + t)

                    path = export_project(self.ffmpeg, snapshot, out, enc, text_layers=text_layers,
                                          overlay_layers=overlay_layers, progress=prog, cancel=cancel)
                    done.append(str(path))
                    if snapshot.text_edit:
                        # субтитры с таймкодами уже готового ролика — рядом с ним
                        words = tr.output_words(snapshot, tr.TranscriptStore(snapshot.dir))
                        if words:
                            srt = Path(path).with_suffix(".srt")
                            srt.write_text(tr.make_srt(words), encoding="utf-8")
                            done.append(str(srt))
                            lang = snapshot.cuts.get("subs_lang") or ""
                            if lang:                    # и перевод — name.de.srt
                                lines, _missing = mt.translated_cues(words, mt.Cache(snapshot.dir),
                                                                     snapshot.cuts.get("subs_mt", "m2m"), lang)
                                srt2 = Path(path).with_suffix(f".{lang}.srt")
                                srt2.write_text(tr.srt_text(lines), encoding="utf-8")
                                done.append(str(srt2))
                bridge.done.emit("\n".join(done))
            except ExportCancelled:
                bridge.failed.emit("")
            except Exception as e:
                log.exception("Экспорт не удался")
                bridge.failed.emit(str(e) + (f"\n\nУже сохранено: {', '.join(done)}" if done else ""))
            finally:
                shutil.rmtree(layers_dir, ignore_errors=True)

        threading.Thread(target=work, daemon=True, name="export").start()
        dlg.show()

    def back_to_sessions(self) -> None:
        """«Все записи»: сохранить, закрыть редактор и показать список сессий."""
        self._save()
        if self.close() and self.on_sessions is not None:
            self.on_sessions()

    def closeEvent(self, e) -> None:
        self._save()
        from glimpsy.editor import voice
        voice.speaker().stop()
        self.ov_video.shutdown()
        QApplication.instance().removeEventFilter(self)
        self.player.shutdown()
        super().closeEvent(e)
