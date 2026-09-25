#!/usr/bin/env bash
# Упаковывает dist/Glimpsy (результат PyInstaller) в один файл Glimpsy-x86_64.AppImage
set -euo pipefail
cd "$(dirname "$0")/../.."
ARCH="${ARCH:-x86_64}"
APPDIR=build/Glimpsy.AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -r dist/Glimpsy "$APPDIR/usr/bin/Glimpsy"
cp packaging/linux/AppRun "$APPDIR/AppRun"
cp packaging/linux/glimpsy.desktop "$APPDIR/glimpsy.desktop"
cp assets/icon.png "$APPDIR/glimpsy.png"
if [ ! -x build/appimagetool ]; then
  curl -fsSL -o build/appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x build/appimagetool
fi
# APPIMAGE_EXTRACT_AND_RUN — чтобы работало без FUSE (например, в GitHub Actions)
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" build/appimagetool "$APPDIR" "dist/Glimpsy-${ARCH}.AppImage"
echo "Готово: dist/Glimpsy-${ARCH}.AppImage"
