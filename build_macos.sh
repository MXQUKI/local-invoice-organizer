#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is not set; the package will use local OCR only."
else
  python3 -c 'import base64, os; open("embedded_config.py", "w", encoding="utf-8").write("DEEPSEEK_API_KEY_B64 = " + repr(base64.b64encode(os.environ["DEEPSEEK_API_KEY"].encode()).decode()) + "\n")'
fi
trap 'rm -f embedded_config.py' EXIT

# If PyInstaller is installed, use it for a self-contained .app. Otherwise
# create a real .app wrapper that uses the user's installed Python 3. The
# latter keeps this project buildable without a network connection.
rm -rf build dist
mkdir -p dist
ICON_ICNS=""
if [[ -f icon.png ]] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  ICONSET="build/本地发票整理.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o build/本地发票整理.icns
  ICON_ICNS="build/本地发票整理.icns"
fi
if command -v pyinstaller >/dev/null 2>&1; then
  ICON_ARGS=()
  if [[ -n "$ICON_ICNS" ]]; then ICON_ARGS+=(--icon "$ICON_ICNS"); fi
  if [[ -f embedded_config.py ]]; then
    pyinstaller --clean --noconfirm --windowed "${ICON_ARGS[@]}" --hidden-import embedded_config --name "本地发票整理" desktop_entry.py
  else
    pyinstaller --clean --noconfirm --windowed "${ICON_ARGS[@]}" --name "本地发票整理" desktop_entry.py
  fi
else
  APP="dist/本地发票整理.app"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp app.py "$APP/Contents/Resources/app.py"
  cp ocr_image.py "$APP/Contents/Resources/ocr_image.py"
  if [[ -n "$ICON_ICNS" ]]; then cp "$ICON_ICNS" "$APP/Contents/Resources/icon.icns"; fi
  if [[ -f embedded_config.py ]]; then cp embedded_config.py "$APP/Contents/Resources/embedded_config.py"; fi
  if command -v swiftc >/dev/null 2>&1; then
    swiftc -O macos_vision_ocr.swift -o "$APP/Contents/Resources/macos_vision_ocr"
  fi
  cp README.md "$APP/Contents/Resources/README.md"
  cp packaging/macos/launcher "$APP/Contents/MacOS/本地发票整理"
  chmod +x "$APP/Contents/MacOS/本地发票整理"
  cp packaging/macos/Info.plist "$APP/Contents/Info.plist"
fi

rm -f "dist/本地发票整理.dmg"
hdiutil create -volname "本地发票整理" -srcfolder "dist/本地发票整理.app" -ov -format UDZO "dist/本地发票整理.dmg"
echo "Created dist/本地发票整理.dmg"
