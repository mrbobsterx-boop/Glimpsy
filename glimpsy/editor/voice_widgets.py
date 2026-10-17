"""Для записи озвучки: суфлёр (текст сценария рядом с кнопкой «Стоп») и немой черновик ролика.

Суфлёр показывает script.md / .txt крупно, абзацами. Пометки в квадратных скобках
([НА ЭКРАНЕ: …], [СНЯТЬ: …], [ПАУЗА]) — мелко и серым, заголовки «#» — жирным. Текущий абзац
подсвечен: стрелки ↑/↓ (или щелчок по абзацу) — какой читаете сейчас, колёсико — прокрутка.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

CURRENT_BG = "#22303A"


def split_script(text: str) -> list[str]:
    """Сценарий → абзацы (пустая строка — новый абзац)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]


def paragraph_html(par: str) -> str:
    """Абзац → HTML: [пометки] мелко и серым, «# заголовок» — жирным, **жирный** — жирным."""
    lines = []
    for line in par.split("\n"):
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        body = html.escape(heading.group(1) if heading else line)
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
        body = re.sub(r"\[([^\]]*)\]", r'<span style="color:#7C8494; font-size:13px">[\1]</span>', body)
        lines.append(f"<b>{body}</b>" if heading else body)
    return "<br>".join(lines)


class ScriptView(QTextBrowser):
    """Крупный текст сценария с отметкой «где я сейчас»."""

    position_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paragraphs: list[str] = []
        self.current = 0
        self.setOpenLinks(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("QTextBrowser { background: #0F1217; border: 1px solid #262B36; border-radius: 10px;"
                           " padding: 10px; font-size: 22px; line-height: 140%; }")
        self.setMinimumSize(420, 320)

    def load_text(self, text: str, pos: int = 0) -> None:
        self.paragraphs = split_script(text)
        self.current = max(0, min(pos, len(self.paragraphs) - 1)) if self.paragraphs else 0
        self._render()

    def load_file(self, path: Path, pos: int = 0) -> None:
        self.load_text(Path(path).read_text(encoding="utf-8", errors="replace"), pos)

    def _render(self) -> None:
        parts = []
        for i, p in enumerate(self.paragraphs):
            style = f"background:{CURRENT_BG};" if i == self.current else "color:#9AA3B2;"
            parts.append(f'<p style="{style} margin:0 0 14px 0; padding:6px"><a name="p{i}"></a>'
                         f"{paragraph_html(p)}</p>")
        self.setHtml("".join(parts) or "<p style='color:#7C8494'>Текст пуст.</p>")
        self.scrollToAnchor(f"p{max(0, self.current - 1)}")

    def set_current(self, i: int) -> None:
        if not self.paragraphs:
            return
        i = max(0, min(i, len(self.paragraphs) - 1))
        if i != self.current:
            self.current = i
            self._render()
            self.position_changed.emit(i)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self.set_current(self.current + 1)
        elif e.key() in (Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.set_current(self.current - 1)
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e) -> None:
        cur: QTextCursor = self.cursorForPosition(e.position().toPoint())
        block = cur.block().blockNumber()
        if 0 <= block < len(self.paragraphs):
            self.set_current(block)
        super().mousePressEvent(e)


class DraftView(QWidget):
    """Немой черновик ролика (MP4 из VideoStudio): идёт вместе с записью, с места курсора."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        self.video = QVideoWidget(self)
        self.video.setMinimumSize(320, 180)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.video)
        self.path = ""

    def load(self, path: str, at: float = 0.0) -> None:
        self.path = path
        self.player.setSource(QUrl.fromLocalFile(path))
        self.seek(at)

    def seek(self, t: float) -> None:
        self.player.setPosition(int(max(0.0, t) * 1000))

    def play(self) -> None:
        if self.path:
            self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def stop(self) -> None:
        self.player.stop()
