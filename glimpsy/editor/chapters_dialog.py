"""Окно «Главы для YouTube»: список глав, правка, «Скопировать» — и вставить в описание видео."""

from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from glimpsy.editor import chapters as ch
from glimpsy.ui import theme


def parse_time(text: str) -> float | None:
    """«1:25», «01:02:03», «85» → секунды."""
    parts = re.findall(r"\d+(?:[.,]\d+)?", text)
    if not parts or len(parts) > 3:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + float(p.replace(",", "."))
    return total


class ChaptersDialog(QDialog):
    def __init__(self, items: list[dict], total: float, suggest: Callable[[], list[dict]],
                 now: Callable[[], float], seek: Callable[[float], None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Главы для YouTube")
        self.resize(560, 520)
        self.total = total
        self._suggest, self._now, self._seek = suggest, now, seek
        self.items = [dict(c) for c in items]
        self._filling = False

        info = QLabel("Главы видны на полосе просмотра YouTube. Скопируйте список и вставьте в описание "
                      "видео. Время и название можно исправить двойным щелчком.")
        info.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Время", "Название"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._on_changed)
        self.table.cellClicked.connect(lambda r, _c: self._seek(self.items[r]["t"]) if r < len(self.items) else None)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("role", "hint")

        auto = theme.mark(QPushButton(theme.icon("sparkles", size=16), " Предложить по тексту"), "ghost")
        auto.setToolTip("Разбить ролик на главы по расшифровке речи (заменит список)")
        auto.clicked.connect(self._auto)
        add = theme.mark(QPushButton(theme.icon("plus", size=16), " Глава здесь"), "ghost")
        add.setToolTip("Новая глава в месте курсора на ленте")
        add.clicked.connect(self._add)
        rem = theme.mark(QPushButton(theme.icon("trash-2", size=16), ""), "ghost")
        rem.setToolTip("Удалить выбранную главу")
        rem.clicked.connect(self._remove)
        copy = theme.mark(QPushButton(theme.icon("check", "#FFFFFF", 16), "  Скопировать для YouTube"), "primary")
        copy.clicked.connect(self._copy)
        close = QPushButton("Готово")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(auto)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        bottom = QHBoxLayout()
        bottom.addWidget(copy)
        bottom.addStretch(1)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(row)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.status)
        lay.addLayout(bottom)
        if not self.items:
            self.items = self._suggest()
        self._fill()

    def _fill(self) -> None:
        self.items.sort(key=lambda c: c["t"])
        self._filling = True
        self.table.setRowCount(len(self.items))
        for r, c in enumerate(self.items):
            t = QTableWidgetItem(ch.fmt(c["t"], self.total >= 3600))
            t.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, 0, t)
            self.table.setItem(r, 1, QTableWidgetItem(c["title"]))
        self._filling = False
        bad = ch.problems(self.items, self.total)
        self.status.setText(" ".join(bad) if bad else f"Глав: {len(self.items)}. Всё по правилам YouTube.")
        if not self.items:
            self.status.setText("Пока нет глав. «Предложить по тексту» — если речь расшифрована, "
                                "или «Глава здесь» — в месте курсора.")

    def _on_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        r = item.row()
        if item.column() == 1:
            self.items[r]["title"] = item.text().strip()
        else:
            t = parse_time(item.text())
            if t is not None:
                self.items[r]["t"] = max(0.0, min(t, self.total))
        self._fill()

    def _auto(self) -> None:
        got = self._suggest()
        if got:
            self.items = got
        self._fill()
        if not got:
            self.status.setText("Не получилось предложить главы: нужна расшифровка речи "
                                "(панель «Текст» или субтитры), а ролик — длиннее 30 секунд.")

    def _add(self) -> None:
        t = round(self._now(), 2)
        self.items.append({"t": t, "title": "Новая глава"})
        self._fill()
        r = next(i for i, c in enumerate(self.items) if c["t"] == t)
        self.table.editItem(self.table.item(r, 1))

    def _remove(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.items):
            del self.items[r]
            self._fill()

    def _copy(self) -> None:
        QApplication.clipboard().setText(ch.as_text(self.items, self.total))
        self.status.setText("Скопировано — вставьте в описание видео на YouTube (Ctrl+V).")
