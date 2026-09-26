"""Наложения: картинка или видео поверх ролика (логотип, макет, съёмка с телефона…).

Оформление (скругление углов, тень, прозрачность) для картинок рисуется средствами Qt —
одинаково в просмотре и в экспорте. Для видео FFmpeg делает то же самое по формулам.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

DEFAULT_LAYOUT = (0.5, 0.5, 0.5)   # центр X, центр Y (доли кадра), ширина (доля ширины кадра)
SHADOW_PAD = 0.08                   # запас под тень — доля меньшей стороны наложения


@dataclass
class OverlayItem:
    id: str
    kind: str                       # "image" или "video"
    src: str                        # файл в папке проекта (media/…)
    start: float                    # когда появляется (секунды ролика)
    duration: float
    width: int = 0                  # размер исходника, пикс.
    height: int = 0
    src_duration: float = 0.0       # длина видео
    in_s: float = 0.0               # с какого места видео начинать
    has_audio: bool = False
    muted: bool = False
    opacity: float = 1.0
    radius: float = 0.0             # скругление углов: доля меньшей стороны (0…0.5)
    shadow: bool = False
    label: str = ""
    layout: dict = field(default_factory=dict)   # {"9:16": [cx, cy, ширина]}
    track: str = ""                 # дорожка (id); пусто — «Наложение»

    @property
    def end(self) -> float:
        return self.start + self.duration

    def layout_for(self, aspect: str) -> tuple[float, float, float]:
        v = self.layout.get(aspect)
        return (float(v[0]), float(v[1]), float(v[2])) if v else DEFAULT_LAYOUT

    def set_layout(self, aspect: str, cx: float, cy: float, scale: float) -> None:
        self.layout[aspect] = [round(max(-0.5, min(1.5, cx)), 4), round(max(-0.5, min(1.5, cy)), 4),
                               round(max(0.03, min(3.0, scale)), 4)]


def overlay_rect(item: OverlayItem, aspect: str, W: float, H: float) -> tuple[float, float, float, float]:
    """(x, y, ширина, высота) наложения внутри ролика W×H."""
    cx, cy, scale = item.layout_for(aspect)
    w = scale * W
    ar = (item.height / item.width) if item.width and item.height else 9 / 16
    h = w * ar
    return cx * W - w / 2, cy * H - h / 2, w, h


def shadow_pad(w: float, h: float) -> int:
    return int(min(w, h) * SHADOW_PAD) + 2


def styled_image(src: QImage, w: int, h: int, radius: float, shadow: bool, opacity: float) -> QImage:
    """Картинка наложения с оформлением. Вокруг — прозрачный запас под тень (shadow_pad)."""
    pad = shadow_pad(w, h)
    img = QImage(w + pad * 2, h + pad * 2, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setOpacity(max(0.0, min(1.0, opacity)))
    r = radius * min(w, h)
    rect = QRectF(pad, pad, w, h)
    if shadow:
        # мягкая тень: несколько полупрозрачных слоёв, смещённых вниз
        steps = 8
        for i in range(steps, 0, -1):
            grow = pad * 0.6 * i / steps
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(-grow, -grow + pad * 0.35, grow, grow + pad * 0.35),
                                r + grow, r + grow)
            p.fillPath(path, QColor(0, 0, 0, int(22 * (1 - i / (steps + 1)))))
    clip = QPainterPath()
    clip.addRoundedRect(rect, r, r)
    p.setClipPath(clip)
    if not src.isNull():
        p.drawImage(rect, src)
    else:
        p.fillRect(rect, QColor("#333"))
    p.end()
    return img


def shadow_image(w: int, h: int, radius: float, opacity: float) -> QImage:
    """Только тень (для видео-наложений: видео кладётся поверх неё)."""
    img = styled_image(QImage(), w, h, radius, True, opacity)
    # убираем серый прямоугольник «без картинки», оставляя только тень
    pad = shadow_pad(w, h)
    p = QPainter(img)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    path = QPainterPath()
    path.addRoundedRect(QRectF(pad, pad, w, h), radius * min(w, h), radius * min(w, h))
    p.fillPath(path, Qt.GlobalColor.transparent)
    p.end()
    return img


def video_alpha_filter(w: int, h: int, radius: float, opacity: float) -> str:
    """Фильтр FFmpeg: скруглённые углы и прозрачность для видео-наложения."""
    parts = [f"scale={w}:{h}", "format=rgba"]
    r = radius * min(w, h)
    if r >= 1:
        inside = (f"clip({r:.2f}-hypot(max(0,max({r:.2f}-X,X-(W-1-{r:.2f}))),"
                  f"max(0,max({r:.2f}-Y,Y-(H-1-{r:.2f}))))+0.5,0,1)")
        parts.append(f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*{opacity:.3f}*{inside}'")
    elif opacity < 0.999:
        parts.append(f"colorchannelmixer=aa={opacity:.3f}")
    return ",".join(parts)


CAMERA_RADIUS = 0.12


def camera_item(item_id: str, src: str, start: float, duration: float, width: int, height: int,
                label: str = "Веб-камера") -> OverlayItem:
    """Окошко с веб-камеры в правом нижнем углу («картинка в картинке») для обоих форматов."""
    o = OverlayItem(item_id, "video", src, round(start, 3), round(duration, 3), width=width, height=height,
                    src_duration=duration, radius=CAMERA_RADIUS, label=label, track="camera")
    ar = (height / width) if width and height else 9 / 16
    # 16:9 — ширина 24 % кадра, отступ 3 % от краёв
    W, H, sc = 1920, 1080, 0.24
    h = sc * W * ar / H
    o.set_layout("16:9", 1 - 0.03 - sc / 2, 1 - 0.03 * W / H - h / 2, sc)
    # 9:16 — крупнее, в нижней части кадра (там обычно поля под записью экрана)
    W, H, sc = 1080, 1920, 0.42
    h = sc * W * ar / H
    o.set_layout("9:16", 1 - 0.05 - sc / 2, min(0.82, 1 - 0.06 - h / 2), sc)
    return o
