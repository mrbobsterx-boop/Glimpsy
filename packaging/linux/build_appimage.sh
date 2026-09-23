#!/usr/bin/env bash
# Упаковывает dist/Worklapse (результат PyInstaller) в один файл Worklapse-x86_64.AppImage
set -euo pipefail
cd "$(dirname "$0")/../.."
ARCH="${ARCH:-x86_64}"
APPDIR=build/Worklapse.AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -r dist/Worklapse "$APPDIR/usr/bin/Worklapse"
cp packaging/linux/AppRun "$APPDIR/AppRun"
cp packaging/linux/worklapse.desktop "$APPDIR/worklapse.desktop"
cp assets/icon.png "$APPDIR/worklapse.png"
if [ ! -x build/appimagetool ]; then
  curl -fsSL -o build/appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x build/appimagetool
fi
# APPIMAGE_EXTRACT_AND_RUN — чтобы работало без FUSE (например, в GitHub Actions)
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" build/appimagetool "$APPDIR" "dist/Worklapse-${ARCH}.AppImage"
echo "Готово: dist/Worklapse-${ARCH}.AppImage"
