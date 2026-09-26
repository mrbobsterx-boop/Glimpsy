"""Раскрывающаяся слева панель «Субтитры»: все субтитры списком, правка прямо в списке.

Щелчок по строке — переход к этому месту ролика; текст меняется двойным щелчком (или
сразу набором). Кнопки: распознать речь заново, добавить субтитр в месте курсора,
удалить выбранные. Стиль всех субтитров — справа, как у любого текста.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from glimpsy.editor.timeline import fmt_time
from glimpsy.ui import theme

ID_ROLE = Qt.ItemDataRole.UserRole


def subtitle_items(project) -> list:
    """Субтитры проекта (всё на дорожках «Субтитры»), по времени."""
    ids = {tr.id for tr in project.tracks if tr.kind == "subtitles"}
    return sorted((t for t in project.texts if t.track in ids), key=lambda t: t.start)


class SubtitlesPanel(QFrame):
    close_requested = Signal()
    recognize_requested = Signal()
    add_requested = Signal()
    selected = Signal(str)              # id субтитра — перейти к нему
    text_edited = Signal(str, str)      # id, новый текст
    delete_requested = Signal(list)     # id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("subtitlesPanel")
        self.setStyleSheet("QFrame#subtitlesPanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        self._shown: list[tuple[str, float, str]] = []
        self._filling = False

        title = QLabel("Субтитры")
        title.setProperty("role", "title")
        close = QToolButton()
        close.setIcon(theme.icon("chevron-left", theme.MUTED, 18))
        close.setToolTip("Свернуть")
        close.clicked.connect(self.close_requested)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close)

        rec = theme.mark(QPushButton(theme.icon("captions", "#FFFFFF", 16), "  Распознать речь"), "primary")
        rec.setToolTip("Автосубтитры: распознать речь в ролике (на этом компьютере)")
        rec.clicked.connect(self.recognize_requested)
        add = theme.mark(QPushButton(theme.icon("plus", size=16), " Субтитр"), "ghost")
        add.setToolTip("Новый субтитр в месте курсора")
        add.clicked.connect(self.add_requested)
        self.del_btn = theme.mark(QPushButton(theme.icon("trash-2", size=16), ""), "ghost")
        self.del_btn.setToolTip("Удалить выбранные (Delete)")
        self.del_btn.clicked.connect(self._delete_selected)
        row = QHBoxLayout()
        row.addWidget(rec, 1)
        row.addWidget(add)
        row.addWidget(self.del_btn)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Время", "Текст"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.itemChanged.connect(self._on_changed)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.count = QLabel()
        self.count.setProperty("role", "hint")
        hint = QLabel("Щелчок — перейти к месту, двойной щелчок — исправить текст. "
                      "Вид всех субтитров — справа.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addLayout(row)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.count)
        lay.addWidget(hint)
        self.setFixedWidth(320)
        self.setVisible(False)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)

    def refresh(self, project, current: str | None = None) -> None:
        """Показать субтитры проекта (список перестраивается, только если он правда изменился)."""
        if not self.isVisible():
            return
        items = subtitle_items(project)
        shown = [(t.id, round(t.start, 2), t.text) for t in items]
        if shown != self._shown:
            self._shown = shown
            self._filling = True
            self.table.setRowCount(len(items))
            for r, t in enumerate(items):
                when = QTableWidgetItem(fmt_time(t.start, precise=True))
                when.setFlags(when.flags() & ~Qt.ItemFlag.ItemIsEditable)
                when.setData(ID_ROLE, t.id)
                text = QTableWidgetItem(t.text)
                text.setData(ID_ROLE, t.id)
                self.table.setItem(r, 0, when)
                self.table.setItem(r, 1, text)
            self.table.resizeRowsToContents()
            self._filling = False
            self.count.setText(f"Субтитров: {len(items)}" if items else
                               "Субтитров пока нет — нажмите «Распознать речь» или «+ Субтитр»")
        if current is not None:
            self.show_current(current)

    def show_current(self, text_id: str | None) -> None:
        """Подсветить субтитр, выбранный на ленте."""
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and it.data(ID_ROLE) == text_id:
                if self.table.currentRow() != r:
                    self._filling = True
                    self.table.selectRow(r)
                    self._filling = False
                self.table.scrollToItem(it)
                return

    def _on_changed(self, it: QTableWidgetItem) -> None:
        if self._filling or it.column() != 1:
            return
        self.text_edited.emit(it.data(ID_ROLE), it.text())

    def _on_select(self) -> None:
        if self._filling:
            return
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if rows:
            it = self.table.item(rows[0], 0)
            if it is not None:
                self.selected.emit(it.data(ID_ROLE))

    def _delete_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        ids = [self.table.item(r, 0).data(ID_ROLE) for r in rows if self.table.item(r, 0) is not None]
        if ids:
            self.delete_requested.emit(ids)
