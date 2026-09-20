"""Create a source-only archive from the reviewed public paths."""
from __future__ import annotations

from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist-mac" / "pve-client-src.zip"
PUBLIC_ROOT_FILES = {
    ".gitignore", "PVEClient.spec", "README.md", "build.bat",
    "client.env.example", "main.py", "requirements-macos.txt",
    "requirements-server.txt", "requirements.txt", "run-dev.bat",
}
PUBLIC_DIRS = {"app", "assets", "docs", "ios", "mac", "scripts", "static", "tests", ".github"}
BLOCKED_PARTS = {
    ".git", ".build", ".swiftpm", ".swiftpm-mirrors", "__pycache__",
    "data", "dist", "dist-ios", "dist-mac", "build", "build-mac",
    "backups", "node_modules", "xcuserdata",
}
BLOCKED_NAMES = {"client.env", "Config.local.plist", ".DS_Store", ".key", "session.key"}
ALLOWED_SUFFIXES = {".py", ".swift", ".sh", ".bat", ".md", ".html", ".css", ".js", ".png", ".ico", ".plist", ".spec", ".txt", ".yml", ".resolved", ".example"}


def included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    parts = set(relative.parts)
    if not path.is_file() or path.is_symlink() or parts & BLOCKED_PARTS:
        return False
    if path.name in BLOCKED_NAMES or path.suffix in {".pyc", ".pyo", ".log", ".db", ".key", ".pem", ".p12"}:
        return False
    if len(relative.parts) == 1:
        return path.name in PUBLIC_ROOT_FILES
    return relative.parts[0] in PUBLIC_DIRS and path.suffix in ALLOWED_SUFFIXES


def main() -> None:
    files = sorted((p for p in ROOT.rglob("*") if included(p)), key=lambda p: p.as_posix())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=OUTPUT.parent, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                archive.write(path, path.relative_to(ROOT).as_posix())
        temporary.replace(OUTPUT)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Created {OUTPUT} ({len(files)} files, {OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
