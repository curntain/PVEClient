"""Config and log paths. Prefer a stable user dir, not PyInstaller _internal."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _user_data_root() -> Path:
    """Per-user application data root for the current OS."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base)


def app_data_dir() -> Path:
    """Stable per-user data dir (survives rebuilds)."""
    if is_frozen():
        d = _user_data_root() / "PVEClient"
    else:
        d = Path(__file__).resolve().parent.parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    return app_data_dir()


def migrate_legacy_data() -> None:
    """Copy configs from old locations into the stable dir once."""
    dest = app_data_dir()
    candidates = []
    if is_frozen():
        # old buggy locations
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(meipass / "data")
        candidates.append(Path(sys.executable).parent / "_internal" / "data")
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            if appdata:
                candidates.append(Path(appdata) / "PVEClient")
        elif sys.platform == "darwin":
            candidates.append(Path.home() / ".pveclient")
        else:
            candidates.append(Path.home() / ".config" / "PVEClient")
    else:
        candidates.append(Path(__file__).resolve().parent.parent / "data")

    names = ("profiles.json", "services.json", ".key")
    for src_dir in candidates:
        if not src_dir.exists():
            continue
        for name in names:
            src = src_dir / name
            dst = dest / name
            if src.exists() and not dst.exists():
                try:
                    dst.write_bytes(src.read_bytes())
                except Exception:
                    pass
