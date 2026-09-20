# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: macOS sidecar backend (single console binary, stdout used by the shell)."""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all

root = Path(os.path.abspath(SPECPATH)).parent  # SPECPATH = <repo>/mac

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
    "app.auth",
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
    "paramiko",
    "bcrypt",
    "nacl",
    "cryptography",
    "eval_type_backport",
]

for pkg in ("paramiko", "bcrypt", "nacl", "certifi", "websockets", "httpx", "cryptography"):
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
    # pywebview is not used: the Swift shell owns the window, and this keeps
    # PyObjC/setuptools out of the bundle.
    excludes=["tkinter", "pytest", "webview", "pywebview", "pip"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pve-client-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
