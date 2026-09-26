"""Раскрывающаяся слева библиотека файлов с компьютера: видео, фото и музыка под рукой.

Папки «Видео», «Изображения», «Музыка», «Загрузки», готовые ролики Glimpsy — в одном списке,
новые файлы сверху. Двойной щелчок или кнопка «На ленту» — файл встаёт после выбранного
фрагмента; можно и перетащить мышкой на ленту (на дорожку наложений — поверх ролика).
Музыка становится фоновой музыкой.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QMimeData, QSettings, QSize, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from glimpsy.editor.media import IMAGE_EXT, VIDEO_EXT, Thumbnailer
from glimpsy.editor.music import AUDIO_EXT
from glimpsy.ui import theme

log = logging.getLogger(__name__)

KINDS = {"all": "Все файлы", "video": "Видео", "image": "Фото", "audio": "Музыка"}
MAX_FILES = 500             # в огромной папке показываем самые новые
MAX_THUMBS = 150            # миниатюры — только для самых новых, остальным хватит значка
THUMB_W, THUMB_H = 64, 36
PATH_ROLE = Qt.ItemDataRole.UserRole
DIR_ROLE = Qt.ItemDataRole.UserRole + 1


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return ""


def places(output_dir: Path | None) -> list[tuple[str, Path]]:
    """Быстрые места: готовые ролики, стандартные папки системы, домашняя папка."""
    std = QStandardPaths.StandardLocation
    out: list[tuple[str, Path]] = []
    if output_dir is not None:
        out.append(("Ролики Glimpsy", output_dir))
    for label, loc in (("Видео", std.MoviesLocation), ("Изображения", std.PicturesLocation),
                       ("Музыка", std.MusicLocation), ("Загрузки", std.DownloadLocation),
                       ("Рабочий стол", std.DesktopLocation), ("Домашняя папка", std.HomeLocation)):
        p = QStandardPaths.writableLocation(loc)
        if p:
            out.append((label, Path(p)))
    seen, uniq = set(), []
    for label, p in out:
        if p not in seen and p.is_dir():
            seen.add(p)
            uniq.append((label, p))
    return uniq


def list_folder(folder: Path, kind: str = "all", query: str = "") -> tuple[list[Path], list[Path]]:
    """(подпапки, медиафайлы) — файлы новые сверху, скрытые не показываем."""
    dirs, files = [], []
    q = query.strip().lower()
    try:
        entries = list(folder.iterdir())
    except OSError as e:
        log.warning("Папка не читается: %s (%s)", folder, e)
        return [], []
    for p in entries:
        if p.name.startswith("."):
            continue
        try:
            if p.is_dir():
                if not q or q in p.name.lower():
                    dirs.append(p)
                continue
        except OSError:
            continue
        k = kind_of(p)
        if k and (kind == "all" or k == kind) and (not q or q in p.name.lower()):
            files.append(p)

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    dirs.sort(key=lambda p: p.name.lower())
    files.sort(key=mtime, reverse=True)
    return dirs, files[:MAX_FILES]


def _size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class _FileList(QListWidget):
    """Список, из которого файлы можно перетащить на ленту."""

    def mimeData(self, items) -> QMimeData:
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(it.data(PATH_ROLE)) for it in items if not it.data(DIR_ROLE)])
        return data

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]


class LibraryPanel(QFrame):
    """Файлы с компьютера, выезжают слева (кнопка «Файлы» на панели инструментов)."""

    add_requested = Signal(list)       # пути файлов — поставить на ленту
    close_requested = Signal()

    def __init__(self, thumbs: Thumbnailer, output_dir: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs
        self.setObjectName("libraryPanel")
        self.setStyleSheet("QFrame#libraryPanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        store = QSettings("Glimpsy", "editor")
        self._places = places(output_dir)
        saved = Path(store.value("library_dir", "", type=str) or "")
        self.folder = saved if str(saved) not in ("", ".") and saved.is_dir() else \
            (self._places[0][1] if self._places else Path.home())

        title = QLabel("Файлы")
        title.setProperty("role", "title")
        close = QToolButton()
        close.setIcon(theme.icon("chevron-left", theme.MUTED, 18))
        close.setToolTip("Свернуть")
        close.clicked.connect(self.close_requested)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close)

        self.place = QComboBox()
        self.place.setToolTip("Быстрый переход к папке")
        for label, p in self._places:
            self.place.addItem(theme.icon("house" if p == Path.home() else "folder", theme.MUTED, 16), label, str(p))
        self.place.addItem(theme.icon("folder-open", theme.MUTED, 16), "Другая папка…", "")
        self.place.activated.connect(self._on_place)

        self.up_btn = QToolButton()
        self.up_btn.setIcon(theme.icon("arrow-up", theme.MUTED, 16))
        self.up_btn.setToolTip("На папку выше")
        self.up_btn.clicked.connect(lambda: self.open_folder(self.folder.parent))
        self.path_lbl = QLabel()
        self.path_lbl.setProperty("role", "hint")
        self.path_lbl.setMinimumWidth(10)
        refresh = QToolButton()
        refresh.setIcon(theme.icon("rotate-ccw", theme.MUTED, 16))
        refresh.setToolTip("Обновить")
        refresh.clicked.connect(self.reload)
        path_row = QHBoxLayout()
        path_row.addWidget(self.up_btn)
        path_row.addWidget(self.path_lbl, 1)
        path_row.addWidget(refresh)

        self.kind = QComboBox()
        for key, label in KINDS.items():
            self.kind.addItem(label, key)
        self.kind.setCurrentIndex(max(0, self.kind.findData(store.value("library_kind", "all", type=str))))
        self.kind.currentIndexChanged.connect(self.reload)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по имени")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)
        filt = QHBoxLayout()
        filt.addWidget(self.kind)
        filt.addWidget(self.search, 1)

        self.list = _FileList()
        self.list.setIconSize(QSize(THUMB_W, THUMB_H))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list.setUniformItemSizes(True)
        self.list.setSpacing(1)
        self.list.itemDoubleClicked.connect(self._on_double)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.empty = QLabel()
        self.empty.setProperty("role", "hint")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.add_btn = theme.mark(QPushButton(theme.icon("plus", "#FFFFFF", 16), "  На ленту"), "primary")
        self.add_btn.setToolTip("Поставить выбранные файлы после выбранного фрагмента (или двойной щелчок)")
        self.add_btn.clicked.connect(self._add_selected)
        hint = QLabel("Двойной щелчок — на ленту, или перетащите мышкой")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addWidget(self.place)
        lay.addLayout(path_row)
        lay.addLayout(filt)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.empty)
        lay.addWidget(hint)
        lay.addWidget(self.add_btn)
        self.setFixedWidth(300)

        self._pending: set[str] = set()          # файлы, чья миниатюра ещё готовится
        self.thumbs.ready.connect(self._fill_thumbs)
        self._loaded = False
        self.setVisible(self.is_open())
        if self.isVisible():
            self.reload()

    # ---------- открыто / свёрнуто ----------

    @staticmethod
    def is_open() -> bool:
        return QSettings("Glimpsy", "editor").value("library_open", False, type=bool)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)
        QSettings("Glimpsy", "editor").setValue("library_open", on)
        if on and not self._loaded:
            self.reload()

    # ---------- папки ----------

    def _on_place(self, index: int) -> None:
        path = self.place.itemData(index)
        if not path:
            chosen = QFileDialog.getExistingDirectory(self, "Выберите папку", str(self.folder))
            self._sync_place()
            if chosen:
                self.open_folder(Path(chosen))
            return
        self.open_folder(Path(path))

    def _sync_place(self) -> None:
        """В списке мест — то место, внутри которого мы сейчас (или «Другая папка…»)."""
        best, best_len = self.place.count() - 1, -1
        for i, (_label, p) in enumerate(self._places):
            if (self.folder == p or p in self.folder.parents) and len(p.parts) > best_len:
                best, best_len = i, len(p.parts)
        self.place.setCurrentIndex(best)

    def open_folder(self, folder: Path) -> None:
        if not folder.is_dir():
            return
        self.folder = folder
        QSettings("Glimpsy", "editor").setValue("library_dir", str(folder))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.reload()

    def reload(self) -> None:
        self._loaded = True
        kind = self.kind.currentData() or "all"
        QSettings("Glimpsy", "editor").setValue("library_kind", kind)
        self._sync_place()
        self.path_lbl.setText(self.folder.name or str(self.folder))
        self.path_lbl.setToolTip(str(self.folder))
        self.up_btn.setEnabled(self.folder.parent != self.folder)
        dirs, files = list_folder(self.folder, kind, self.search.text())
        self.list.clear()
        self._pending.clear()
        folder_icon = self._blank_icon("folder")
        for d in dirs:
            it = QListWidgetItem(folder_icon, d.name)
            it.setData(PATH_ROLE, str(d))
            it.setData(DIR_ROLE, True)
            it.setToolTip(str(d))
            self.list.addItem(it)
        for f in files:
            try:
                st = f.stat()
                info = _size(st.st_size)
            except OSError:
                info = ""
            it = QListWidgetItem(f.name + (f"\n{info}" if info else ""))
            it.setData(PATH_ROLE, str(f))
            it.setData(DIR_ROLE, False)
            it.setToolTip(str(f))
            self.list.addItem(it)
            if self.list.count() - len(dirs) <= MAX_THUMBS:
                self._set_thumb(it)
            else:
                k = kind_of(f)
                it.setIcon(self._blank_icon("music" if k == "audio" else "film" if k == "video" else "image"))
        if not dirs and not files:
            self.empty.setText("Здесь нет видео, фото и музыки" if not self.search.text()
                               else "Ничего не нашлось")
        self.empty.setVisible(not dirs and not files)
        self._update_buttons()

    # ---------- миниатюры ----------

    def _blank_icon(self, name: str) -> QIcon:
        pm = QPixmap(THUMB_W, THUMB_H)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        ic = theme.pixmap(name, theme.MUTED, 22)
        p.drawPixmap((THUMB_W - ic.width()) // 2, (THUMB_H - ic.height()) // 2, ic)
        p.end()
        return QIcon(pm)

    def _set_thumb(self, it: QListWidgetItem) -> bool:
        path = Path(it.data(PATH_ROLE))
        k = kind_of(path)
        if k == "audio":
            it.setIcon(self._blank_icon("music"))
            return True
        img = self.thumbs.get(path, 1.0 if k == "video" else 0.0, THUMB_H * 2, is_image=k == "image")
        if img is None or img.isNull():
            it.setIcon(self._blank_icon("film" if k == "video" else "image"))
            self._pending.add(str(path))
            return False
        pm = QPixmap.fromImage(img).scaled(THUMB_W * 2, THUMB_H * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(2.0)
        it.setIcon(QIcon(pm))
        self._pending.discard(str(path))
        return True

    def _fill_thumbs(self) -> None:
        if not self._pending or not self.isVisible():
            return
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(PATH_ROLE) in self._pending:
                self._set_thumb(it)

    # ---------- на ленту ----------

    def _files(self, items) -> list[str]:
        return [it.data(PATH_ROLE) for it in items if not it.data(DIR_ROLE)]

    def _update_buttons(self) -> None:
        self.add_btn.setEnabled(bool(self._files(self.list.selectedItems())))

    def _on_double(self, it: QListWidgetItem) -> None:
        if it.data(DIR_ROLE):
            self.open_folder(Path(it.data(PATH_ROLE)))
        else:
            self.add_requested.emit([it.data(PATH_ROLE)])

    def _add_selected(self) -> None:
        files = self._files(self.list.selectedItems())
        if files:
            self.add_requested.emit(files)
