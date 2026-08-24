#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_DIR="${BUILD_DIR:-$PROJECT_DIR/build}"
DIST_DIR="${DIST_DIR:-$PROJECT_DIR/dist}"
APP="$DIST_DIR/本地发票整理.app"
DMG="$DIST_DIR/本地发票整理.dmg"

if [[ -z "$BUILD_DIR" || -z "$DIST_DIR" || "$BUILD_DIR" == "/" || "$DIST_DIR" == "/" ]]; then
  echo "Refusing unsafe build paths." >&2
  exit 1
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  if [[ -n "${EMBEDDED_CONFIG_SOURCE:-}" && -f "$EMBEDDED_CONFIG_SOURCE" ]]; then
    cp "$EMBEDDED_CONFIG_SOURCE" embedded_config.py
    echo "Using the supplied embedded DeepSeek configuration."
  else
    echo "DEEPSEEK_API_KEY is not set; the package will use local OCR only."
  fi
else
  "$PYTHON_BIN" -c 'import base64, os; open("embedded_config.py", "w", encoding="utf-8").write("DEEPSEEK_API_KEY_B64 = " + repr(base64.b64encode(os.environ["DEEPSEEK_API_KEY"].encode()).decode()) + "\n")'
fi
trap 'rm -f "$PROJECT_DIR/embedded_config.py"' EXIT

rm -rf "$BUILD_DIR" "$DIST_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

ICON_ICNS=""
if [[ -f icon.png ]] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$BUILD_DIR/本地发票整理.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$BUILD_DIR/本地发票整理.icns"
  ICON_ICNS="$BUILD_DIR/本地发票整理.icns"
fi

VISION_BINARY=""
if command -v swiftc >/dev/null 2>&1; then
  VISION_BINARY="$BUILD_DIR/macos_vision_ocr"
  swiftc -O macos_vision_ocr.swift -o "$VISION_BINARY"
fi

if "$PYTHON_BIN" -c 'import PyInstaller' >/dev/null 2>&1; then
  PYI_ARGS=(
    --clean
    --noconfirm
    --windowed
    --name "本地发票整理"
    --osx-bundle-identifier "local.invoice.organizer"
    --distpath "$DIST_DIR"
    --workpath "$BUILD_DIR/pyinstaller"
    --specpath "$BUILD_DIR"
    --hidden-import ocr_image
    --add-data "$PROJECT_DIR/README.md:."
  )
  if [[ -n "$ICON_ICNS" ]]; then
    PYI_ARGS+=(--icon "$ICON_ICNS")
  fi
  if [[ -n "$VISION_BINARY" ]]; then
    PYI_ARGS+=(--add-binary "$VISION_BINARY:.")
  fi
  if [[ -f embedded_config.py ]]; then
    PYI_ARGS+=(--hidden-import embedded_config)
  fi
  "$PYTHON_BIN" -m PyInstaller "${PYI_ARGS[@]}" desktop_entry.py
else
  if [[ "${ALLOW_PYTHON_WRAPPER:-0}" != "1" ]]; then
    echo "PyInstaller is required for a distributable macOS app." >&2
    echo "Install it with: $PYTHON_BIN -m pip install -r requirements-build.txt" >&2
    echo "For a local Python-dependent wrapper only, set ALLOW_PYTHON_WRAPPER=1." >&2
    exit 1
  fi
  echo "WARNING: building a Python-dependent wrapper, not a distributable app." >&2
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp app.py "$APP/Contents/Resources/app.py"
  cp ocr_image.py "$APP/Contents/Resources/ocr_image.py"
  cp README.md "$APP/Contents/Resources/README.md"
  if [[ -n "$ICON_ICNS" ]]; then cp "$ICON_ICNS" "$APP/Contents/Resources/icon.icns"; fi
  if [[ -f embedded_config.py ]]; then cp embedded_config.py "$APP/Contents/Resources/embedded_config.py"; fi
  if [[ -n "$VISION_BINARY" ]]; then cp "$VISION_BINARY" "$APP/Contents/Resources/macos_vision_ocr"; fi
  cp packaging/macos/launcher "$APP/Contents/MacOS/本地发票整理"
  chmod +x "$APP/Contents/MacOS/本地发票整理"
  cp packaging/macos/Info.plist "$APP/Contents/Info.plist"
fi

PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.0.0" "$PLIST" 2>/dev/null ||
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string 1.0.0" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 11.0" "$PLIST" 2>/dev/null ||
  /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 11.0" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :NSHighResolutionCapable true" "$PLIST" 2>/dev/null ||
  /usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST"

SIGN_IDENTITY="${MACOS_CODESIGN_IDENTITY:--}"
if [[ "$SIGN_IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - "$APP"
else
  codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"

SMOKE_DIR="$(mktemp -d)"
set +e
LOCAL_INVOICE_DATA_DIR="$SMOKE_DIR" "$APP/Contents/MacOS/本地发票整理" --check
SMOKE_STATUS=$?
set -e
rm -rf "$SMOKE_DIR"
if [[ $SMOKE_STATUS -ne 0 ]]; then
  echo "Packaged app smoke test failed." >&2
  exit $SMOKE_STATUS
fi

DMG_ROOT="$BUILD_DIR/dmg-root"
mkdir -p "$DMG_ROOT"
cp -R "$APP" "$DMG_ROOT/本地发票整理.app"
ln -s /Applications "$DMG_ROOT/Applications"
hdiutil create -volname "本地发票整理" -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"

if [[ "$SIGN_IDENTITY" != "-" ]]; then
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
fi

if [[ -n "${MACOS_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$MACOS_NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi

echo "Created $DMG"
