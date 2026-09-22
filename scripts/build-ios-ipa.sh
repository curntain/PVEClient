#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/dist-ios"
VERSION="${VERSION:-1.0.0}"
VERSION="${VERSION#v}"
IPA_PATH="$OUT_DIR/PVEClient-iOS-${VERSION}-unsigned.ipa"
WORK_DIR="$(mktemp -d)"
APP_DIR="$WORK_DIR/Payload/PVEClient.app"
SDK_PATH="$(xcrun --sdk iphoneos --show-sdk-path)"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

mkdir -p "$APP_DIR" "$OUT_DIR"
plutil -lint "$ROOT/ios/Info.plist" >/dev/null
SDKROOT="$SDK_PATH" xcrun --sdk iphoneos swiftc \
  "$ROOT/ios/PVEClientApp.swift" \
  -parse-as-library \
  -target arm64-apple-ios17.0 \
  -sdk "$SDK_PATH" \
  -O \
  -framework SwiftUI \
  -framework UIKit \
  -framework WebKit \
  -emit-executable \
  -o "$APP_DIR/PVEClient"

cp "$ROOT/ios/Info.plist" "$APP_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_DIR/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${GITHUB_RUN_NUMBER:-1}" "$APP_DIR/Info.plist"
if [ -f "$ROOT/ios/Config.local.plist" ]; then
  plutil -lint "$ROOT/ios/Config.local.plist" >/dev/null
  bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$ROOT/ios/Config.local.plist" 2>/dev/null || true)"
  if [ -n "$bundle_id" ]; then /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $bundle_id" "$APP_DIR/Info.plist"; fi
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
echo "未签名 IPA 已生成。直接安装或提交 App Store 需要 Apple 开发者证书及 provisioning profile。首次启动时在应用内填写服务地址。"
