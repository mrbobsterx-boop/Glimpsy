"""Панель «Текст» — монтаж по тексту.

Всё, что сказано в видео, — текстом. Щелчок по слову — видео перематывается к нему.
Выделили кусок текста и нажали Delete — этот кусок вырезан из видео (текст остаётся,
но зачёркнут). Щелчок по зачёркнутому — вернуть. Паузы показаны как «[пауза 1,2 с]»:
длинные вырезаются сами, щелчок по паузе — оставить её (или снова вырезать).
"""

from __future__ import annotations

from bisect import bisect_right

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from glimpsy.editor import transcript as tr
from glimpsy.editor.timeline import fmt_time
from glimpsy.ui import theme

KEY = QTextFormat.Property.UserProperty + 1          # у каждого слова и паузы: "w|p", номер видео, номер слова
COL_TEXT = QColor("#E6E8EE")
COL_GONE = QColor("#6B7280")
COL_PAUSE = QColor("#7C8494")
COL_NOW = QColor(34, 174, 187, 90)


def _pause_text(gap: float) -> str:
    return f"[пауза {gap:.1f} с] ".replace(".", ",")


class TranscriptView(QTextEdit):
    """Текст с таймкодами: слова и паузы, у каждого — своё состояние (оставлен / вырезан)."""

    clicked = Signal(tuple)          # ("w" | "p", номер видео, номер слова)
    delete_keys = Signal(list)       # выделенные слова и паузы — вырезать
    play_toggle = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setUndoRedoEnabled(False)
        f = QFont(self.font())
        f.setPixelSize(15)
        self.setFont(f)
        self.setStyleSheet("QTextEdit { background: #0F1217; border: none; padding: 6px; }")
        self._spans: dict[tuple, tuple[int, int]] = {}     # ключ → (начало, конец) в тексте
        self._state: dict[tuple, str] = {}                 # ключ → как показан сейчас
        self._starts: list[list[float]] = []               # начала слов каждого видео (для поиска)
        self._press = -1

    # ---------- построение ----------

    def build(self, sources: list[tuple[str, str, tr.SourceWords | None]], pause_min: float) -> None:
        """sources: [(путь видео, подпись, слова или None)]."""
        self.clear()
        self._spans.clear()
        self._state.clear()
        self._starts = []
        cur = self.textCursor()
        cur.beginEditBlock()
        head = QTextCharFormat()
        head.setFontWeight(QFont.Weight.Bold)
        head.setForeground(COL_PAUSE)
        for si, (_src, label, sw) in enumerate(sources):
            if len(sources) > 1:
                if si:
                    cur.insertBlock()
                cur.insertText(f"🎬 {label}", head)
                cur.insertBlock()
            words = sw.words if sw is not None else []
            self._starts.append([w.start for w in words])
            if sw is None:
                cur.insertText("(ещё не расшифровано)", head)
                continue
            gaps = {i: g for i, g in tr.pauses(words, pause_min, sw.duration)}
            if -1 in gaps:
                self._put(cur, ("p", si, -1), _pause_text(gaps[-1]))
            for i, w in enumerate(words):
                self._put(cur, ("w", si, i), w.text)
                cur.insertText(" ", QTextCharFormat())
                if i in gaps:
                    self._put(cur, ("p", si, i), _pause_text(gaps[i]))
                nxt = words[i + 1].start if i + 1 < len(words) else None
                # абзац — после конца предложения и заметной паузы (так текст легче читать)
                if nxt is not None and (nxt - w.end > 1.5 or (w.text[-1:] in ".?!…" and nxt - w.end > 0.6)):
                    cur.insertBlock()
        cur.endEditBlock()
        self.moveCursor(QTextCursor.MoveOperation.Start)

    def _put(self, cur: QTextCursor, key: tuple, text: str) -> None:
        a = cur.position()
        fmt = QTextCharFormat()
        fmt.setProperty(KEY, "|".join(map(str, key)))
        cur.insertText(text, fmt)
        self._spans[key] = (a, cur.position())

    # ---------- состояние слов ----------

    def apply_states(self, states: dict[tuple, str]) -> None:
        """states: ключ → "keep" | "cut" | "filler" — перекрашиваем только то, что изменилось."""
        cur = QTextCursor(self.document())
        cur.beginEditBlock()
        for key, (a, b) in self._spans.items():
            st = states.get(key, "keep")
            if self._state.get(key) == st:
                continue
            self._state[key] = st
            cur.setPosition(a)
            cur.setPosition(b, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setProperty(KEY, "|".join(map(str, key)))
            pause = key[0] == "p"
            fmt.setForeground(COL_GONE if st == "cut" else COL_PAUSE if pause else COL_TEXT)
            fmt.setFontStrikeOut(st == "cut")
            cur.setCharFormat(fmt)
        cur.endEditBlock()

    def set_current(self, si: int, t: float, follow: bool) -> None:
        """Подсветить слово, которое звучит сейчас."""
        sels = []
        if 0 <= si < len(self._starts) and self._starts[si]:
            i = max(0, bisect_right(self._starts[si], t) - 1)
            span = self._spans.get(("w", si, i))
            if span is not None:
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(COL_NOW)
                c = QTextCursor(self.document())
                c.setPosition(span[0])
                c.setPosition(span[1], QTextCursor.MoveMode.KeepAnchor)
                sel.cursor = c
                sels.append(sel)
                if follow:
                    r = self.cursorRect(c)
                    if not self.viewport().rect().adjusted(0, 40, 0, -40).contains(r.center()):
                        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + r.center().y()
                                                          - self.viewport().height() // 3)
        self.setExtraSelections(sels)

    # ---------- мышь и клавиши ----------

    def _key_at(self, pos: int) -> tuple | None:
        c = QTextCursor(self.document())
        c.setPosition(min(pos + 1, self.document().characterCount() - 1))
        raw = c.charFormat().property(KEY)
        if not raw:
            c.setPosition(pos)
            raw = c.charFormat().property(KEY)
        if not raw:
            return None
        kind, si, i = str(raw).split("|")
        return kind, int(si), int(i)

    def keys_in_selection(self) -> list[tuple]:
        c = self.textCursor()
        if not c.hasSelection():
            return []
        a, b = c.selectionStart(), c.selectionEnd()
        return [k for k, (x, y) in self._spans.items() if x < b and y > a]

    def mousePressEvent(self, e) -> None:
        self._press = self.cursorForPosition(e.position().toPoint()).position()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        super().mouseReleaseEvent(e)
        if e.button() != Qt.MouseButton.LeftButton or self.textCursor().hasSelection():
            return
        key = self._key_at(self.cursorForPosition(e.position().toPoint()).position())
        if key is not None:
            self.clicked.emit(key)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            keys = self.keys_in_selection()
            if keys:
                self.delete_keys.emit(keys)
                c = self.textCursor()
                c.clearSelection()
                self.setTextCursor(c)
            return
        if e.key() == Qt.Key.Key_Space:
            self.play_toggle.emit()
            return
        super().keyPressEvent(e)


class TranscriptPanel(QFrame):
    """Раскрывающаяся слева панель «Текст»."""

    close_requested = Signal()
    transcribe_requested = Signal()
    cancel_requested = Signal()
    settings_changed = Signal(dict)       # {"pause_cut", "pause_min", "pad"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("transcriptPanel")
        self.setStyleSheet("QFrame#transcriptPanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        self._loading = False
        title = QLabel("Монтаж по тексту")
        title.setProperty("role", "title")
        close = QToolButton()
        close.setIcon(theme.icon("chevron-left", theme.MUTED, 18))
        close.setToolTip("Свернуть")
        close.clicked.connect(self.close_requested)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(close)

        self.transcribe_btn = theme.mark(QPushButton(theme.icon("captions", "#FFFFFF", 16), "  Расшифровать речь"),
                                         "primary")
        self.transcribe_btn.clicked.connect(self.transcribe_requested)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar_text = QLabel()
        self.bar_text.setProperty("role", "hint")
        self.cancel_btn = theme.mark(QPushButton("Остановить"), "ghost")
        self.cancel_btn.clicked.connect(self.cancel_requested)
        prog = QHBoxLayout()
        prog.addWidget(self.bar, 1)
        prog.addWidget(self.cancel_btn)

        self.pause_cut = QCheckBox("Вырезать паузы длиннее")
        self.pause_min = QDoubleSpinBox(minimum=0.3, maximum=5.0, singleStep=0.1, decimals=1, suffix=" с")
        self.pad = QDoubleSpinBox(minimum=0.0, maximum=0.5, singleStep=0.05, decimals=2, suffix=" с")
        self.pad.setToolTip("Сколько оставлять тишины у речи на стыках — чтобы не звучало рублено")
        prow = QHBoxLayout()
        prow.addWidget(self.pause_cut)
        prow.addWidget(self.pause_min)
        prow.addStretch(1)
        prow2 = QHBoxLayout()
        pad_lbl = QLabel("Запас тишины на стыках")
        pad_lbl.setToolTip(self.pad.toolTip())
        prow2.addWidget(pad_lbl)
        prow2.addWidget(self.pad)
        prow2.addStretch(1)
        for w in (self.pause_cut,):
            w.toggled.connect(self._emit_settings)
        for w in (self.pause_min, self.pad):
            w.valueChanged.connect(self._emit_settings)

        self.stats = QLabel()
        self.stats.setProperty("role", "hint")
        self.view = TranscriptView()
        hint = QLabel("Щелчок по слову — перейти. Выделите текст и нажмите Delete — вырезать. "
                      "Щелчок по зачёркнутому — вернуть. Пробел — пуск/пауза, Ctrl+Z — отменить.")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addWidget(self.transcribe_btn)
        lay.addLayout(prog)
        lay.addWidget(self.bar_text)
        lay.addLayout(prow)
        lay.addLayout(prow2)
        lay.addWidget(self.stats)
        lay.addWidget(self.view, 1)
        lay.addWidget(hint)
        self.setMinimumWidth(380)
        self.setMaximumWidth(560)
        self.set_progress(None)
        self.setVisible(False)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)

    def set_settings(self, cuts: dict) -> None:
        self._loading = True
        self.pause_cut.setChecked(bool(cuts["pause_cut"]))
        self.pause_min.setValue(float(cuts["pause_min"]))
        self.pad.setValue(float(cuts["pad"]))
        self.pause_min.setEnabled(bool(cuts["pause_cut"]))
        self._loading = False

    def _emit_settings(self) -> None:
        self.pause_min.setEnabled(self.pause_cut.isChecked())
        if not self._loading:
            self.settings_changed.emit({"pause_cut": self.pause_cut.isChecked(),
                                        "pause_min": round(self.pause_min.value(), 2),
                                        "pad": round(self.pad.value(), 3)})

    def set_needs_transcript(self, missing: int) -> None:
        self.transcribe_btn.setVisible(missing > 0)
        self.transcribe_btn.setText("  Расшифровать речь" + (f" ({missing} видео)" if missing > 1 else ""))

    def set_progress(self, value: float | None, text: str = "") -> None:
        on = value is not None
        for w in (self.bar, self.cancel_btn, self.bar_text):
            w.setVisible(on)
        if on:
            self.bar.setValue(int(value * 1000))
            self.bar_text.setText(text)
            self.transcribe_btn.setVisible(False)

    def set_stats(self, before: float, after: float) -> None:
        cut = max(0.0, before - after)
        self.stats.setText(f"Было {fmt_time(before)} → стало {fmt_time(after)}" +
                           (f"  (вырезано {fmt_time(cut)})" if cut > 0.5 else ""))

