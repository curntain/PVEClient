#!/usr/bin/env bash
# 在 macOS 上构建 PVEClient.app（SwiftUI 外壳 + Python 侧车）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${VERSION:-1.0.0}"
VERSION="${VERSION#v}"
BUNDLE_ID="${BUNDLE_ID:-com.pveclient.shell}"
PYTHON_BIN="${PYTHON:-python3}"
VENV="${VENV:-.venv-mac}"
BACKEND_NAME="pve-client-backend"
APP_DIR="${APP_DIR:-dist-mac/PVEClient.app}"
ZIP_PATH="${ZIP_PATH:-dist-mac/PVEClient-macos-$VERSION.zip}"

echo "==> 仓库: $ROOT"
echo "==> 版本: $VERSION"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "找不到 $PYTHON_BIN，请先安装 Python 3.11+（brew install python@3.12 或 python.org 版）" >&2
  exit 1
fi

echo "==> 1/5 准备 Python 环境 ($VENV)"
if [ ! -d "$VENV" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-macos.txt

echo "==> 2/5 打包 Python 后端（单文件、带控制台输出）"
pyinstaller --noconfirm --clean \
  --distpath build-mac/backend-dist \
  --workpath build-mac/backend-work \
  mac/PVEClient-macos.spec

echo "==> 3/5 编译 SwiftUI 外壳"
swift build -c release --package-path mac
swift build -c release --package-path mac --product PVENativeHelper

echo "==> 4/5 组装 $APP_DIR"
mkdir -p "$(dirname "$APP_DIR")"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/backend"

cp "mac/.build/release/PVEClientShell" "$APP_DIR/Contents/MacOS/PVEClient"
chmod +x "$APP_DIR/Contents/MacOS/PVEClient"

cp "build-mac/backend-dist/$BACKEND_NAME" "$APP_DIR/Contents/Resources/backend/$BACKEND_NAME"
chmod +x "$APP_DIR/Contents/Resources/backend/$BACKEND_NAME"
cp "mac/.build/release/PVENativeHelper" "$APP_DIR/Contents/Resources/backend/PVENativeHelper"
chmod +x "$APP_DIR/Contents/Resources/backend/PVENativeHelper"

if [ -f assets/icon.icns ]; then
  cp assets/icon.icns "$APP_DIR/Contents/Resources/AppIcon.icns"
elif [ -f assets/icon.png ]; then
  iconset="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$iconset"
  for size in 16 32 64 128 256 512; do
    sips -z $size $size assets/icon.png --out "$iconset/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double assets/icon.png --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$APP_DIR/Contents/Resources/AppIcon.icns" || true
fi

sed -e "s/@VERSION@/$VERSION/g" -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" mac/Info.plist > "$APP_DIR/Contents/Info.plist"
printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"

echo "==> 5/5 签名并打包 zip"
# 想分发给别人时设置这两个环境变量（否则只做 ad-hoc 签名，本机自用足够）：
#   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE=notary-profile   # 先用 xcrun notarytool store-credentials 建好
BACKEND_BIN="$APP_DIR/Contents/Resources/backend/$BACKEND_NAME"
NATIVE_HELPER_BIN="$APP_DIR/Contents/Resources/backend/PVENativeHelper"

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  echo "    使用 Developer ID 签名：$CODESIGN_IDENTITY"
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$BACKEND_BIN"
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$NATIVE_HELPER_BIN"
  for f in "$APP_DIR"/Contents/Resources/backend/*; do
    [ "$f" = "$BACKEND_BIN" ] || codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$f" || true
  done
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP_DIR/Contents/MacOS/PVEClient"
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP_DIR"
  codesign --verify --strict --verbose=2 "$APP_DIR"

  if [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "    提交公证（notarytool）…"
    ditto -c -k --keepParent "$APP_DIR" "dist-mac/_notarize.zip"
    xcrun notarytool submit "dist-mac/_notarize.zip" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP_DIR"
    rm -f "dist-mac/_notarize.zip"
  fi
else
  codesign --force --deep --sign - "$APP_DIR" 2>/dev/null || echo "    codesign 跳过（仅本机运行没问题）"
fi

xattr -cr "$APP_DIR" 2>/dev/null || true
ditto -c -k --keepParent "$APP_DIR" "$ZIP_PATH"

echo
echo "完成："
echo "  应用: $APP_DIR"
echo "  压缩包: $ZIP_PATH"
echo
echo "直接运行：open \"$APP_DIR\""
echo "公网访问：编辑 ~/.pveclient.env 后重启应用（见 docs/remote-access.md）"
