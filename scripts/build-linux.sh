#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="${VERSION:-1.0.0}"
VERSION="${VERSION#v}"
rm -rf build-linux-tmp dist-linux dist-linux-pyinstaller
python -m PyInstaller --noconfirm --clean --workpath build-linux-tmp --distpath dist-linux-pyinstaller \
  --name pveclient --onedir --add-data 'static:static' --add-data 'assets:assets' \
  --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto main.py
PKG="dist-linux/pkgroot"
mkdir -p "$PKG/opt/pveclient" "$PKG/usr/bin" "$PKG/usr/share/applications"
cp -R dist-linux-pyinstaller/pveclient/. "$PKG/opt/pveclient/"
printf '#!/bin/sh\nexec /opt/pveclient/pveclient "$@"\n' > "$PKG/usr/bin/pveclient"
chmod 755 "$PKG/usr/bin/pveclient"
cat > "$PKG/usr/share/applications/pveclient.desktop" <<'DESKTOP'
[Desktop Entry]
Name=PVEClient
Comment=Proxmox VE remote management client
Exec=pveclient
Terminal=false
Type=Application
Categories=Network;System;
DESKTOP
mkdir -p "$PKG/DEBIAN"
printf 'Package: pveclient\nVersion: %s\nSection: net\nPriority: optional\nArchitecture: amd64\nMaintainer: PVEClient contributors\nDescription: Proxmox VE remote management client\n' "$VERSION" > "$PKG/DEBIAN/control"
dpkg-deb --build --root-owner-group "$PKG" "dist-linux/PVEClient-linux-amd64-${VERSION}.deb"
echo "Linux package created: dist-linux/PVEClient-linux-amd64-${VERSION}.deb"
