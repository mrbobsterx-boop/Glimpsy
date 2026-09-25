"""Внешний вид Glimpsy: тёмная тема в духе монтажных программ, шрифт Inter, иконки Lucide.

Все цвета — здесь, в одном месте (токены). Окна используют роли вместо цветов:
    button.setProperty("role", "primary")   — главная кнопка (бирюзовая)
    label.setProperty("role", "muted")      — второстепенный текст
    frame.setProperty("role", "card")       — карточка-подложка
"""

from __future__ import annotations

import logging
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

from glimpsy import paths

log = logging.getLogger(__name__)

# ---------------- токены ----------------
BG = "#0D0F13"          # фон окна
SURFACE = "#14171D"     # панели
RAISED = "#1B1F27"      # поля ввода, карточки
HOVER = "#232834"
BORDER = "#262B36"
BORDER_STRONG = "#343A48"
TEXT = "#E9EBF1"
MUTED = "#8E95A5"
FAINT = "#5D6474"
ACCENT = "#22AEBB"      # бирюзовый — фирменный цвет Glimpsy
ACCENT_HOVER = "#2BC2CF"
ACCENT_DOWN = "#1B8E99"
ACCENT_SOFT = "rgba(34,174,187,0.16)"
DANGER = "#F0565B"
WARN = "#F5A524"
FONT = "Inter"


def load_fonts() -> str:
    """Подключить Inter из assets/fonts. Возвращает имя семейства (или системное, если не вышло)."""
    family = ""
    for f in ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf"):
        fid = QFontDatabase.addApplicationFont(str(paths.asset(f"fonts/{f}")))
        if fid >= 0 and not family:
            fams = QFontDatabase.applicationFontFamilies(fid)
            family = fams[0] if fams else ""
    return family


# ---------------- иконки ----------------

@lru_cache(maxsize=512)
def _svg(name: str) -> bytes:
    try:
        return paths.asset(f"icons/{name}.svg").read_bytes()
    except OSError:
        log.warning("Нет иконки %s", name)
        return b""


def icon(name: str, color: str = TEXT, size: int = 20, stroke: float = 1.75) -> QIcon:
    """Иконка Lucide нужного цвета. Для неактивного состояния — приглушённая копия."""
    ic = QIcon()
    for mode, col in ((QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, FAINT), (QIcon.Mode.Active, color)):
        for scale in (1, 2):
            ic.addPixmap(pixmap(name, col, size * scale, stroke), mode)
    return ic


def pixmap(name: str, color: str = TEXT, size: int = 20, stroke: float = 1.75) -> QPixmap:
    data = _svg(name).replace(b"currentColor", color.encode()).replace(b'stroke-width="2"',
                                                                      f'stroke-width="{stroke}"'.encode())
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    if data:
        r = QSvgRenderer(QByteArray(data))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r.render(p, QRectF(0, 0, size, size))
        p.end()
    return pm


# ---------------- стили ----------------

def stylesheet() -> str:
    return f"""
* {{ font-family: "{FONT}"; }}
QWidget {{ color: {TEXT}; font-size: 13px; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#surface, QFrame[role="panel"] {{ background: {SURFACE}; }}
QFrame[role="card"] {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame[role="divider"] {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role="hint"] {{ color: {FAINT}; font-size: 11px; }}
QLabel[role="title"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="h1"] {{ font-size: 20px; font-weight: 700; }}
QLabel[role="section"] {{ color: {MUTED}; font-size: 12px; font-weight: 600; padding-top: 12px;
    padding-bottom: 2px; }}
QLabel[role="kbd"] {{ background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-bottom-width: 2px;
    border-radius: 5px; padding: 1px 6px; font-size: 11px; color: {TEXT}; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; border-radius: 6px;
    padding: 5px 8px; }}

/* ---------- кнопки ---------- */
QPushButton {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 14px; }}
QPushButton:hover {{ background: {HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {FAINT}; background: {SURFACE}; }}
QPushButton:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT_HOVER}; }}
QPushButton[role="primary"] {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #27BFCB, stop:1 #1A8F9D);
    border: none; color: #FFFFFF; font-weight: 600; padding: 7px 18px; }}
QPushButton[role="primary"]:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #34CEDA, stop:1 #1FA0AE); }}
QPushButton[role="primary"]:pressed {{ background: {ACCENT_DOWN}; }}
QPushButton[role="primary"]:disabled {{ background: {RAISED}; color: {FAINT}; }}
QPushButton[role="ghost"] {{ background: transparent; border: 1px solid transparent; }}
QPushButton[role="ghost"]:hover {{ background: {HOVER}; }}
QPushButton[role="danger"] {{ color: {DANGER}; }}
QPushButton[role="danger"]:hover {{ background: rgba(240,86,91,0.12); border-color: {DANGER}; }}

QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 5px; }}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:pressed {{ background: {BORDER}; }}
QToolButton:checked {{ background: {ACCENT_SOFT}; color: {ACCENT_HOVER}; }}
QToolButton:disabled {{ color: {FAINT}; }}
QToolButton[role="rail"] {{ padding: 8px 2px 6px 2px; font-size: 11px; color: {MUTED}; border-radius: 10px; }}
QToolButton[role="rail"]:hover {{ color: {TEXT}; background: {HOVER}; }}
QToolButton[role="rail"]:checked {{ color: {ACCENT_HOVER}; background: {ACCENT_SOFT}; }}
QToolButton[role="play"] {{ background: {TEXT}; border-radius: 18px; padding: 8px; }}
QToolButton[role="play"]:hover {{ background: #FFFFFF; }}

/* ---------- поля ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {ACCENT_DOWN}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QPlainTextEdit:hover {{
    border-color: {BORDER_STRONG}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {FAINT}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 16px; border: none; background: transparent; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({_icon_url("chevron-up-small")}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({_icon_url("chevron-down-small")}); width: 10px; height: 10px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({_icon_url("chevron-down-small")}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 4px; outline: none; selection-background-color: {HOVER}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {BORDER_STRONG};
    background: {RAISED}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
    image: url({_icon_url("check-small")}); }}
QCheckBox:disabled {{ color: {FAINT}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER_STRONG}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: #FFFFFF; }}

QProgressBar {{ background: {RAISED}; border: none; border-radius: 4px; height: 8px; text-align: center;
    color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

/* ---------- списки, меню, вкладки ---------- */
QListWidget, QTreeView, QListView {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border-radius: 10px; padding: 6px; margin: 2px 0; }}
QListWidget::item:hover {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}

QMenu {{ background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {HOVER}; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 8px; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {FAINT}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:hover {{ background: {BORDER}; }}
QStatusBar {{ background: {SURFACE}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QMessageBox QLabel {{ min-width: 260px; }}
"""


_ICON_DIR = None


def _icon_url(name: str) -> str:
    """Маленькие служебные иконки (стрелки, галочка) — PNG во временной папке, чтобы QSS мог их взять."""
    global _ICON_DIR
    if _ICON_DIR is None:
        _ICON_DIR = paths.temp_root() / "theme"
        _ICON_DIR.mkdir(parents=True, exist_ok=True)
    base, _, _small = name.partition("-small")
    color = "#FFFFFF" if base == "check" else MUTED
    f = _ICON_DIR / f"{name}.png"
    if not f.exists():
        pixmap(base, color, 32, 2.5).save(str(f))
    return f.as_posix()


class _Style(QProxyStyle):
    """Fusion без стандартных значков на кнопках «Сохранить/Отмена» (дискета, крестик)."""

    def styleHint(self, hint, option=None, widget=None, data=None) -> int:
        if hint == QStyle.StyleHint.SH_DialogButtonBox_ButtonsHaveIcons:
            return 0
        return super().styleHint(hint, option, widget, data)


def apply(app: QApplication) -> None:
    """Включить тему для всей программы."""
    family = load_fonts()
    global FONT
    if family:
        FONT = family
    app.setStyle(_Style(QStyleFactory.create("Fusion")))   # одинаковый вид на Windows, macOS и Linux
    pal = QPalette()
    for role, col in (
        (QPalette.ColorRole.Window, BG), (QPalette.ColorRole.WindowText, TEXT), (QPalette.ColorRole.Base, RAISED),
        (QPalette.ColorRole.AlternateBase, SURFACE), (QPalette.ColorRole.Text, TEXT),
        (QPalette.ColorRole.Button, RAISED), (QPalette.ColorRole.ButtonText, TEXT),
        (QPalette.ColorRole.Highlight, ACCENT_DOWN), (QPalette.ColorRole.HighlightedText, "#FFFFFF"),
        (QPalette.ColorRole.ToolTipBase, RAISED), (QPalette.ColorRole.ToolTipText, TEXT),
        (QPalette.ColorRole.PlaceholderText, FAINT), (QPalette.ColorRole.Link, ACCENT_HOVER),
        (QPalette.ColorRole.Mid, BORDER), (QPalette.ColorRole.Dark, BG), (QPalette.ColorRole.Light, HOVER),
    ):
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(FAINT))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(FAINT))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(FAINT))
    app.setPalette(pal)
    f = QFont(FONT)
    f.setPixelSize(13)
    app.setFont(f)
    app.setStyleSheet(stylesheet())


def mark(widget, role: str):
    """Назначить виджету роль стиля (см. начало файла). Возвращает сам виджет — удобно в цепочках."""
    widget.setProperty("role", role)
    return widget


def icon_size(px: int) -> QSize:
    return QSize(px, px)
