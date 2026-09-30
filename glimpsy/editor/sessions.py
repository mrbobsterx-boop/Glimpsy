"""Список всех записанных сессий — выбираете, какую открыть в редакторе."""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStyle,
    QStyledItemDelegate, QVBoxLayout,
)

from glimpsy import paths
from glimpsy.editor.media import Thumbnailer
from glimpsy.editor.project import Project, list_projects
from glimpsy.editor.timeline import fmt_time
from glimpsy.ui import theme
from glimpsy.ui.icons import app_logo

log = logging.getLogger(__name__)

THUMB_H = 126
CARD_W, CARD_H = 240, 188


class _CardDelegate(QStyledItemDelegate):
    """Карточка сессии: превью, дата, сведения."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(CARD_W, CARD_H)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(6, 6, -6, -6)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.setPen(QPen(QColor(theme.ACCENT if selected else theme.BORDER_STRONG if hover else theme.BORDER),
                      2 if selected else 1))
        p.setBrush(QColor(theme.HOVER if hover and not selected else theme.RAISED))
        p.drawRoundedRect(r, 12, 12)
        thumb = QRectF(r.left() + 8, r.top() + 8, r.width() - 16, THUMB_H - 16)
        path = QPainterPath()
        path.addRoundedRect(thumb, 8, 8)
        p.setClipPath(path)
        p.fillRect(thumb, QColor(theme.BG))
        pm = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pm, QPixmap) and not pm.isNull():
            scaled = pm.scaled(thumb.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(thumb.topLeft(), scaled, QRectF(0, 0, thumb.width(), thumb.height()))
        badge = index.data(Qt.ItemDataRole.UserRole + 3)
        if badge:
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(badge) + 14
            b = QRectF(thumb.right() - w - 6, thumb.bottom() - 22, w, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawRoundedRect(b, 8, 8)
            p.setPen(QColor("#FFFFFF"))
            f = QFont(p.font())
            f.setPixelSize(10)
            p.setFont(f)
            p.drawText(b, Qt.AlignmentFlag.AlignCenter, badge)
        p.setClipping(False)
        title = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        meta = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        f = QFont(p.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(r.left() + 12, thumb.bottom() + 10, r.width() - 24, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(r.left() + 12, thumb.bottom() + 30, r.width() - 24, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, meta)
        p.restore()


def projects_root() -> Path:
    return paths.data_dir() / "projects"


class SessionsDialog(QDialog):
    def __init__(self, ffmpeg: str, open_project: Callable[[Path], None]) -> None:
        super().__init__()
        self.setWindowTitle("Glimpsy — мои сессии")
        self.resize(820, 620)
        self.open_project = open_project
        self.thumbs = Thumbnailer(ffmpeg)
        self.thumbs.ready.connect(self._refresh_icons)
        self._first_frames: dict[int, tuple[Path, float, bool]] = {}

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setGridSize(QSize(CARD_W, CARD_H))
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(_CardDelegate(self.list))
        self.list.setStyleSheet("QListWidget::item, QListWidget::item:selected, QListWidget::item:hover "
                                "{ background: transparent; border: none; }")
        self.list.itemDoubleClicked.connect(lambda _: self._open())
        self.list.currentRowChanged.connect(lambda _: self._update_buttons())
        self.empty = theme.mark(QLabel("Пока нет ни одной сессии.\nЗапишите и соберите ролик — он появится здесь.\n"
                                       "Или смонтируйте своё видео — кнопка «Смонтировать видео…» внизу."), "muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.b_open = theme.mark(QPushButton(theme.icon("film", "#FFFFFF", 16), "  Открыть в редакторе"), "primary")
        self.b_open.setDefault(True)
        self.b_open.clicked.connect(self._open)
        self.b_folder = QPushButton(theme.icon("folder-open", size=16), "  Файлы")
        self.b_folder.clicked.connect(self._show_folder)
        self.b_video = QPushButton(theme.icon("film", size=16), "  Смонтировать видео…")
        self.b_video.setToolTip("Своё видео (можно несколько, любой длины): расшифровка речи, монтаж по тексту, "
                                "вырезание пауз. Исходные файлы не меняются и не копируются.")
        self.b_video.clicked.connect(self._new_from_videos)
        self.ffmpeg = ffmpeg
        self.b_delete = theme.mark(QPushButton(theme.icon("trash-2", theme.DANGER, 16), "  Удалить…"), "danger")
        self.b_delete.clicked.connect(self._delete)
        close = theme.mark(QPushButton("Закрыть"), "ghost")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.b_delete)
        buttons.addWidget(self.b_folder)
        buttons.addWidget(self.b_video)
        buttons.addStretch(1)
        buttons.addWidget(close)
        buttons.addWidget(self.b_open)

        logo = QLabel()
        logo.setPixmap(app_logo(40))
        title = theme.mark(QLabel("Мои сессии"), "h1")
        sub = theme.mark(QLabel("Каждая запись — отдельный проект. Правки сохраняются сами, исходный ролик "
                                "не меняется."), "muted")
        sub.setWordWrap(True)
        head_text = QVBoxLayout()
        head_text.setSpacing(2)
        head_text.addWidget(title)
        head_text.addWidget(sub)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(logo, alignment=Qt.AlignmentFlag.AlignTop)
        head.addLayout(head_text, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addLayout(head)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.empty, 1)
        lay.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        self._first_frames.clear()
        for d in list_projects(projects_root()):
            try:
                p = Project.load(d)
            except Exception:
                log.exception("Проект %s не читается", d)
                continue
            when = time.strftime("%d.%m.%Y, %H:%M", time.localtime(p.created or d.stat().st_mtime))
            stream = _stream_name(d)
            if stream:                      # запись отдельного окна (поток)
                when += f" · {stream}"
            info = f"{len(p.clips)} фрагм. · {fmt_time(p.total)}"
            if p.edited:
                info += " · изменён"
            if p.text_edit:
                n = len(p.cuts.get("sources", []))
                when += " · видео" + (f" ({n})" if n > 1 else "")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(d))
            item.setData(Qt.ItemDataRole.UserRole + 1, when)
            item.setData(Qt.ItemDataRole.UserRole + 2, f"{len(p.clips)} фрагм." + (" · изменён" if p.edited else ""))
            item.setData(Qt.ItemDataRole.UserRole + 3, fmt_time(p.total))
            self.list.addItem(item)
            if p.clips:
                c = p.clips[0]
                self._first_frames[self.list.count() - 1] = (p.path_of(c), c.in_s, c.kind == "image")
        empty = self.list.count() == 0
        self.list.setVisible(not empty)
        self.empty.setVisible(empty)
        if not empty:
            self.list.setCurrentRow(0)
        self._refresh_icons()
        self._update_buttons()

    def _refresh_icons(self) -> None:
        for row, (path, t, is_image) in self._first_frames.items():
            img = self.thumbs.get(path, t, THUMB_H, is_image)
            item = self.list.item(row)
            if img is not None and item is not None and item.data(Qt.ItemDataRole.DecorationRole) is None:
                item.setData(Qt.ItemDataRole.DecorationRole, QPixmap.fromImage(img))

    def _current_dir(self) -> Path | None:
        item = self.list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        return Path(value) if value else None

    def _update_buttons(self) -> None:
        ok = self._current_dir() is not None
        for b in (self.b_open, self.b_folder, self.b_delete):
            b.setEnabled(ok)

    def _open(self) -> None:
        d = self._current_dir()
        if d:
            self.open_project(d)

    def _show_folder(self) -> None:
        d = self._current_dir()
        if d:
            paths.open_in_file_manager(d)

    def _new_from_videos(self) -> None:
        """Новый проект «монтаж по тексту» из своих видео."""
        from PySide6.QtWidgets import QApplication, QFileDialog

        from glimpsy.editor.media import VIDEO_EXT, MediaError, probe

        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Видео для монтажа", str(Path.home()),
                                                f"Видео ({exts});;Все файлы (*)")
        if not files:
            return
        infos, bad = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for f in files:
                try:
                    info = probe(self.ffmpeg, Path(f))
                    if info.is_image or info.duration <= 0:
                        raise MediaError("это не видео")
                    infos.append((Path(f), info))
                except (MediaError, OSError) as e:
                    bad.append(f"{Path(f).name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        if bad:
            QMessageBox.warning(self, "Glimpsy", "Не удалось открыть:\n\n" + "\n".join(bad))
        if not infos:
            return
        p = Project.for_videos(projects_root(), [f for f, _ in infos], [i for _, i in infos])
        self.reload()
        self.open_project(p.dir)

    def _delete(self) -> None:
        d = self._current_dir()
        if not d:
            return
        video = False
        try:
            video = Project.load(d).text_edit
        except Exception:
            log.debug("Проект %s не читается", d, exc_info=True)
        ans = QMessageBox.question(self, "Удалить сессию?",
                                   "Удалить правки и расшифровку этого проекта?\n\nСами видео не удаляются."
                                   if video else
                                   "Удалить фрагменты и правки этой сессии?\n\n"
                                   "Уже сохранённые ролики в папке «Видео» останутся.")
        if ans == QMessageBox.StandardButton.Yes:
            shutil.rmtree(d, ignore_errors=True)
            self.reload()


def _stream_name(d: Path) -> str:
    try:
        return str(json.loads((d / "project.json").read_text(encoding="utf-8")).get("stream", ""))
    except (OSError, ValueError):
        return ""
