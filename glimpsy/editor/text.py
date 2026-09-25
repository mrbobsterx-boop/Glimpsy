"""Тексты (субтитры и подписи) поверх ролика.

Текст рисуется средствами Qt в картинку с прозрачным фоном. Эта же картинка
показывается в окне просмотра и накладывается на видео при экспорте, поэтому
результат совпадает один в один: тот же шрифт, размер, подложка.

Стиль задаётся один раз для всего проекта («общий»). У отдельного текста можно
включить «свой стиль» — тогда он меняется независимо от остальных.
"""

from __future__ import annotations

import copy
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPainterPath, QPen

from glimpsy import paths

log = logging.getLogger(__name__)

ANIM_DURATION = 0.3
ANIMATIONS = {"none": "Без анимации", "fade": "Плавное появление", "slide": "Выезд снизу", "pop": "Всплытие"}
DEFAULT_POS = (0.5, 0.85)       # центр текста: по центру, внизу кадра

DEFAULT_STYLE = {
    "font": "",                 # пусто — системный шрифт по умолчанию
    "size": 0.055,              # высота букв в долях меньшей стороны кадра
    "color": "#ffffff",
    "bold": True,
    "italic": False,
    "bg": True,                 # подложка (плашка) под текстом
    "bg_color": "#000000",
    "bg_opacity": 0.55,
    "bg_radius": 0.35,          # скругление подложки в долях высоты строки
    "shadow": False,
    "anim": "fade",
}


@dataclass
class TextItem:
    id: str
    text: str
    start: float                 # когда появляется (секунды ролика)
    duration: float = 2.5
    style: dict | None = None    # None — общий стиль проекта
    pos: dict = field(default_factory=dict)   # {"9:16": [x, y]} — центр текста в долях кадра
    auto: bool = False           # создан автосубтитрами (их можно пересоздать или убрать разом)

    @property
    def end(self) -> float:
        return self.start + self.duration

    def pos_for(self, aspect: str) -> tuple[float, float]:
        p = self.pos.get(aspect)
        return (float(p[0]), float(p[1])) if p else DEFAULT_POS

    def set_pos(self, aspect: str, x: float, y: float) -> None:
        self.pos[aspect] = [round(max(0.0, min(1.0, x)), 4), round(max(0.0, min(1.0, y)), 4)]


def effective_style(item: TextItem, project_style: dict) -> dict:
    base = copy.deepcopy(DEFAULT_STYLE)
    base.update(project_style or {})
    if item.style is not None:
        base.update(item.style)
    return base


# ---------------- свои шрифты ----------------

def fonts_dir() -> Path:
    d = paths.data_dir() / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_custom_fonts() -> list[str]:
    """Подключить все шрифты, которые пользователь добавлял раньше."""
    families = []
    for f in sorted(fonts_dir().iterdir()):
        if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
            fid = QFontDatabase.addApplicationFont(str(f))
            if fid >= 0:
                families += QFontDatabase.applicationFontFamilies(fid)
    return families


def add_font(path: Path) -> str | None:
    """Скопировать файл шрифта в папку программы и подключить. Возвращает название семейства."""
    dest = fonts_dir() / path.name
    if not dest.exists():
        shutil.copyfile(path, dest)   # только содержимое: системные флаги файла (macOS) не копируем
    fid = QFontDatabase.addApplicationFont(str(dest))
    if fid < 0:
        return None
    fams = QFontDatabase.applicationFontFamilies(fid)
    return fams[0] if fams else None


# ---------------- рисование ----------------

def make_font(style: dict, W: int, H: int) -> QFont:
    font = QFont(style["font"]) if style.get("font") else QFont()
    font.setPixelSize(max(6, int(style["size"] * min(W, H))))
    font.setBold(bool(style.get("bold")))
    font.setItalic(bool(style.get("italic")))
    return font


def render_text(text: str, style: dict, W: int, H: int) -> QImage:
    """Картинка текста (с подложкой) в масштабе ролика W×H, фон прозрачный."""
    font = make_font(style, W, H)
    probe = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(probe)
    p.setFont(font)
    flags = int(Qt.AlignmentFlag.AlignHCenter) | int(Qt.TextFlag.TextWordWrap)
    box = p.boundingRect(QRectF(0, 0, W * 0.88, H * 4), flags, text or " ")
    p.end()
    px = font.pixelSize()
    pad_x, pad_y = px * 0.45, px * 0.22
    shadow = px * 0.06 if style.get("shadow") else 0
    w = int(box.width() + pad_x * 2 + shadow * 2) + 2
    h = int(box.height() + pad_y * 2 + shadow * 2) + 2
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setFont(font)
    rect = QRectF(shadow, shadow, w - shadow * 2 - 2, h - shadow * 2 - 2)
    if style.get("bg"):
        bg = QColor(style.get("bg_color", "#000000"))
        bg.setAlphaF(max(0.0, min(1.0, float(style.get("bg_opacity", 0.55)))))
        path = QPainterPath()
        r = float(style.get("bg_radius", 0.35)) * px
        path.addRoundedRect(rect, r, r)
        p.fillPath(path, bg)
    # та же ширина, что при измерении (+запас), и без обрезки — иначе Qt может перенести
    # последнее слово на строку, которая уже не помещается
    text_rect = QRectF(rect.x() + pad_x - 2, rect.y() + pad_y, box.width() + 4, box.height())
    flags |= int(Qt.TextFlag.TextDontClip)
    if shadow:
        p.setPen(QColor(0, 0, 0, 170))
        p.drawText(text_rect.translated(shadow, shadow), flags, text)
    p.setPen(QPen(QColor(style.get("color", "#ffffff"))))
    p.drawText(text_rect, flags, text)
    p.end()
    return img


def placement(img_w: int, img_h: int, pos: tuple[float, float], W: int, H: int) -> tuple[float, float]:
    """Левый верхний угол картинки текста в ролике: центр в pos, не выходя за края."""
    x = pos[0] * W - img_w / 2
    y = pos[1] * H - img_h / 2
    return max(0.0, min(W - img_w, x)), max(0.0, min(H - img_h, y))


def anim_state(anim: str, t: float, duration: float) -> tuple[float, float, float]:
    """(непрозрачность 0..1, сдвиг вниз в долях высоты кадра, масштаб) в момент t от появления."""
    D = min(ANIM_DURATION, duration / 2)
    if anim == "none" or D <= 0:
        return 1.0, 0.0, 1.0
    p_in = max(0.0, min(1.0, t / D))
    p_out = max(0.0, min(1.0, (duration - t) / D))
    opacity = min(p_in, p_out)
    if anim == "slide":
        return opacity, (1 - p_in) * 0.04, 1.0
    if anim == "pop":
        return opacity, 0.0, 0.8 + 0.2 * (1 - (1 - p_in) ** 3)
    return opacity, 0.0, 1.0


def ffmpeg_overlay(index: int, src_label: str, out_label: str, item: TextItem, style: dict,
                   x: float, y: float, w: int, h: int, H: int) -> str:
    """Фильтры FFmpeg для одного текста — те же формулы анимации, что в anim_state."""
    S, E = item.start, item.end
    D = min(ANIM_DURATION, item.duration / 2)
    anim = style.get("anim", "fade")
    chain = f"[{index}:v]format=rgba"
    if anim != "none" and D > 0:
        chain += f",fade=t=in:st={S:.3f}:d={D:.3f}:alpha=1,fade=t=out:st={E - D:.3f}:d={D:.3f}:alpha=1"
    p_in = f"min(1,max(0,(t-{S:.3f})/{D:.3f}))" if D > 0 else "1"
    xc, yc = x + w / 2, y + h / 2
    if anim == "pop":
        k = f"(0.8+0.2*(1-pow(1-{p_in},3)))"
        chain += f",scale=w='2*trunc(iw*{k}/2)':h='2*trunc(ih*{k}/2)':eval=frame"
    dy = f"(1-{p_in})*{0.04 * H:.2f}" if anim == "slide" else "0"
    chain += f"[t{index}];"
    chain += (f"[{src_label}][t{index}]overlay=x='{xc:.2f}-w/2':y='{yc:.2f}-h/2+{dy}':"
              f"enable='between(t,{S:.3f},{E:.3f})':eval=frame[{out_label}]")
    return chain
