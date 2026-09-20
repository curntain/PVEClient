#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/dist-ios"
IPA_PATH="$OUT_DIR/PVEClient-iOS-1.20-unsigned.ipa"
WORK_DIR="$(mktemp -d)"
APP_DIR="$WORK_DIR/Payload/PVEClient.app"
SDK_PATH="$(xcrun --sdk iphoneos --show-sdk-path)"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

mkdir -p "$APP_DIR" "$OUT_DIR"
plutil -lint "$ROOT/ios/Info.plist" >/dev/null
CONFIG="$ROOT/ios/Config.local.plist"
if [ ! -f "$CONFIG" ]; then
  echo "请先复制 ios/Config.local.plist.example 为 ios/Config.local.plist 并填写自己的地址和 Bundle ID" >&2
  exit 1
fi
plutil -lint "$CONFIG" >/dev/null

SDKROOT="$SDK_PATH" xcrun --sdk iphoneos swiftc \
  "$ROOT/ios/PVEClientApp.swift" \
  -parse-as-library \
  -target arm64-apple-ios17.0 \
  -sdk "$SDK_PATH" \
  -O \
  -framework SwiftUI \
  -framework UIKit \
  -framework WebKit \
  -framework Network \
  -emit-executable \
  -o "$APP_DIR/PVEClient"

cp "$ROOT/ios/Info.plist" "$APP_DIR/Info.plist"
for key in CFBundleIdentifier PVEClientLocalURL PVEClientPublicURL; do
  value="$(/usr/libexec/PlistBuddy -c "Print :$key" "$CONFIG")"
  if [ -n "$value" ]; then
    /usr/libexec/PlistBuddy -c "Set :$key $value" "$APP_DIR/Info.plist"
  fi
done
local_url="$(/usr/libexec/PlistBuddy -c 'Print :PVEClientLocalURL' "$CONFIG")"
public_url="$(/usr/libexec/PlistBuddy -c 'Print :PVEClientPublicURL' "$CONFIG")"
if [ -z "$local_url" ] && [ -z "$public_url" ]; then
  echo "PVEClientLocalURL 和 PVEClientPublicURL 至少填写一个" >&2
  exit 1
fi
printf 'APPL????' > "$APP_DIR/PkgInfo"

if [ -f "$ROOT/assets/icon.png" ]; then
  sips -s format jpeg "$ROOT/assets/icon.png" --out "$WORK_DIR/icon-flat.jpg" >/dev/null
  sips -s format png -z 120 120 "$WORK_DIR/icon-flat.jpg" --out "$APP_DIR/AppIcon60x60@2x.png" >/dev/null
  sips -s format png -z 180 180 "$WORK_DIR/icon-flat.jpg" --out "$APP_DIR/AppIcon60x60@3x.png" >/dev/null
fi

rm -f "$IPA_PATH"
(
  cd "$WORK_DIR"
  /usr/bin/zip -qry "$IPA_PATH" Payload
)

echo "IPA: $IPA_PATH"
file "$APP_DIR/PVEClient"
xcrun lipo -info "$APP_DIR/PVEClient"
echo "注意：当前 IPA 未签名，需要使用 Apple 开发者证书、AltStore 或 Sideloadly 签名后安装。"
