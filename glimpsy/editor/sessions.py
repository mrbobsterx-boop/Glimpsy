"""Список всех записанных сессий — выбираете, какую открыть в редакторе."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from glimpsy import paths
from glimpsy.editor.media import Thumbnailer
from glimpsy.editor.project import Project, list_projects
from glimpsy.editor.timeline import fmt_time

log = logging.getLogger(__name__)

THUMB_H = 72


def projects_root() -> Path:
    return paths.data_dir() / "projects"


class SessionsDialog(QDialog):
    def __init__(self, ffmpeg: str, open_project: Callable[[Path], None]) -> None:
        super().__init__()
        self.setWindowTitle("Glimpsy — мои сессии")
        self.resize(640, 560)
        self.open_project = open_project
        self.thumbs = Thumbnailer(ffmpeg)
        self.thumbs.ready.connect(self._refresh_icons)
        self._first_frames: dict[int, tuple[Path, float, bool]] = {}

        self.list = QListWidget()
        self.list.setIconSize(QSize(THUMB_H * 16 // 9, THUMB_H))
        self.list.setSpacing(4)
        self.list.itemDoubleClicked.connect(lambda _: self._open())
        self.list.currentRowChanged.connect(lambda _: self._update_buttons())

        self.b_open = QPushButton("Открыть в редакторе")
        self.b_open.setDefault(True)
        self.b_open.clicked.connect(self._open)
        self.b_folder = QPushButton("Показать файлы")
        self.b_folder.clicked.connect(self._show_folder)
        self.b_delete = QPushButton("Удалить…")
        self.b_delete.clicked.connect(self._delete)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        for b in (self.b_open, self.b_folder, self.b_delete):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)

        hint = QLabel("Каждая сессия записи — отдельный проект. Правки сохраняются автоматически, "
                      "исходный ролик не меняется.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b8d98;")
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.list, 1)
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
            info = f"{len(p.clips)} фрагм. · {fmt_time(p.total)}"
            if p.edited:
                info += " · изменён"
            item = QListWidgetItem(f"{when}\n{info}")
            item.setData(Qt.ItemDataRole.UserRole, str(d))
            item.setSizeHint(QSize(0, THUMB_H + 12))
            self.list.addItem(item)
            if p.clips:
                c = p.clips[0]
                self._first_frames[self.list.count() - 1] = (p.path_of(c), c.in_s, c.kind == "image")
        if self.list.count() == 0:
            item = QListWidgetItem("Пока нет ни одной сессии. Запишите и соберите ролик — "
                                   "он появится здесь.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        else:
            self.list.setCurrentRow(0)
        self._refresh_icons()
        self._update_buttons()

    def _refresh_icons(self) -> None:
        for row, (path, t, is_image) in self._first_frames.items():
            img = self.thumbs.get(path, t, THUMB_H, is_image)
            item = self.list.item(row)
            if img is not None and item is not None and item.icon().isNull():
                item.setIcon(QIcon(QPixmap.fromImage(img)))

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

    def _delete(self) -> None:
        d = self._current_dir()
        if not d:
            return
        ans = QMessageBox.question(self, "Удалить сессию?",
                                   "Удалить фрагменты и правки этой сессии?\n\n"
                                   "Уже сохранённые ролики в папке «Видео» останутся.")
        if ans == QMessageBox.StandardButton.Yes:
            shutil.rmtree(d, ignore_errors=True)
            self.reload()
