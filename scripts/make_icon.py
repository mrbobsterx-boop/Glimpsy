"""Рисует значок программы assets/icon.png из знака Glimpsy (glimpsy/ui/icons.py → draw_logo).

Запускать после изменения знака: python scripts/make_icon.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt                                          # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter            # noqa: E402

app = QGuiApplication([])
from glimpsy.ui.icons import draw_logo                                 # noqa: E402

S = 1024
img = QImage(S, S, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
p.translate(S * 0.06, S * 0.06)            # поля, как у системных значков
draw_logo(p, S * 0.88)
p.end()
out = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
img.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(str(out))
print(out)
