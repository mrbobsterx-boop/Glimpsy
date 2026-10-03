"""Окно «Обложка»: выбрать кадр (или свою картинку), добавить заголовок и сохранить."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSlider, QVBoxLayout,
)

from glimpsy.editor import cover
from glimpsy.editor.text_panel import ColorButton
from glimpsy.ui import theme

log = logging.getLogger(__name__)

PLACES = [("bottom", "Внизу"), ("center", "По центру"), ("top", "Вверху")]


class _Bridge(QObject):
    ready = Signal(list)


class CoverDialog(QDialog):
    def __init__(self, ffmpeg: str, project, out_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Обложка")
        self.resize(1000, 620)
        self.ffmpeg, self.project, self.out_dir = ffmpeg, project, out_dir
        self.opts = cover.settings(project)
        self.aspect = project.aspect if project.aspect in cover.SIZES else "16:9"
        self.background = QImage()
        self.frames: list[tuple[float, QImage]] = []
        self.saved: Path | None = None

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(176, 99))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setFixedWidth(400)
        self.list.currentRowChanged.connect(self._on_pick)
        self.wait = theme.mark(QLabel("Подбираю удачные кадры…"), "muted")
        own = theme.mark(QPushButton(theme.icon("image-plus", size=16), " Своя картинка…"), "ghost")
        own.clicked.connect(self._own_image)
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Кадр</b> — программа выбрала лучшие:"))
        left.addWidget(self.wait)
        left.addWidget(self.list, 1)
        left.addWidget(own)

        self.view = QLabel()
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(520, 300)
        self.fmt = QComboBox()
        self.fmt.addItem("YouTube — 1280×720", "16:9")
        self.fmt.addItem("Shorts, Reels — 1080×1920", "9:16")
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(self.aspect)))
        self.fmt.currentIndexChanged.connect(lambda _i: self._set("_aspect", self.fmt.currentData()))
        self.title = QLineEdit(self.opts["title"])
        self.title.setPlaceholderText("Заголовок, например «Обзор камеры за 5 минут»")
        self.title.textChanged.connect(lambda t: self._set("title", t))
        self.sub = QLineEdit(self.opts["subtitle"])
        self.sub.setPlaceholderText("Вторая строка (можно пусто)")
        self.sub.textChanged.connect(lambda t: self._set("subtitle", t))
        self.place = QComboBox()
        for key, label in PLACES:
            self.place.addItem(label, key)
        self.place.setCurrentIndex(max(0, self.place.findData(self.opts["place"])))
        self.place.currentIndexChanged.connect(lambda _i: self._set("place", self.place.currentData()))
        self.size = QSlider(Qt.Orientation.Horizontal, minimum=6, maximum=24)
        self.size.setValue(int(round(float(self.opts["size"]) * 100)))
        self.size.valueChanged.connect(lambda v: self._set("size", v / 100))
        self.color = ColorButton()
        self.color.set_color(self.opts["color"])
        self.color.picked.connect(lambda c: self._set("color", c))
        self.accent = ColorButton()
        self.accent.set_color(self.opts["accent"])
        self.accent.picked.connect(lambda c: self._set("accent", c))
        colors = QHBoxLayout()
        colors.addWidget(self.color)
        colors.addWidget(QLabel("вторая строка:"))
        colors.addWidget(self.accent)
        colors.addStretch(1)
        self.outline = QCheckBox("Обводка букв")
        self.outline.setChecked(bool(self.opts["outline"]))
        self.outline.toggled.connect(lambda on: self._set("outline", on))
        self.shade = QCheckBox("Затемнить под текстом")
        self.shade.setChecked(bool(self.opts["shade"]))
        self.shade.toggled.connect(lambda on: self._set("shade", on))
        form = QFormLayout()
        form.addRow("Для", self.fmt)
        form.addRow("Заголовок", self.title)
        form.addRow("Ниже", self.sub)
        form.addRow("Где", self.place)
        form.addRow("Размер", self.size)
        form.addRow("Цвет", colors)
        form.addRow(self.outline)
        form.addRow(self.shade)
        self.status = theme.mark(QLabel(), "muted")
        self.status.setWordWrap(True)
        save = theme.mark(QPushButton(theme.icon("download", "#FFFFFF", 16), "  Сохранить обложку…"), "primary")
        save.clicked.connect(self._save)
        done = QPushButton("Готово")
        done.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(save)
        buttons.addWidget(done)
        right = QVBoxLayout()
        right.addWidget(self.view, 1)
        right.addLayout(form)
        body = QHBoxLayout()
        body.addLayout(left)
        body.addLayout(right, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addLayout(buttons)

        if self.opts.get("image") and Path(self.opts["image"]).exists():
            self.background = QImage(self.opts["image"])
        self._render()
        self._bridge = _Bridge(self)
        self._bridge.ready.connect(self._on_frames)
        threading.Thread(target=self._find, daemon=True, name="cover-frames").start()

    # ---------- кадры ----------

    def _find(self) -> None:
        try:
            frames = cover.candidates(self.ffmpeg, self.project)
        except Exception:                                   # noqa: BLE001 — без кадров можно взять свою картинку
            log.exception("Кадры для обложки не подобрались")
            frames = []
        try:
            self._bridge.ready.emit(frames)
        except RuntimeError:                                # окно уже закрыли
            pass

    def _on_frames(self, frames: list) -> None:
        self.frames = frames
        self.wait.setVisible(False)
        from glimpsy.editor.timeline import fmt_time

        for t, img in frames:
            it = QListWidgetItem(QIcon(QPixmap.fromImage(img.scaled(176, 99, Qt.AspectRatioMode.KeepAspectRatio,
                                                                    Qt.TransformationMode.SmoothTransformation))),
                                 fmt_time(t))
            self.list.addItem(it)
        if not frames:
            self.wait.setText("Кадры не нашлись — возьмите свою картинку.")
            self.wait.setVisible(True)
            return
        want = self.opts.get("t")
        if self.background.isNull():
            best = min(range(len(frames)), key=lambda i: abs(frames[i][0] - want)) if want is not None else 0
            self.list.setCurrentRow(best)

    def _on_pick(self, row: int) -> None:
        if 0 <= row < len(self.frames):
            t, img = self.frames[row]
            where = cover.frame_at(self.project, t)
            if where is not None and where[0].suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                full = cover.grab(self.ffmpeg, where[0], where[1], 1920)   # для обложки — в полном размере
                img = full if not full.isNull() else img
            self.background = img
            self.opts["t"], self.opts["image"] = t, ""
            self._render()

    def _own_image(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Своя картинка для обложки", str(Path.home()),
                                           "Картинки (*.png *.jpg *.jpeg *.webp *.bmp)")
        if f:
            img = QImage(f)
            if not img.isNull():
                self.background = img
                self.opts["image"] = f
                self.list.clearSelection()
                self._render()

    # ---------- обложка ----------

    def _set(self, key: str, value) -> None:
        if key == "_aspect":
            self.aspect = value
        else:
            self.opts[key] = value
        self._render()

    def image(self) -> QImage:
        return cover.compose(self.background, self.opts, self.aspect)

    def _render(self) -> None:
        img = self.image()
        self.view.setPixmap(QPixmap.fromImage(img).scaled(self.view.width() or 520, self.view.height() or 300,
                                                          Qt.AspectRatioMode.KeepAspectRatio,
                                                          Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._render()

    def _save(self) -> None:
        stem = self.project.name + ("_cover" if self.aspect == "16:9" else "_cover_9x16")
        f, _ = QFileDialog.getSaveFileName(self, "Сохранить обложку", str(self.out_dir / f"{stem}.jpg"),
                                           "JPEG (*.jpg);;PNG (*.png)")
        if not f:
            return
        path = Path(f)
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            path = path.with_suffix(".jpg")
        if self.image().save(str(path), quality=92):
            self.saved = path
            self.status.setText(f"Сохранено: {path}")
        else:
            self.status.setText("Не удалось сохранить — проверьте папку.")

    def result_opts(self) -> dict:
        return {k: v for k, v in self.opts.items() if k in cover.DEFAULTS}
