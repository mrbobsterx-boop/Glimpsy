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
BLUR_LAYOUT = (0.5, 0.5, 0.3, 0.2)  # размытие по умолчанию: центр X, центр Y, ширина, высота (доли кадра)


@dataclass
class OverlayItem:
    id: str
    kind: str                       # "image", "video", "audio" (голос, записанный в редакторе) или "blur"
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
    auto: str = ""                  # создано программой (переозвучка фразы: её номер) — пересоздаётся само
    bg: str = ""                    # фон за человеком на видео: "" — как есть, "remove" — убрать, "blur" — размыть
    strength: float = 0.5           # размытие: насколько сильно (0…1)
    mosaic: bool = False            # размытие: квадратиками (пикселями) вместо мягкого

    @property
    def end(self) -> float:
        return self.start + self.duration

    def layout_for(self, aspect: str) -> tuple[float, float, float]:
        v = self.layout.get(aspect)
        return (float(v[0]), float(v[1]), float(v[2])) if v else DEFAULT_LAYOUT

    def height_for(self, aspect: str) -> float | None:
        """Своя высота (доля высоты кадра) — у размытия: его прямоугольник тянется как угодно."""
        v = self.layout.get(aspect)
        if v and len(v) > 3:
            return float(v[3])
        return BLUR_LAYOUT[3] if self.kind == "blur" else None

    def set_layout(self, aspect: str, cx: float, cy: float, scale: float, height: float | None = None) -> None:
        v = [round(max(-0.5, min(1.5, cx)), 4), round(max(-0.5, min(1.5, cy)), 4),
             round(max(0.01 if self.kind == "blur" else 0.03, min(3.0, scale)), 4)]
        if height is None and self.kind == "blur":
            height = self.height_for(aspect)
        if height is not None:
            v.append(round(max(0.01, min(3.0, height)), 4))
        self.layout[aspect] = v

    def set_box(self, aspect: str, x: float, y: float, w: float, h: float) -> None:
        """Прямоугольник по левому верхнему углу и размеру (доли кадра)."""
        self.set_layout(aspect, x + w / 2, y + h / 2, w, h)


def overlay_rect(item: OverlayItem, aspect: str, W: float, H: float) -> tuple[float, float, float, float]:
    """(x, y, ширина, высота) наложения внутри ролика W×H."""
    cx, cy, scale = item.layout_for(aspect) if item.layout.get(aspect) or item.kind != "blur" else BLUR_LAYOUT[:3]
    w = scale * W
    own_h = item.height_for(aspect)
    if own_h is not None:
        h = own_h * H
    else:
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
        parts.append(f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*{opacity:.3f}*{inside}'")
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


# ---------------- размытие части кадра (лицо, номер, пароль…) ----------------

def blur_sigma(strength: float, W: float) -> float:
    """Сила мягкого размытия в пикселях кадра шириной W — одинаково в просмотре и в ролике."""
    return (0.004 + 0.02 * max(0.0, min(1.0, strength))) * W


def mosaic_block(strength: float, W: float) -> float:
    """Размер «пикселя» мозаики в пикселях кадра шириной W."""
    return (0.008 + 0.035 * max(0.0, min(1.0, strength))) * W


def blur_box(item: OverlayItem, aspect: str, W: int, H: int) -> tuple[int, int, int, int] | None:
    """Прямоугольник размытия в целых чётных пикселях, обрезанный по кадру; None — он весь за кадром."""
    x, y, w, h = overlay_rect(item, aspect, W, H)
    x0, y0 = max(0, int(x) // 2 * 2), max(0, int(y) // 2 * 2)
    x1, y1 = min(W, int(x + w + 1) // 2 * 2), min(H, int(y + h + 1) // 2 * 2)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return x0, y0, x1 - x0, y1 - y0


def blur_filter(item: OverlayItem, w: int, h: int, W: int) -> str:
    """Фильтр FFmpeg для вырезанного куска кадра w×h (кадр ролика шириной W)."""
    if item.mosaic:
        b = mosaic_block(item.strength, W)
        return (f"scale={max(1, round(w / b))}:{max(1, round(h / b))}:flags=area,"
                f"scale={w}:{h}:flags=neighbor")
    return f"gblur=sigma={blur_sigma(item.strength, W):.2f}:steps=2"


def blur_image(img: QImage, strength: float, mosaic: bool, frame_w: float) -> QImage:
    """Размыть картинку (кусок кадра в просмотре, где кадр шириной frame_w пикселей)."""
    if img.isNull() or img.width() < 2 or img.height() < 2:
        return img
    if mosaic:
        k = mosaic_block(strength, frame_w)
        mode = Qt.TransformationMode.FastTransformation
    else:
        k = blur_sigma(strength, frame_w) * 2.2
        mode = Qt.TransformationMode.SmoothTransformation
    sw, sh = max(1, round(img.width() / max(1.0, k))), max(1, round(img.height() / max(1.0, k)))
    small = img.scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if not mosaic and min(sw, sh) > 2:             # второй проход — без «квадратиков» от растяжения
        small = small.scaled(max(1, sw * 2), max(1, sh * 2), Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return small.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio, mode)


def blur_item(item_id: str, start: float, duration: float, aspect: str, box: list[float] | None = None,
              track: str = "") -> OverlayItem:
    """Новое размытие: box — [x, y, ширина, высота] (доли кадра) или прямоугольник по центру."""
    o = OverlayItem(item_id, "blur", "", round(start, 2), round(duration, 2), label="Размытие", track=track)
    if box:
        o.set_box(aspect, *box)
    else:
        o.set_layout(aspect, *BLUR_LAYOUT)
    return o
