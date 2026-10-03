"""Обучение по редактору (кнопка «?»): по очереди подсвечивает части окна и объясняет, что это.

Поверх окна — полупрозрачное затемнение с «окошком» вокруг нужной кнопки или панели и
карточка с объяснением: «Назад», «Далее», «Закончить» (или Esc). Шаги, у которых в этом
проекте нет кнопки (например, «Текст» есть только в монтаже по тексту), пропускаются.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from glimpsy.ui import theme

# (что подсветить, заголовок, объяснение)
STEPS: list[tuple[str, str, str]] = [
    ("", "Добро пожаловать в редактор Glimpsy",
     "Сейчас покажу, что где находится и для чего нужно. Это займёт пару минут.\n\n"
     "Листать — «Далее» и «Назад» (или стрелки на клавиатуре). Закрыть — «Закончить» или Esc. "
     "Вернуться к обучению можно в любой момент — кнопка «?» справа вверху."),
    ("back", "Все записи",
     "Список всех ваших записей и проектов. Проект сохраняется сам — можно спокойно уходить."),
    ("format", "Формат ролика",
     "16:9 — горизонтальный, для YouTube. 9:16 — вертикальный, для Shorts, Reels и TikTok. "
     "Для каждого формата своё положение кадра и текстов — переключайтесь и смотрите."),
    ("montage", "Автомонтаж",
     "Одной кнопкой: приближение к кликам, темп, наезды, склейки под музыку, вертикальная версия. "
     "Всё, что сделает автомонтаж, потом можно поправить руками."),
    ("export", "Экспорт — сохранить ролик",
     "Готовые варианты: для YouTube, для Shorts/Reels/TikTok, для Telegram (файл поменьше) или оба "
     "формата сразу. Здесь же — «Главы для YouTube», «Обложка» и «Быстрое сохранение»."),
    ("tool:Текст", "Монтаж по тексту",
     "Расшифровка вашей речи. Монтируете как в документе: выделили слова и Delete — они вырезаны из видео. "
     "Здесь же — паузы, слова-паразиты, неудачные дубли и субтитры из текста."),
    ("tool:Файлы", "Файлы",
     "Видео, фото и музыка с компьютера под рукой — перетаскивайте прямо на ленту."),
    ("tool:Медиа", "Медиа",
     "Видео или фото на весь кадр поверх ролика — с места курсора на ленте (клавиша M)."),
    ("tool:Текст+", "Добавить текст",
     "Надпись в месте курсора (клавиша T). Двигается мышью в просмотре, справа — шрифт, цвет, подложка, "
     "анимация и подсветка слов (караоке)."),
    ("tool:Субтитры", "Субтитры",
     "Все субтитры списком: распознать речь, исправить текст, удалить лишнее. Вид субтитров — справа."),
    ("tool:Наложение", "Наложение",
     "Картинка или видео поверх ролика — например, окошко с веб-камерой. У видео с камеры можно убрать "
     "или размыть фон за вами."),
    ("tool:Музыка", "Музыка",
     "Фоновая музыка на весь ролик: громкость, плавное начало и конец, сама тише, когда вы говорите."),
    ("tool:Улучшить", "Улучшить",
     "Чистый голос (убрать шум), одинаковая громкость, стабилизация дрожащей съёмки, яркость и цвет. "
     "Можно послушать и посмотреть «как было / как будет»."),
    ("tool:До/после", "Было → стало",
     "Вставка-сравнение: шторка, ускоренный показ (таймлапс) или стоп-кадр."),
    ("tool:Запись", "Запись",
     "Записать голос, камеру или то и другое прямо в ролик — с места курсора."),
    ("tool:Папки", "Папки",
     "Где лежат готовые ролики, проекты, скачанные модели и журналы — открыть одним нажатием."),
    ("tool:Клавиши", "Горячие клавиши",
     "Список всех клавиш редактора. Самые нужные: Пробел — пуск/пауза, S — разрезать, Delete — удалить, "
     "Ctrl+Z — отменить."),
    ("preview", "Просмотр",
     "Здесь виден ролик таким, каким он сохранится. Тексты и наложения можно двигать мышью, "
     "размер — за уголки или колёсиком."),
    ("play", "Пуск и пауза",
     "Запустить или остановить просмотр (Пробел). Слева — время, справа — громкость просмотра."),
    ("side", "Настройки справа",
     "Показывает настройки того, что выбрано: куска видео, текста, наложения или музыки. "
     "Выберите что-нибудь на ленте — здесь появятся его настройки."),
    ("edit", "Отменить, разрезать, удалить",
     "Отменить и повторить действие, разрезать кусок в месте курсора (S) и удалить выбранное (Delete)."),
    ("tracks", "Дорожки",
     "Добавить дорожку для текстов, субтитров, наложений, медиа, камеры или голоса — сколько угодно. "
     "Правый щелчок по дорожке — ещё действия."),
    ("timeline", "Лента",
     "Весь ролик по времени: куски видео, тексты, наложения, музыка. Щелчок — перейти к месту, "
     "куски можно двигать и тянуть за края. Ctrl+колёсико — крупнее или мельче."),
    ("help", "Вот и всё!",
     "Если что-то забудете — нажмите «?», обучение начнётся заново. Удачного монтажа!"),
]


class TourOverlay(QWidget):
    """Затемнение с подсветкой + карточка с объяснением. targets: ключ шага → виджет (или None)."""

    finished = Signal()

    def __init__(self, parent: QWidget, targets: Callable[[str], QWidget | None]) -> None:
        super().__init__(parent)
        self._targets = targets
        self.steps = [s for s in STEPS if not s[0] or self._visible(s[0])]
        self.i = 0
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.card = QFrame(self)
        self.card.setObjectName("tourCard")
        self.card.setStyleSheet("QFrame#tourCard { background: #1C2029; border: 1px solid #3A4152;"
                                " border-radius: 12px; }")
        self.card.setFixedWidth(380)
        self.counter = theme.mark(QLabel(), "muted")
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 16px; font-weight: 700;")
        self.title.setWordWrap(True)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.b_back = theme.mark(QPushButton("Назад"), "ghost")
        self.b_next = theme.mark(QPushButton("Далее"), "primary")
        self.b_close = theme.mark(QPushButton("Закончить"), "ghost")
        self.b_back.clicked.connect(lambda: self.go(self.i - 1))
        self.b_next.clicked.connect(lambda: self.go(self.i + 1))
        self.b_close.clicked.connect(self.close_tour)
        row = QHBoxLayout()
        row.addWidget(self.b_close)
        row.addStretch(1)
        row.addWidget(self.b_back)
        row.addWidget(self.b_next)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)
        lay.addWidget(self.counter)
        lay.addWidget(self.title)
        lay.addWidget(self.text)
        lay.addLayout(row)
        parent.installEventFilter(self)

    def _visible(self, key: str) -> bool:
        w = self._targets(key)
        return w is not None and w.isVisible()

    # ---------- шаги ----------

    def start(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.setFocus()
        self.go(0)

    def go(self, i: int) -> None:
        if i >= len(self.steps):
            self.close_tour()
            return
        self.i = max(0, i)
        _key, title, text = self.steps[self.i]
        self.counter.setText(f"{self.i + 1} из {len(self.steps)}")
        self.title.setText(title)
        self.text.setText(text)
        self.b_back.setEnabled(self.i > 0)
        self.b_next.setText("Готово" if self.i + 1 == len(self.steps) else "Далее")
        self._place()
        self.update()

    def close_tour(self) -> None:
        self.parentWidget().removeEventFilter(self)
        self.hide()
        self.finished.emit()
        self.deleteLater()

    def target_rect(self) -> QRect | None:
        key = self.steps[self.i][0]
        w = self._targets(key) if key else None
        if w is None or not w.isVisible():
            return None
        top_left = w.mapTo(self.parentWidget(), QPoint(0, 0))
        return QRect(top_left, w.size()).adjusted(-6, -6, 6, 6)

    def _place(self) -> None:
        """Карточку — рядом с подсвеченным местом, чтобы не закрывала его."""
        self.card.adjustSize()
        W, H = self.width(), self.height()
        cw, ch = self.card.width(), self.card.sizeHint().height()
        r = self.target_rect()
        if r is None:
            x, y = (W - cw) // 2, (H - ch) // 2
        else:
            gap = 14
            if r.right() + gap + cw < W:                        # справа
                x, y = r.right() + gap, r.top()
            elif r.left() - gap - cw > 0:                       # слева
                x, y = r.left() - gap - cw, r.top()
            elif r.bottom() + gap + ch < H:                     # снизу
                x, y = r.left(), r.bottom() + gap
            else:                                               # сверху
                x, y = r.left(), r.top() - gap - ch
            x = max(10, min(W - cw - 10, x))
            y = max(10, min(H - ch - 10, y))
        self.card.setGeometry(x, y, cw, ch)

    # ---------- рисование и клавиши ----------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        shade = QPainterPath()
        shade.addRect(QRectF(self.rect()))
        r = self.target_rect()
        if r is not None:
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(r), 10, 10)
            shade = shade.subtracted(hole)
        p.fillPath(shade, QColor(8, 10, 14, 175))
        if r is not None:
            p.setPen(QPen(QColor(theme.ACCENT_HOVER), 2))
            p.drawRoundedRect(QRectF(r), 10, 10)
        p.end()

    def mousePressEvent(self, e) -> None:
        e.accept()                                  # пока идёт обучение, окно под ним не нажимается

    def keyPressEvent(self, e: QKeyEvent) -> None:
        k = e.key()
        if k == Qt.Key.Key_Escape:
            self.close_tour()
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.go(self.i + 1)
        elif k == Qt.Key.Key_Left:
            self.go(self.i - 1)
        else:
            super().keyPressEvent(e)

    def eventFilter(self, obj, e) -> bool:
        if obj is self.parentWidget() and e.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
            self._place()
        return False
