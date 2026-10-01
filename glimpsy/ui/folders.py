"""«Папки программы»: где что лежит (ролики, проекты, модели, голоса, журналы) — открыть одним нажатием.

Чтобы не искать файлы Glimpsy по всему компьютеру: всё, что программа хранит, — списком,
с размером и кнопкой «Открыть» (открывается в обычном файловом менеджере).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from glimpsy import paths


@dataclass
class Place:
    title: str
    path: Path
    note: str = ""


def places(output_dir: Path | str | None = None, project_dir: Path | None = None) -> list[Place]:
    """Все папки программы — по порядку, как их обычно ищут."""
    from glimpsy.editor import subtitles as subs
    from glimpsy.editor import translate as mt
    from glimpsy.editor import voice
    from glimpsy.editor.sessions import projects_root

    out: list[Place] = []
    if output_dir:
        out.append(Place("Готовые ролики", Path(output_dir), "сюда сохраняются записи экрана"))
    if project_dir is not None:
        out.append(Place("Этот проект", Path(project_dir), "монтаж, расшифровка, переозвучка этого проекта"))
    out += [
        Place("Все проекты и сессии", projects_root(), "проекты редактора и записи для монтажа"),
        Place("Модели распознавания речи", subs.models_dir(), "для расшифровки и субтитров (whisper)"),
        Place("Переводчик субтитров", mt.model_dir("m2m").parent, "лежит в той же папке моделей"),
        Place("Голосовой модуль", voice.home(), "нейросеть для переозвучки и журнал её установки"),
        Place("Сохранённые голоса", voice.voices_dir(), "голоса для переозвучки"),
        Place("Журналы (логи)", paths.log_dir(), "пригодятся, если что-то пошло не так"),
        Place("Временные файлы записи", paths.temp_root(), "черновики, пока идёт запись; потом удаляются"),
        Place("Настройки", paths.config_dir(), "настройки программы"),
    ]
    return out


def folder_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def human_size(n: int) -> str:
    if n <= 0:
        return "пусто"
    for unit, k in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= k:
            return f"{n / k:.1f} {unit}".replace(".", ",")
    return f"{n} байт"


class _Sizes(QObject):
    ready = Signal(int, str)


class FoldersWidget(QWidget):
    """Список папок: название, где лежит, сколько занимает, «Открыть»."""

    def __init__(self, items: list[Place], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        self._sizes: list[QLabel] = []
        for row, p in enumerate(items):
            title = QLabel(f"<b>{p.title}</b><br><span style='color:#8b8d98'>{p.note}</span>")
            title.setToolTip(str(p.path))
            where = QLabel(str(p.path))
            where.setStyleSheet("color: #8b8d98; font-size: 11px;")
            where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            where.setWordWrap(True)
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(title)
            box.addWidget(where)
            size = QLabel("…")
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            btn = QPushButton("Открыть")
            btn.clicked.connect(lambda _=False, path=p.path: paths.open_in_file_manager(path))
            grid.addLayout(box, row, 0)
            grid.addWidget(size, row, 1)
            grid.addWidget(btn, row, 2)
            self._sizes.append(size)
        # размеры считаем в фоне — в папке моделей могут быть гигабайты
        self._signals = _Sizes(self)
        self._signals.ready.connect(lambda i, text: self._sizes[i].setText(text))
        sig = self._signals

        def count() -> None:
            for i, p in enumerate(items):
                try:
                    sig.ready.emit(i, human_size(folder_size(p.path)))
                except RuntimeError:                 # окно уже закрыли
                    return

        threading.Thread(target=count, daemon=True, name="folder-sizes").start()


class FoldersDialog(QDialog):
    def __init__(self, items: list[Place], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Папки программы")
        self.resize(720, 560)
        hint = QLabel("Всё, что хранит Glimpsy. «Открыть» — папка откроется в файловом менеджере.")
        hint.setWordWrap(True)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(FoldersWidget(items))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(scroll, 1)
        lay.addWidget(buttons)
