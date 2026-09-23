"""Рисует иконку приложения assets/icon.png (запускать один раз, результат лежит в репозитории)."""

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter

app = QGuiApplication([])
S = 1024
img = QImage(S, S, QImage.Format.Format_ARGB32)
img.fill(Qt.GlobalColor.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
grad = QLinearGradient(0, 0, S, S)
grad.setColorAt(0, QColor("#2B2B33"))
grad.setColorAt(1, QColor("#121216"))
p.setBrush(grad)
p.setPen(Qt.PenStyle.NoPen)
p.drawRoundedRect(QRectF(64, 64, S - 128, S - 128), 200, 200)
# «плёнка» из трёх кадров разной длины — символ нарезки моментов
for i, (x, w) in enumerate([(230, 150), (410, 230), (670, 120)]):
    p.setBrush(QColor(["#5B5BD6", "#8E4EC6", "#3E63DD"][i]))
    p.drawRoundedRect(QRectF(x, 330, w, 250), 40, 40)
p.setBrush(QColor("#E5484D"))
p.drawEllipse(QRectF(437, 640, 150, 150))
p.end()
out = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
img.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(str(out))
print(out)
