"""Dedicated loopback origins used to embed appliance admin UIs.

Admin UIs build absolute URLs (``/Action/login``, ``/cgi-bin/luci/...``) and
route on the path, so proxying them under a path prefix such as
``/embed/<profile>/<vmid>/`` cannot work: their scripts would request the
client's own origin instead.

Every system therefore gets its own loopback server whose *root* is that
system, so the embedded page behaves exactly as if it had been opened directly.
Separate origins also keep cookies/localStorage and stray in-flight requests
from leaking between two systems. Servers are started on first use.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse

from . import auth, embed_proxy

_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]

_lock = threading.Lock()
_servers: dict[str, tuple[int, "uvicorn.Server"]] = {}


def _base_port() -> int:
    raw = (os.environ.get("PVE_CLIENT_EMBED_BASE_PORT") or "").strip()
    try:
        port = int(raw)
    except ValueError:
        port = 0
    return port if 1024 <= port <= 60000 else 9000


def preferred_port(vmid: str) -> int:
    """Stable port per system (base + vmid) so a reverse proxy is configured once."""
    try:
        offset = int(str(vmid)) % 1000
    except ValueError:
        offset = abs(hash(str(vmid))) % 1000
    return min(_base_port() + offset, 65000)


def _free_port(preferred: int, tries: int = 40) -> int:
    for port in range(preferred, preferred + tries):
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, tries: int = 120) -> bool:
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def create_embed_app(key: str) -> FastAPI:
    app = FastAPI(title="PVE Client · 系统管理", docs_url=None, redoc_url=None)
    # Appliance UIs commonly own /login and /logout themselves (iKuai does).
    # Keep the client's access gate on a private namespace so those routes can
    # reach the upstream instead of bouncing between two different logins.
    auth.install(
        app,
        login_path="/_pve_client/login",
        logout_path="/_pve_client/logout",
        # Appliance admin pages own their forms and cannot add our CSRF header.
        # Same-origin checks still apply in the authentication middleware.
        enforce_csrf=False,
    )

    @app.get("/_pve_client/reset-appliance-session")
    async def _reset_appliance_session(request: Request):
        """Forget only the embedded appliance's host-local login cookies.

        The PVE client's access cookies are shared across the managed public
        hosts, so they must never be removed here.  This recovery route is
        intentionally user-triggered from the embedded toolbar; it fixes an
        otherwise unrecoverable stale HttpOnly session without logging the
        user out of the client itself.
        """
        response = RedirectResponse(url="/", status_code=303)
        for name in request.cookies:
            if name in (auth.COOKIE_NAME, auth.CSRF_COOKIE_NAME) or name.startswith("__pve_"):
                continue
            response.delete_cookie(
                name,
                path="/",
                secure=request.url.scheme == "https",
                httponly=True,
                samesite="lax",
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.api_route("/{path:path}", methods=_METHODS)
    async def _proxy(path: str, request: Request):
        return await embed_proxy.proxy_embed_root(key, path, request)

    @app.websocket("/{path:path}")
    async def _ws(path: str, websocket: WebSocket) -> None:
        await embed_proxy.proxy_embed_ws(key, path, websocket)

    return app


def embed_port(key: str) -> int | None:
    """Port of a system's origin, or None when it was never started."""
    entry = _servers.get(key)
    return entry[0] if entry else None


def ensure_embed_server(profile_id: str, vmid: str) -> tuple[str, int]:
    """Start the origin for one system if needed; returns (key, port).

    The port is stable per vmid (``PVE_CLIENT_EMBED_BASE_PORT`` + vmid, default
    9000), so a reverse proxy / tunnel can map a public hostname to it once.
    """
    key = embed_proxy.embed_key(profile_id, str(vmid))
    with _lock:
        if key in _servers:
            return key, _servers[key][0]
        port = _free_port(preferred_port(vmid))
        config = uvicorn.Config(
            create_embed_app(key),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            log_config=None,
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, name=f"embed-{port}", daemon=True).start()
        _servers[key] = (port, server)
        if not _wait_port(port):
            _servers.pop(key, None)
            raise HTTPException(status_code=500, detail=f"无法启动系统管理端口 {port}，请重试或查看日志")
        return key, port
