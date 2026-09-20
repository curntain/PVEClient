"""PVE Remote Client entrypoint: local API + desktop shell."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="PVE远程管理客户端", add_help=True)
    parser.add_argument(
        "--host",
        default=os.environ.get("PVE_CLIENT_HOST", "127.0.0.1"),
        help="监听地址，默认 127.0.0.1；对外提供服务（反代/隧道后面）可设 0.0.0.0（环境变量 PVE_CLIENT_HOST）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PVE_CLIENT_PORT") or 0) or None,
        help="固定监听端口（环境变量 PVE_CLIENT_PORT）；不指定则自动挑一个空闲端口",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        default=(os.environ.get("PVE_CLIENT_NO_WINDOW") or "").strip() in ("1", "true", "yes"),
        help="只跑本地服务、不打开窗口（供 macOS 侧车 / 后台服务使用）",
    )
    parser.add_argument("--print-url", action="store_true", help="就绪后把地址以 JSON 打到标准输出（供外部程序读取）")
    return parser.parse_args(argv)


def _resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    return base / relative


def _log_dir() -> Path:
    try:
        from app.paths import log_dir

        return log_dir()
    except Exception:
        if getattr(sys, "frozen", False):
            home = Path.home()
            if os.name == "nt":
                base = Path(os.environ.get("LOCALAPPDATA") or home) / "PVEClient"
            elif sys.platform == "darwin":
                base = home / "Library" / "Application Support" / "PVEClient"
            else:
                base = home / ".local" / "share" / "PVEClient"
        else:
            base = Path(__file__).resolve().parent / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base


def _log(msg: str) -> None:
    try:
        path = _log_dir() / "startup.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _write_crash(exc: BaseException) -> Path:
    path = _log_dir() / "crash.log"
    path.write_text(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        encoding="utf-8",
    )
    _log(f"CRASH written to {path}")
    return path


def _show_error(message: str) -> None:
    _log("ERROR: " + message)
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "PVE 远程管理客户端", 0x10)
        elif sys.platform == "darwin":
            script = 'display dialog ' + json.dumps(message, ensure_ascii=False) + ' with title "PVE 远程管理客户端" buttons {"好"} default button 1 with icon stop'
            subprocess.run(["osascript", "-e", script], check=False)
        else:
            if shutil.which("zenity"):
                subprocess.run(["zenity", "--error", "--text", message], check=False)
    except Exception:
        pass


def _find_free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 80):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_available(host: str, port: int) -> bool:
    for family, addr in ((socket.AF_INET, (host, port)),):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            try:
                s.bind(addr)
                return True
            except OSError:
                return False
    return False


def _ensure_stdio() -> None:
    """PyInstaller windowed builds have sys.stdout/stderr = None; uvicorn logging needs them."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _run_server(host: str, port: int) -> None:
    try:
        _ensure_stdio()
        _log("import uvicorn/app")
        import uvicorn

        from app.server import app

        _log("uvicorn configure")
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
            ws="auto",
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
        _log("uvicorn run")
        uvicorn.Server(config).run()
    except Exception as exc:
        _write_crash(exc)
        _log(f"server thread died: {exc}")


def _wait_port(host: str, port: int, tries: int = 120) -> bool:
    for _ in range(tries):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _browser_candidates() -> list[Path]:
    """Chromium-based browsers that support --app= window mode, per platform."""
    paths: list[Path] = []
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        paths += [
            Path(pf) / "Microsoft/Edge/Application/msedge.exe",
            Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
            Path(pf) / "Google/Chrome/Application/chrome.exe",
            Path(pf86) / "Google/Chrome/Application/chrome.exe",
        ]
        if local:
            paths.append(Path(local) / "Google/Chrome/Application/chrome.exe")
    elif sys.platform == "darwin":
        paths += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                paths.append(Path(found))
    return [p for p in paths if p.exists()]


def _open_app_window(url: str) -> subprocess.Popen | None:
    user_data = _log_dir() / "browser-profile"
    user_data.mkdir(parents=True, exist_ok=True)
    for exe in _browser_candidates():
        try:
            proc = subprocess.Popen(
                [
                    str(exe),
                    f"--app={url}",
                    f"--user-data-dir={user_data}",
                    "--window-size=1280,860",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-service-autorun",
                ],
                close_fds=True,
            )
            _log(f"opened app window via {exe.name} pid={proc.pid}")
            return proc
        except Exception as exc:
            _log(f"browser launch failed {exe}: {exc}")
    try:
        webbrowser.open(url)
        _log("fallback webbrowser.open")
        return None
    except Exception as exc:
        _log(f"webbrowser failed: {exc}")
        return None


def _try_webview(url: str, icon: Path, debug: bool) -> bool:
    """Optional native window. Returns False if unavailable/failed/skipped."""
    # Frozen + pywebview/WinForms is unreliable under PyInstaller; use browser shell.
    # On macOS the Swift shell provides the native window, so it stays off there too.
    if getattr(sys, "frozen", False) and os.environ.get("PVE_CLIENT_FORCE_WEBVIEW") != "1":
        _log("skip webview in frozen build")
        return False
    if os.environ.get("PVE_CLIENT_NO_WEBVIEW") == "1":
        return False
    try:
        import webview

        webview.create_window(
            title="PVE 远程管理客户端",
            url=url,
            width=1280,
            height=820,
            min_size=(960, 640),
            background_color="#0f1419",
            easy_drag=False,
            text_select=True,
        )
        kwargs: dict = {"debug": debug}
        if icon.exists():
            kwargs["icon"] = str(icon)
        _log("webview.start begin")
        webview.start(**kwargs)
        _log("webview.start returned")
        return True
    except Exception as exc:
        _log(f"webview failed: {exc}")
        _write_crash(exc)
        return False


def main() -> None:
    _ensure_stdio()
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).parent)
        sys.path.insert(0, str(Path(sys.executable).parent))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))

    _log(f"start frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")

    # optional client.env next to the data/exe so settings need no system env vars
    try:
        from app.env_file import load_client_env

        for loaded in load_client_env():
            _log(f"loaded settings {loaded}")
    except Exception as exc:
        _log(f"client.env load skipped: {exc}")

    args = _parse_args()
    host = args.host or "127.0.0.1"
    fixed_port = args.port
    if fixed_port:
        if not _port_available(host, fixed_port):
            message = f"端口 {fixed_port} 已被占用（{host}），请改 PVE_CLIENT_PORT 或关掉占用它的程序"
            _write_crash(RuntimeError(message))
            _show_error(message)
            raise SystemExit(2)
        port = fixed_port
    else:
        port = _find_free_port(8765)
    # local URL is always loopback: the browser window runs on this machine
    local_url = f"http://127.0.0.1:{port}/"
    url = os.environ.get("PVE_CLIENT_PUBLIC_URL") or local_url
    _log(f"listen {host}:{port} local={local_url} open={url} no_window={args.no_window}")

    try:
        _log("import app.server in main")
        import app.server  # noqa: F401
        _log("app.server import ok")
        threading.Thread(target=_run_server, args=(host, port), daemon=True).start()
        if not _wait_port("127.0.0.1", port):
            raise RuntimeError(f"本地服务启动失败: {local_url}")
    except Exception as exc:
        _write_crash(exc)
        _show_error(f"本地服务启动失败\n\n{exc}")
        raise
    _log("server ready")

    if args.print_url:
        try:
            # The native shell consumes this URL and embeds it in WKWebView.
            # Report the configured public URL when present; otherwise the
            # shell can never leave loopback even though PVE_CLIENT_PUBLIC_URL
            # is set and the browser launcher below would use it correctly.
            print(json.dumps({"event": "ready", "url": url, "port": port, "host": host}), flush=True)
        except Exception:
            pass

    if args.no_window:
        _log("no-window mode: serving until terminated")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    icon = _resource_path("assets/icon.ico")
    debug = bool(os.environ.get("PVE_CLIENT_DEBUG"))

    # Prefer native window when it works; always fall back to browser app mode.
    if _try_webview(url, icon, debug):
        return

    proc = _open_app_window(url)
    _log("serving in background for browser window")
    try:
        if proc is not None:
            # If launcher hands off to existing browser it exits immediately.
            try:
                proc.wait(timeout=4)
                _log("browser launcher exited; keep API alive")
                while True:
                    time.sleep(3600)
            except subprocess.TimeoutExpired:
                proc.wait()
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            _write_crash(exc)
        except Exception:
            pass
        raise
