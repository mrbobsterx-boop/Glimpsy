"""Статистика сессии: сколько работали, где, сколько кликов. Только для вас — в ролик не попадает."""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

ACCENT = "#1f6f78"
COLORS = {"active_s": "#1f9aa6", "idle_s": "#f5a524", "paused_s": "#8b8d98", "private_s": "#8e4ec6"}
LABELS = {"active_s": "Работа", "idle_s": "Не было за компьютером", "paused_s": "Пауза", "private_s": "Приватные приложения"}


def load_stats(project_dir: Path) -> dict | None:
    try:
        return json.loads((project_dir / "stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = s // 3600, s % 3600 // 60
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин"
    return f"{s} с"


class _Bar(QWidget):
    """Полоска долей: [(значение, цвет)]."""

    def __init__(self, parts: list[tuple[float, str]], height: int = 10) -> None:
        super().__init__()
        self.parts = parts
        self.setFixedHeight(height)
        self.setMinimumWidth(120)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(127, 127, 127, 40))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        total = sum(v for v, _ in self.parts) or 1
        x = r.x()
        p.setClipRect(r)
        for v, c in self.parts:
            w = r.width() * v / total
            if w > 0:
                p.setBrush(QColor(c))
                p.drawRoundedRect(QRectF(x, r.y(), w, r.height()), r.height() / 2, r.height() / 2)
            x += w


def _big(value: str, caption: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    a = QLabel(value)
    a.setStyleSheet("font-size: 22px; font-weight: 700;")
    b = QLabel(caption)
    b.setStyleSheet("color: #8b8d98; font-size: 11px;")
    v.addWidget(a)
    v.addWidget(b)
    return w


class StatsDialog(QDialog):
    def __init__(self, project, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Статистика — {project.name}")
        self.setMinimumWidth(460)
        st = load_stats(project.dir)
        lay = QVBoxLayout(self)
        if not st:
            msg = QLabel("Статистики у этой сессии нет — она появляется у записей, сделанных в новой версии "
                         "Glimpsy.")
            msg.setWordWrap(True)
            lay.addWidget(msg)
        else:
            lay.addWidget(self._content(st, project))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _content(self, st: dict, project) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        start, end = st.get("start", 0), st.get("end", 0)
        when = time.strftime("%d.%m.%Y, %H:%M", time.localtime(start)) if start else ""
        until = time.strftime("%H:%M", time.localtime(end)) if end else ""
        head = QLabel(f"{when} — {until}" if when else "Сессия")
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(head)
        note = QLabel("Только для вас — в ролик это не попадает.")
        note.setStyleSheet("color: #8b8d98; font-size: 11px;")
        v.addWidget(note)

        # --- крупные цифры ---
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        meters = st.get("mouse_px", 0) / 3780          # ≈ 96 точек на дюйм
        cells = [
            (fmt_duration(st.get("active_s", 0)), "активной работы"),
            (f"{st.get('clicks', 0):,}".replace(",", " "), "кликов"),
            (f"{st.get('keys', 0):,}".replace(",", " "), "нажатий клавиш"),
            (f"{meters:.0f} м" if meters >= 1 else f"{meters * 100:.0f} см", "проехала мышь"),
            (str(st.get("important", 0)), "важных моментов ⭐"),
            (fmt_duration(project.total), f"ролик · {len(project.clips)} фрагм."),
        ]
        for i, (a, b) in enumerate(cells):
            grid.addWidget(_big(a, b), i // 3, i % 3)
        v.addSpacing(8)
        v.addLayout(grid)

        # --- на что ушло время ---
        v.addSpacing(14)
        t = QLabel("На что ушло время")
        t.setStyleSheet("font-weight: 600;")
        v.addWidget(t)
        keys = [k for k in ("active_s", "idle_s", "paused_s", "private_s") if st.get(k)]
        v.addWidget(_Bar([(st.get(k, 0), COLORS[k]) for k in keys], 12))
        legend = QGridLayout()
        for i, k in enumerate(keys):
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {COLORS[k]};")
            legend.addWidget(dot, i, 0)
            legend.addWidget(QLabel(LABELS[k]), i, 1)
            val = QLabel(fmt_duration(st[k]))
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            legend.addWidget(val, i, 2)
        legend.setColumnStretch(1, 1)
        v.addLayout(legend)

        # --- программы ---
        apps = sorted(((a, s) for a, s in st.get("apps", {}).items() if s > 0), key=lambda kv: -kv[1])
        if apps:
            v.addSpacing(14)
            t = QLabel("Программы")
            t.setStyleSheet("font-weight: 600;")
            v.addWidget(t)
            top = apps[0][1] or 1
            g = QGridLayout()
            g.setVerticalSpacing(6)
            for i, (name, sec) in enumerate(apps[:8]):
                g.addWidget(QLabel(name), i, 0)
                g.addWidget(_Bar([(sec, ACCENT), (top - sec, "transparent")], 8), i, 1)
                val = QLabel(fmt_duration(sec))
                val.setAlignment(Qt.AlignmentFlag.AlignRight)
                g.addWidget(val, i, 2)
            g.setColumnStretch(1, 1)
            v.addLayout(g)
        v.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        scroll.setMinimumHeight(420)
        return scroll
