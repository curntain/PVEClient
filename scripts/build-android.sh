#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="${VERSION:-1.0.0}"
VERSION="${VERSION#v}"
rm -rf dist-android
mkdir -p dist-android
gradle -p android --no-daemon :app:assembleDebug
cp android/app/build/outputs/apk/debug/app-debug.apk "dist-android/PVEClient-android-${VERSION}.apk"
echo "Android APK created: dist-android/PVEClient-android-${VERSION}.apk"
