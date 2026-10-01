"""Панель «Текст» — монтаж по тексту.

Всё, что сказано в видео, — текстом. Щелчок по слову — видео перематывается к нему.
Выделили кусок текста и нажали Delete — этот кусок вырезан из видео (текст остаётся,
но зачёркнут). Щелчок по зачёркнутому — вернуть. Паузы показаны как «[пауза 1,2 с]»; щелчок по паузе —
вырезать или вернуть именно её, а ещё её можно укоротить до своей длины.

Меню режимов сверху: «Вырезать паузы» (длинные паузы уходят сами), «Паузы вручную»
(ничего само не режется — решаете по каждой паузе), «Слова-паразиты» («ну», «э», «короче»
и свои слова — найти, отметить, вырезать разом; вернуть можно так же, как любое слово).
"""

from __future__ import annotations

from bisect import bisect_right

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from glimpsy.editor import transcript as tr
from glimpsy.editor.timeline import fmt_time
from glimpsy.ui import theme

KEY = QTextFormat.Property.UserProperty + 1          # у каждого слова и паузы: "w|p", номер видео, номер слова
COL_TEXT = QColor("#E6E8EE")
COL_GONE = QColor("#6B7280")
COL_PAUSE = QColor("#7C8494")
COL_SHORT = QColor("#5FC9D3")
COL_FILLER = QColor("#F2A65A")
COL_MUTE = QColor("#8FB3FF")          # без звука (картинка идёт)
COL_HIDE_BG = QColor(150, 90, 200, 70)  # без картинки (звук идёт)
COL_VOICE = QColor("#7EE0A1")         # переозвучено вашим голосом
COL_NOW = QColor(34, 174, 187, 90)
SENTENCE_END = ".?!…"

MODES = [("auto", "Вырезать паузы"), ("manual", "Паузы вручную"), ("fillers", "Слова-паразиты")]


def _sec(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def _pause_text(gap: float, short: float | None = None) -> str:
    if short is not None and short < gap:
        return f"[пауза {_sec(gap)} → {_sec(short)} с] "
    return f"[пауза {_sec(gap)} с] "


class TranscriptView(QTextEdit):
    """Текст с таймкодами: слова и паузы, у каждого — своё состояние (оставлен / вырезан)."""

    clicked = Signal(tuple)          # ("w" | "p", номер видео, номер слова)
    delete_keys = Signal(list)       # выделенные слова и паузы — вырезать
    play_toggle = Signal()
    context = Signal(tuple, QPoint)  # правая кнопка по слову или паузе (ключ, где показать меню)
    edit_word = Signal(tuple)        # двойной щелчок по слову — исправить его
    selection_menu = Signal(list, QPoint)   # правая кнопка по выделенному тексту

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

    def build(self, sources: list[tuple], scroll: int | None = None) -> None:
        """sources: [(путь видео, подпись, слова или None, [(номер, длина паузы, укорочена до | None)],
        {номер слова: исправленный текст})].

        Каждое предложение — с новой строки; паузы — там, где они в речи.
        """
        self.clear()
        self._spans.clear()
        self._state.clear()
        self._starts = []
        cur = self.textCursor()
        cur.beginEditBlock()
        head = QTextCharFormat()
        head.setFontWeight(QFont.Weight.Bold)
        head.setForeground(COL_PAUSE)
        for si, (_src, label, sw, plist, fixes) in enumerate(sources):
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
            gaps = {i: (g, short) for i, g, short in plist}
            if -1 in gaps:
                self._put(cur, ("p", si, -1), _pause_text(*gaps[-1]))
            for i, w in enumerate(words):
                text = fixes.get(str(i), w.text)
                self._put(cur, ("w", si, i), text)
                cur.insertText(" ", QTextCharFormat())
                if i in gaps:
                    self._put(cur, ("p", si, i), _pause_text(*gaps[i]))
                nxt = words[i + 1].start if i + 1 < len(words) else None
                # каждое предложение — с новой строки (и после очень долгой паузы — тоже)
                if nxt is not None and (text[-1:] in SENTENCE_END or nxt - w.end > 1.5):
                    cur.insertBlock()
        cur.endEditBlock()
        if scroll is None:
            self.moveCursor(QTextCursor.MoveOperation.Start)
        else:
            self.verticalScrollBar().setValue(scroll)

    def _put(self, cur: QTextCursor, key: tuple, text: str) -> None:
        a = cur.position()
        fmt = QTextCharFormat()
        fmt.setProperty(KEY, "|".join(map(str, key)))
        cur.insertText(text, fmt)
        self._spans[key] = (a, cur.position())

    # ---------- состояние слов ----------

    def apply_states(self, states: dict[tuple, str]) -> None:
        """states: ключ → "keep" | "cut" | "short" | "filler" | слова "mute" / "hide" через пробел
        (без звука / без картинки) — перекрашиваем только то, что изменилось."""
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
            flags = set(st.split())
            fmt.setForeground(COL_GONE if "cut" in flags else COL_SHORT if "short" in flags else
                              COL_VOICE if "voice" in flags else COL_MUTE if "mute" in flags else
                              COL_FILLER if "filler" in flags else COL_PAUSE if pause else COL_TEXT)
            fmt.setFontStrikeOut("cut" in flags)
            fmt.setFontItalic("mute" in flags and "voice" not in flags)
            if "hide" in flags:
                fmt.setBackground(COL_HIDE_BG)
            if "filler" in flags:
                fmt.setFontUnderline(True)
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

    def mouseDoubleClickEvent(self, e) -> None:
        key = self._key_at(self.cursorForPosition(e.position().toPoint()).position())
        if key is not None and key[0] == "w":
            self.edit_word.emit(key)
            return
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e) -> None:
        keys = self.keys_in_selection()
        if keys:
            self.selection_menu.emit(keys, e.globalPos())
            return
        key = self._key_at(self.cursorForPosition(e.pos()).position())
        if key is not None:
            self.context.emit(key, e.globalPos())

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
    settings_changed = Signal(dict)       # {"mode", "pause_cut", "pause_min", "pad"}
    pause_length = Signal(float)          # выбранную паузу: −1 — целиком, 0 — вырезать, иначе — до стольких секунд
    fillers_changed = Signal(list)        # список слов-паразитов поправили
    fillers_cut = Signal(list)            # вырезать все найденные: [слово из списка]
    fillers_restore = Signal(list)        # вернуть все найденные
    subtitles_toggled = Signal(bool)      # субтитры на видео из текста: вкл / выкл
    selection_action = Signal(str)        # с выделенным текстом: "cut" | "mute" | "hide" | "restore"
    subs_lang_changed = Signal(str)       # язык субтитров: "" — как в речи, иначе код (de, en…)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("transcriptPanel")
        self.setStyleSheet("QFrame#transcriptPanel { background: #14171D; border: 1px solid #262B36;"
                           " border-radius: 12px; }")
        self._loading = False
        self._fillers: list[str] = []
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

        # режим
        self.mode = QComboBox()
        for key, text in MODES:
            self.mode.addItem(text, key)
        self.mode.setToolTip("Вырезать паузы — длинные паузы уходят сами.\n"
                             "Паузы вручную — ничего само не режется: щёлкайте по паузам.\n"
                             "Слова-паразиты — найти «ну», «э», «короче» и вырезать разом.")
        self.mode.currentIndexChanged.connect(self._on_mode)

        # паузы
        self.pause_lbl = QLabel()
        self.pause_min = QDoubleSpinBox(minimum=0.3, maximum=5.0, singleStep=0.1, decimals=1, suffix=" с")
        self.pad = QDoubleSpinBox(minimum=0.0, maximum=0.5, singleStep=0.05, decimals=2, suffix=" с")
        self.pad.setToolTip("Сколько оставлять тишины у речи на стыках — чтобы не звучало рублено")
        prow = QHBoxLayout()
        prow.addWidget(self.pause_lbl)
        prow.addWidget(self.pause_min)
        prow.addStretch(1)
        prow2 = QHBoxLayout()
        pad_lbl = QLabel("Запас тишины на стыках")
        pad_lbl.setToolTip(self.pad.toolTip())
        prow2.addWidget(pad_lbl)
        prow2.addWidget(self.pad)
        prow2.addStretch(1)
        for w in (self.pause_min, self.pad):
            w.valueChanged.connect(self._emit_settings)

        # выбранная пауза: своя длина
        self.sel_lbl = QLabel()
        self.sel_len = QDoubleSpinBox(minimum=0.1, maximum=30.0, singleStep=0.1, decimals=1, suffix=" с")
        self.sel_len.setToolTip("Сколько тишины оставить от этой паузы")
        short_btn = theme.mark(QPushButton("Укоротить"), "ghost")
        short_btn.clicked.connect(lambda: self.pause_length.emit(round(self.sel_len.value(), 2)))
        whole_btn = theme.mark(QPushButton("Целиком"), "ghost")
        whole_btn.setToolTip("Оставить паузу как есть")
        whole_btn.clicked.connect(lambda: self.pause_length.emit(-1.0))
        self.sel_box = QWidget()
        srow = QHBoxLayout(self.sel_box)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(self.sel_lbl)
        srow.addWidget(self.sel_len)
        srow.addWidget(short_btn)
        srow.addWidget(whole_btn)
        srow.addStretch(1)
        self.sel_box.setVisible(False)

        self.pause_box = QWidget()
        pl = QVBoxLayout(self.pause_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        pl.addLayout(prow)
        pl.addLayout(prow2)
        pl.addWidget(self.sel_box)

        # слова-паразиты
        self.filler_list = QListWidget()
        self.filler_list.setMaximumHeight(150)
        self.filler_list.setToolTip("Отметьте слова, которые убрать. Найденные подчёркнуты в тексте.")
        self.filler_add = QLineEdit()
        self.filler_add.setPlaceholderText("Своё слово, например «типа»")
        self.filler_add.returnPressed.connect(self._add_filler)
        add_btn = theme.mark(QPushButton("Добавить"), "ghost")
        add_btn.clicked.connect(self._add_filler)
        del_btn = theme.mark(QPushButton("Убрать из списка"), "ghost")
        del_btn.clicked.connect(self._remove_filler)
        cut_btn = theme.mark(QPushButton("Вырезать отмеченные"), "primary")
        cut_btn.clicked.connect(lambda: self.fillers_cut.emit(self.checked_fillers()))
        back_btn = theme.mark(QPushButton("Вернуть"), "ghost")
        back_btn.setToolTip("Вернуть все вырезанные отмеченные слова")
        back_btn.clicked.connect(lambda: self.fillers_restore.emit(self.checked_fillers()))
        arow = QHBoxLayout()
        arow.addWidget(self.filler_add, 1)
        arow.addWidget(add_btn)
        brow = QHBoxLayout()
        brow.addWidget(cut_btn)
        brow.addWidget(back_btn)
        brow.addStretch(1)
        brow.addWidget(del_btn)
        self.filler_box = QWidget()
        fl = QVBoxLayout(self.filler_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        fl.addWidget(self.filler_list)
        fl.addLayout(arow)
        fl.addLayout(brow)

        self.subs = QCheckBox("Субтитры на видео из этого текста")
        self.subs.setToolTip("Субтитры берутся из расшифровки и сами меняются, когда вы что-то вырезаете\n"
                             "или исправляете слово (двойной щелчок по слову). Вид — как у любых субтитров.")
        self.subs.toggled.connect(lambda on: None if self._loading else self.subtitles_toggled.emit(on))
        from glimpsy.editor import translate as mt
        self.subs_lang = QComboBox()
        self.subs_lang.addItem("на языке речи", "")
        for code, (name, _a, _b) in mt.LANGS.items():
            self.subs_lang.addItem("перевод: " + name, code)
        self.subs_lang.setToolTip("Субтитры на другом языке: перевод делается прямо на компьютере, без интернета\n"
                                  "(переводчик скачивается один раз). Файл .srt сохраняется на обоих языках.")
        self.subs_lang.currentIndexChanged.connect(
            lambda _i: None if self._loading else self.subs_lang_changed.emit(self.subs_lang.currentData() or ""))
        self.voice_status = QLabel()
        self.voice_status.setProperty("role", "hint")
        self.voice_status.setWordWrap(True)
        self.voice_status.setVisible(False)
        self.subs_status = QLabel()
        self.subs_status.setProperty("role", "hint")
        self.subs_status.setVisible(False)
        subs_row = QHBoxLayout()
        subs_row.addWidget(self.subs)
        subs_row.addWidget(self.subs_lang, 1)
        self.stats = QLabel()
        self.stats.setProperty("role", "hint")
        self.view = TranscriptView()
        # что сделать с выделенным текстом
        act = QHBoxLayout()
        act.setSpacing(4)
        for key, text, tip in (
                ("cut", "Вырезать", "Убрать и звук, и картинку (Delete)"),
                ("mute", "Без звука", "Картинка идёт, звук выключен. Ещё раз — вернуть звук"),
                ("hide", "Без картинки", "Звук идёт, вместо картинки — чёрный кадр (сверху можно положить "
                                         "своё фото или видео). Ещё раз — вернуть картинку"),
                ("restore", "Вернуть", "Вернуть выделенному и звук, и картинку"),
                ("respeak", "Переозвучить…", "Исправить фразу и озвучить её вашим голосом (без интернета)")):
            b = theme.mark(QPushButton(text), "ghost")
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)          # выделение в тексте не пропадает
            b.clicked.connect(lambda _=False, k=key: self.selection_action.emit(k))
            act.addWidget(b)
        act.addStretch(1)
        self.hint = QLabel()
        self.hint.setProperty("role", "hint")
        self.hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(6)
        lay.addLayout(head)
        lay.addWidget(self.transcribe_btn)
        lay.addLayout(prog)
        lay.addWidget(self.bar_text)
        lay.addWidget(self.mode)
        lay.addWidget(self.pause_box)
        lay.addWidget(self.filler_box)
        lay.addLayout(subs_row)
        lay.addWidget(self.subs_status)
        lay.addWidget(self.voice_status)
        lay.addWidget(self.stats)
        lay.addLayout(act)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.hint)
        self.setMinimumWidth(380)
        self.setMaximumWidth(560)
        self.set_progress(None)
        self._show_mode("auto")
        self.setVisible(False)

    def set_open(self, on: bool) -> None:
        self.setVisible(on)

    @property
    def mode_key(self) -> str:
        return self.mode.currentData() or "auto"

    def _show_mode(self, key: str) -> None:
        self.pause_box.setVisible(key in ("auto", "manual"))
        self.filler_box.setVisible(key == "fillers")
        self.pause_lbl.setText("Вырезать паузы длиннее" if key == "auto" else "Показывать паузы длиннее")
        common = "Щелчок по слову — перейти, двойной — исправить. Выделите текст и Delete — вырезать, или " \
                 "кнопки над текстом: синий курсив — без звука, фиолетовый фон — без картинки, зелёный — " \
                 "переозвучено вашим голосом. " \
                 "Щелчок по зачёркнутому — вернуть. Пробел — пуск/пауза, Ctrl+Z — отменить."
        extra = {
            "auto": "Длинные паузы вырезаются сами. Щелчок по паузе — оставить её; правая кнопка — своя длина.",
            "manual": "Паузы сами не режутся. Щелчок по паузе — вырезать её (ещё раз — вернуть); "
                      "правая кнопка или поле сверху — укоротить до своей длины.",
            "fillers": "Найденные слова подчёркнуты оранжевым. Отметьте нужные в списке и нажмите "
                       "«Вырезать отмеченные». Вернуть одно — щёлкните по нему в тексте.",
        }[key]
        self.hint.setText(extra + " " + common)

    def _on_mode(self) -> None:
        self._show_mode(self.mode_key)
        self.sel_box.setVisible(False)
        self._emit_settings()

    def set_settings(self, cuts: dict) -> None:
        self._loading = True
        mode = cuts.get("mode") or ("auto" if cuts["pause_cut"] else "manual")
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self.pause_min.setValue(float(cuts["pause_min"]))
        self.pad.setValue(float(cuts["pad"]))
        self.subs.setChecked(bool(cuts.get("subtitles")))
        self.subs_lang.setCurrentIndex(max(0, self.subs_lang.findData(cuts.get("subs_lang") or "")))
        self._show_mode(mode)
        self._loading = False

    def _emit_settings(self) -> None:
        if self._loading:
            return
        mode = self.mode_key
        values = {"mode": mode, "pause_min": round(self.pause_min.value(), 2), "pad": round(self.pad.value(), 3)}
        if mode in ("auto", "manual"):
            values["pause_cut"] = mode == "auto"
        self.settings_changed.emit(values)

    # ---------- выбранная пауза ----------

    def show_pause(self, gap: float | None, keep: float | None = None) -> None:
        """Показать поле «своя длина» для паузы (gap=None — спрятать)."""
        self.sel_box.setVisible(gap is not None and self.mode_key in ("auto", "manual"))
        if gap is not None:
            self.sel_lbl.setText(f"Пауза {_sec(gap)} с — оставить")
            self.sel_len.setMaximum(max(0.1, round(gap, 1)))
            self.sel_len.setValue(keep if keep is not None and keep > 0 else min(0.5, gap))

    # ---------- слова-паразиты ----------

    def set_fillers(self, fillers: list[str], counts: dict[str, tuple[int, int]]) -> None:
        """counts: слово → (найдено, из них вырезано). Отметки сохраняем."""
        checked = set(self.checked_fillers()) if self._fillers else set(fillers)
        self._fillers = list(fillers)
        self.filler_list.blockSignals(True)
        self.filler_list.clear()
        for f in fillers:
            found, gone = counts.get(f, (0, 0))
            text = f"{f}   — {found}" + (f" (вырезано {gone})" if gone else "")
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if f in checked else Qt.CheckState.Unchecked)
            if not found:
                it.setForeground(COL_GONE)
            self.filler_list.addItem(it)
        self.filler_list.blockSignals(False)

    def checked_fillers(self) -> list[str]:
        out = []
        for n in range(self.filler_list.count()):
            it = self.filler_list.item(n)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _add_filler(self) -> None:
        word = " ".join(self.filler_add.text().strip().lower().split())
        self.filler_add.clear()
        if word and tr.norm_word(word) and word not in self._fillers:
            self.fillers_changed.emit(self._fillers + [word])

    def _remove_filler(self) -> None:
        it = self.filler_list.currentItem()
        if it is not None:
            word = it.data(Qt.ItemDataRole.UserRole)
            self.fillers_changed.emit([f for f in self._fillers if f != word])

    # ---------- прочее ----------

    def set_voice_status(self, text: str) -> None:
        self.voice_status.setText(text)
        self.voice_status.setVisible(bool(text))

    def set_subs_status(self, text: str) -> None:
        self.subs_status.setText(text)
        self.subs_status.setVisible(bool(text))

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
