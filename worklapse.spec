# -*- mode: python ; coding: utf-8 -*-
# Рецепт сборки PyInstaller для всех трёх систем:
#   pyinstaller worklapse.spec --noconfirm
# Результат: dist/Worklapse/ (Windows, Linux) или dist/Worklapse.app (macOS).
# FFmpeg должен лежать в vendor/ffmpeg/ (см. scripts/fetch_ffmpeg.py).

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)
__version__ = re.search(r'__version__ = "(.+?)"', (ROOT / "worklapse" / "__init__.py").read_text()).group(1)
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

ffmpeg = ROOT / "vendor" / "ffmpeg" / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
if not ffmpeg.exists():
    raise SystemExit("Нет vendor/ffmpeg — сначала выполните: python scripts/fetch_ffmpeg.py")

hidden = [
    "pynput.keyboard._win32", "pynput.mouse._win32",
    "pynput.keyboard._darwin", "pynput.mouse._darwin",
    "pynput.keyboard._xorg", "pynput.mouse._xorg",
]
if sys.platform.startswith("linux"):
    hidden += ["Xlib", "jeepney", "jeepney.io.blocking"]

a = Analysis(
    [str(ROOT / "worklapse" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[(str(ffmpeg), "ffmpeg")],
    datas=[(str(ROOT / "assets" / "icon.png"), "assets")],
    hiddenimports=hidden,
    # Лишние модули Qt сильно раздувают сборку — нам они не нужны
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "assets" / "icon.png")

# WORKLAPSE_ONEFILE=1 — один переносной файл Worklapse.exe (удобно, но стартует медленнее:
# при каждом запуске распаковывается во временную папку). По умолчанию — папка с программой.
ONEFILE = __import__("os").environ.get("WORKLAPSE_ONEFILE") == "1"

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Worklapse", console=False, icon=icon, upx=False)
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="Worklapse",
        console=False,          # без чёрного окна консоли
        icon=icon,
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="Worklapse", upx=False)

if IS_MAC and not ONEFILE:
    app = BUNDLE(
        coll,
        name="Worklapse.app",
        icon=icon,
        bundle_identifier="io.github.worklapse",
        version=__version__,
        info_plist={
            "CFBundleName": "Worklapse",
            "CFBundleDisplayName": "Worklapse",
            "CFBundleShortVersionString": __version__,
            "LSUIElement": True,             # только иконка в строке меню, без значка в Dock
            "NSHighResolutionCapable": True,
            "NSCameraUsageDescription": "Worklapse может иногда сохранять фрагменты с веб-камеры.",
            "NSAppleEventsUsageDescription": "Worklapse проверяет активное приложение для чёрного списка.",
        },
    )
