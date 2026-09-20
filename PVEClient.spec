# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from pathlib import Path
import os

block_cipher = None
root = Path(os.path.abspath(SPECPATH))

datas = [
    (str(root / "static"), "static"),
    (str(root / "assets"), "assets"),
]
binaries = []
hiddenimports = [
    "app",
    "app.server",
    "app.ssh_manager",
    "app.config_store",
    "app.monitoring",
    "app.pve_ops",
    "app.file_ops",
    "app.embed_proxy",
    "app.embed_server",
    "app.paths",
    "httpx",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "websockets",
    "websockets.legacy",
    "wsproto",
    "paramiko",
    "bcrypt",
    "nacl",
    "cryptography",
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

for pkg in ("webview", "paramiko", "certifi", "websockets", "httpx"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PVE远程管理客户端",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / "assets" / "icon.ico") if (root / "assets" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PVE远程管理客户端",
)
