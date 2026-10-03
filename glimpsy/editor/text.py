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

from PySide6.QtCore import QPointF, QRectF, Qt
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
    "karaoke": "none",          # подсветка слова, которое звучит (см. KARAOKE)
    "hl_color": "#FFD400",      # цвет подсветки
}

KARAOKE = {"none": "Без подсветки слов", "color": "Слово подсвечивается цветом", "box": "Слово на цветной плашке",
           "fill": "Сказанное закрашивается", "word": "По одному слову, крупно"}


@dataclass
class TextItem:
    id: str
    text: str
    start: float                 # когда появляется (секунды ролика)
    duration: float = 2.5
    style: dict | None = None    # None — общий стиль проекта
    pos: dict = field(default_factory=dict)   # {"9:16": [x, y]} — центр текста в долях кадра
    auto: bool = False           # создан автосубтитрами (их можно пересоздать или убрать разом)
    track: str = ""              # дорожка (id); пусто — по типу: субтитры или текст
    words: list = field(default_factory=list)  # время слов от начала текста [[начало, конец], …] (караоке)

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
    if base.get("karaoke", "none") != "none":
        base["anim"] = "none"          # подсветка слов — уже анимация; так просмотр и ролик совпадают
    return base


# ---------------- караоке: какое слово звучит ----------------

def word_times(item: TextItem) -> list[tuple[float, float]]:
    """Время каждого слова от начала текста. Если точного нет (перевод, ручной субтитр) —
    делим время по длине слов."""
    words = item.text.split()
    if item.words and len(item.words) == len(words):
        return [(float(a), float(b)) for a, b in item.words]
    if not words:
        return []
    total = sum(len(w) + 1 for w in words)
    span = max(0.1, item.duration - 0.1)
    out, t = [], 0.05
    for w in words:
        d = span * (len(w) + 1) / total
        out.append((t, t + d))
        t += d
    return out


def active_word(item: TextItem, t: float) -> int:
    """Номер слова, которое звучит в момент t от начала ролика (до первого — первое)."""
    rel = t - item.start
    k = 0
    for i, (a, _b) in enumerate(word_times(item)):
        if rel >= a - 0.02:
            k = i
    return k


def karaoke_segments(item: TextItem) -> list[tuple[float, float, int]]:
    """Отрезки (начало, конец — от начала ролика) с номером подсвеченного слова."""
    times = word_times(item)
    out = []
    for k, (a, _b) in enumerate(times):
        s0 = 0.0 if k == 0 else a
        s1 = item.duration if k + 1 == len(times) else times[k + 1][0]
        if s1 - s0 > 0.01:
            out.append((item.start + s0, item.start + s1, k))
    return out


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


def render_text(text: str, style: dict, W: int, H: int, active: int | None = None) -> QImage:
    """Картинка текста (с подложкой) в масштабе ролика W×H, фон прозрачный.
    active — номер подсвеченного слова (караоке), None — без подсветки."""
    mode = style.get("karaoke", "none")
    if active is not None and mode != "none" and text.split():
        return render_karaoke(text, style, W, H, active)
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


def render_karaoke(text: str, style: dict, W: int, H: int, active: int) -> QImage:
    """Текст, где подсвечено слово active: строки раскладываем сами, чтобы знать, где каждое слово
    (размер картинки один и тот же для всех слов — текст не прыгает)."""
    from PySide6.QtGui import QFontMetricsF

    mode = style.get("karaoke", "color")
    words = text.split()
    active = max(0, min(active, len(words) - 1))
    if mode == "word":                                 # одно слово, крупнее обычного
        big = dict(style, karaoke="none", size=float(style.get("size", 0.055)) * 1.6)
        return render_text(words[active], big, W, H)
    font = make_font(style, W, H)
    fm = QFontMetricsF(font)
    space = fm.horizontalAdvance(" ")
    max_w = W * 0.88
    lines: list[list[int]] = [[]]
    width = 0.0
    for i, w in enumerate(words):
        ww = fm.horizontalAdvance(w)
        if lines[-1] and width + space + ww > max_w:
            lines.append([])
            width = 0.0
        width += (space if lines[-1] else 0) + ww
        lines[-1].append(i)
    widths = [sum(fm.horizontalAdvance(words[i]) for i in ln) + space * (len(ln) - 1) for ln in lines]
    px = font.pixelSize()
    pad_x, pad_y = px * 0.45, px * 0.22
    lh = fm.height()
    shadow = px * 0.06 if style.get("shadow") else 0
    box_pad = px * 0.12
    w = int(max(widths) + pad_x * 2 + shadow * 2 + box_pad * 2) + 2
    h = int(lh * len(lines) + pad_y * 2 + shadow * 2) + 2
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
    color = QColor(style.get("color", "#ffffff"))
    hl = QColor(style.get("hl_color", "#FFD400"))
    for n, ln in enumerate(lines):
        x = rect.x() + (rect.width() - widths[n]) / 2
        y = rect.y() + pad_y + lh * n + fm.ascent()
        for i in ln:
            ww = fm.horizontalAdvance(words[i])
            on = i == active
            pen = hl if (on and mode == "color") or (i <= active and mode == "fill") else color
            if on and mode == "box":
                box = QPainterPath()
                box.addRoundedRect(QRectF(x - box_pad, y - fm.ascent() - box_pad * 0.3, ww + box_pad * 2,
                                          lh + box_pad * 0.6), px * 0.18, px * 0.18)
                p.fillPath(box, hl)
                pen = QColor("#111111") if hl.lightnessF() > 0.55 else QColor("#ffffff")
            if shadow:
                p.setPen(QColor(0, 0, 0, 170))
                p.drawText(QPointF(x + shadow, y + shadow), words[i])
            p.setPen(QPen(pen))
            p.drawText(QPointF(x, y), words[i])
            x += ww + space
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
