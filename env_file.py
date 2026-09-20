"""Tiny ``KEY=VALUE`` settings file so the client can be configured without
touching system environment variables (a Windows shortcut has no env editor).

Looked up in this order, first existing file wins for each key:

1. ``<用户数据目录>/client.env``   - Windows: ``%LOCALAPPDATA%\\PVEClient\\client.env``
                                     macOS:   ``~/Library/Application Support/PVEClient/client.env``
2. ``<程序目录>/client.env``       - next to the .exe / repo root

Real environment variables always win over the file, and command line flags win
over both. See ``client.env.example`` for the supported keys.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import app_data_dir

_loaded = False


def candidates() -> list[Path]:
    out = [app_data_dir() / "client.env"]
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).parent / "client.env")
    else:
        out.append(Path(__file__).resolve().parent.parent / "client.env")
    return out


def parse_env_text(text: str) -> dict[str, str]:
    # Notepad / PowerShell write a UTF-8 BOM; drop it or the first key breaks
    text = text.lstrip("\ufeff")
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_client_env(override: bool = False) -> list[Path]:
    """Merge the settings file into os.environ. Idempotent."""
    global _loaded
    used: list[Path] = []
    for path in candidates():
        try:
            if not path.is_file():
                continue
            values = parse_env_text(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        used.append(path)
        for key, value in values.items():
            if override or key not in os.environ:
                os.environ[key] = value
    _loaded = True
    return used
