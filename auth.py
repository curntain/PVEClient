"""Optional access gate for the client's own web UI.

The desktop app is a loopback-only tool with no login: every ``/api`` route
executes SSH commands, reads/writes remote files and opens a root shell. That is
fine on ``127.0.0.1``, but the moment the UI is reachable from anywhere else
(reverse proxy, tunnel, LAN) it must be protected.

Enable it with environment variables - nothing changes when they are unset:

* ``PVE_CLIENT_AUTH=user:password`` (or just ``password``) - required to use the
  UI. Both an HTTP Basic header and a signed session cookie are accepted.
* ``PVE_CLIENT_COOKIE_DOMAIN=.example.com`` - share the session with the
  per-system embed hosts (``ikuai.example.com`` and friends).
* ``PVE_CLIENT_COOKIE_SECURE=1`` - mark the cookie Secure when the UI is reached
  through HTTPS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import os
import secrets
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .paths import app_data_dir

COOKIE_NAME = "pve_client_session"
CSRF_COOKIE_NAME = "pve_client_csrf"
_SESSION_HOURS = 24
_LOGIN_WINDOW = 300
_LOGIN_LIMIT = 8
_LOGIN_BLOCK_SECONDS = 300
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))
_DEFAULT_LAN_NETWORKS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128,fc00::/7"
_DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


class Config:
    __slots__ = (
        "user",
        "password",
        "cookie_domain",
        "cookie_secure",
        "secret",
        "lan_bypass",
        "lan_networks",
        "lan_hosts",
        "trusted_proxies",
        "session_seconds",
    )

    def __init__(
        self,
        user: str,
        password: str,
        cookie_domain: str,
        cookie_secure: bool,
        secret: bytes,
        *,
        lan_bypass: bool = False,
        lan_networks: tuple[ipaddress._BaseNetwork, ...] = (),
        lan_hosts: tuple[str, ...] = (),
        trusted_proxies: tuple[ipaddress._BaseNetwork, ...] = (),
        session_seconds: int = _SESSION_HOURS * 3600,
    ):
        self.user = user
        self.password = password
        self.cookie_domain = cookie_domain
        self.cookie_secure = cookie_secure
        self.secret = secret
        self.lan_bypass = lan_bypass
        self.lan_networks = lan_networks
        self.lan_hosts = lan_hosts
        self.trusted_proxies = trusted_proxies
        self.session_seconds = session_seconds


_config: Config | None = None
_config_loaded = False


def _truthy(name: str, default: bool = False) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _networks(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    result = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            result.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return tuple(result)


def _session_secret() -> bytes:
    """Stable per-installation secret so sessions survive a restart."""
    path = app_data_dir() / "session.key"
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if len(data) >= 32:
                return data
        data = secrets.token_bytes(48)
        path.write_bytes(data)
        try:
            path.chmod(0o600)
        except Exception:
            pass
        return data
    except Exception:
        return secrets.token_bytes(48)


def config() -> Config | None:
    global _config, _config_loaded
    if _config_loaded:
        return _config
    _config_loaded = True
    raw = (os.environ.get("PVE_CLIENT_AUTH") or "").strip()
    if not raw:
        return None
    user, _, password = raw.partition(":")
    if not password:  # bare token
        user, password = "pve", raw
    try:
        session_hours = min(168, max(1, int(os.environ.get("PVE_CLIENT_SESSION_HOURS") or _SESSION_HOURS)))
    except ValueError:
        session_hours = _SESSION_HOURS
    _config = Config(
        user=user or "pve",
        password=password,
        cookie_domain=(os.environ.get("PVE_CLIENT_COOKIE_DOMAIN") or "").strip(),
        cookie_secure=_truthy("PVE_CLIENT_COOKIE_SECURE"),
        secret=_session_secret(),
        lan_bypass=_truthy("PVE_CLIENT_LAN_BYPASS"),
        lan_networks=_networks(os.environ.get("PVE_CLIENT_LAN_NETWORKS") or _DEFAULT_LAN_NETWORKS),
        lan_hosts=tuple(
            item.strip().lower()
            for item in (os.environ.get("PVE_CLIENT_LAN_HOSTS") or "").split(",")
            if item.strip()
        ),
        trusted_proxies=_networks(
            os.environ.get("PVE_CLIENT_TRUSTED_PROXIES") or _DEFAULT_TRUSTED_PROXIES
        ),
        session_seconds=session_hours * 3600,
    )
    return _config


def enabled() -> bool:
    return config() is not None


# --------------------------------------------------------------------------
# credentials / session tokens
# --------------------------------------------------------------------------


def check_credentials(user: str, password: str, cfg: Config) -> bool:
    return hmac.compare_digest(user.encode(), cfg.user.encode()) and hmac.compare_digest(
        password.encode(), cfg.password.encode()
    )


def issue_session(cfg: Config, user: str | None = None) -> str:
    expires = int(time.time()) + cfg.session_seconds
    payload = f"{user or cfg.user}|{expires}"
    sig = hmac.new(cfg.secret, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + sig


def verify_session(token: str, cfg: Config) -> bool:
    if not token or "." not in token:
        return False
    raw, _, sig = token.rpartition(".")
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except Exception:
        return False
    expect = hmac.new(cfg.secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    _, _, expires = payload.rpartition("|")
    try:
        return int(expires) > time.time()
    except ValueError:
        return False


# --------------------------------------------------------------------------
# ASGI plumbing
# --------------------------------------------------------------------------


def _header(scope: dict[str, Any], name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


def _cookie(scope: dict[str, Any], cookie_name: str) -> str:
    raw = _header(scope, b"cookie")
    for part in raw.split(";"):
        n, _, v = part.strip().partition("=")
        if n == cookie_name:
            return v
    return ""


def _basic_ok(scope: dict[str, Any], cfg: Config) -> bool:
    raw = _header(scope, b"authorization")
    if not raw.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(raw.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    user, _, password = decoded.partition(":")
    return check_credentials(user, password, cfg)


def authorized(scope: dict[str, Any], cfg: Config) -> bool:
    if verify_session(_cookie(scope, COOKIE_NAME), cfg):
        return True
    return _basic_ok(scope, cfg)


def _ip(value: str) -> ipaddress._BaseAddress | None:
    value = value.strip().strip("[]")
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _in_networks(address: ipaddress._BaseAddress | None, networks) -> bool:
    return bool(address) and any(address.version == net.version and address in net for net in networks)


def client_ip(scope: dict[str, Any], cfg: Config) -> ipaddress._BaseAddress | None:
    """Resolve the client from a trusted proxy chain, never from a naked spoofable header."""
    peer = _ip(str((scope.get("client") or ("", 0))[0]))
    if not _in_networks(peer, cfg.trusted_proxies):
        return peer

    forwarded = _header(scope, b"x-forwarded-for")
    chain = [_ip(part) for part in forwarded.split(",") if part.strip()]
    chain = [item for item in chain if item is not None]
    if not chain:
        real = _ip(_header(scope, b"x-real-ip"))
        if real is not None:
            chain = [real]
    if chain:
        # Walk from the proxy side toward the browser. The first untrusted hop
        # is authoritative; attacker-supplied values further left are ignored.
        for item in reversed(chain):
            if not _in_networks(item, cfg.trusted_proxies):
                return item
        return chain[0]

    # A direct loopback request is local only when the Host is loopback too.
    # A public Host with no forwarding metadata is a fail-closed proxy case.
    host = _request_host(scope)
    if host in ("localhost", "127.0.0.1", "::1"):
        return peer
    return None


def _request_host(scope: dict[str, Any]) -> str:
    value = _header(scope, b"host").strip()
    try:
        return (urlsplit(f"//{value}").hostname or "").lower()
    except ValueError:
        return ""


def is_lan_request(scope: dict[str, Any], cfg: Config) -> bool:
    if not cfg.lan_bypass or not _in_networks(client_ip(scope, cfg), cfg.lan_networks):
        return False
    host = _request_host(scope)
    if cfg.lan_hosts:
        return host in cfg.lan_hosts
    host_ip = _ip(host)
    return host == "localhost" or _in_networks(host_ip, cfg.lan_networks)


def auth_required(scope: dict[str, Any]) -> bool:
    cfg = config()
    return bool(cfg is not None and not is_lan_request(scope, cfg))


def _csrf_value(session_token: str, cfg: Config) -> str:
    return hmac.new(cfg.secret, f"csrf|{session_token}".encode(), hashlib.sha256).hexdigest()


def _csrf_ok(scope: dict[str, Any], cfg: Config) -> bool:
    session_token = _cookie(scope, COOKIE_NAME)
    expected = _csrf_value(session_token, cfg) if verify_session(session_token, cfg) else ""
    supplied = _header(scope, b"x-csrf-token")
    cookie = _cookie(scope, CSRF_COOKIE_NAME)
    return bool(expected and hmac.compare_digest(cookie, expected) and hmac.compare_digest(supplied, expected))


def _websocket_csrf_ok(scope: dict[str, Any], cfg: Config) -> bool:
    """Authenticate a WebSocket even when WKWebView supplies another same-site Origin."""
    session_token = _cookie(scope, COOKIE_NAME)
    if not verify_session(session_token, cfg):
        return False
    expected = _csrf_value(session_token, cfg)
    cookie = _cookie(scope, CSRF_COOKIE_NAME)
    try:
        query = parse_qs(
            (scope.get("query_string") or b"").decode("utf-8", errors="replace"),
            max_num_fields=32,
        )
    except Exception:
        return False
    supplied = (query.get("__pve_ws_csrf") or [""])[0]
    return bool(
        expected
        and hmac.compare_digest(cookie, expected)
        and hmac.compare_digest(supplied, expected)
    )


def _request_origin(scope: dict[str, Any], cfg: Config) -> str:
    peer = _ip(str((scope.get("client") or ("", 0))[0]))
    trusted = _in_networks(peer, cfg.trusted_proxies)
    scheme = scope.get("scheme") or "http"
    host = _header(scope, b"host")
    if trusted:
        scheme = (_header(scope, b"x-forwarded-proto").split(",", 1)[0].strip() or scheme)
        host = (_header(scope, b"x-forwarded-host").split(",", 1)[0].strip() or host)
    # ASGI uses ws/wss for sockets, but browsers send an HTTP(S) Origin.
    # Uvicorn may already have consumed trusted forwarding headers and changed
    # the client address, so normalize the resulting scope scheme too.
    scheme = {"ws": "http", "wss": "https"}.get(scheme.lower(), scheme.lower())
    return f"{scheme}://{host.lower()}"


def _same_origin(scope: dict[str, Any], cfg: Config) -> bool:
    if _header(scope, b"sec-fetch-site").lower() == "cross-site":
        return False
    origin = _header(scope, b"origin").strip()
    if not origin:
        return True
    if origin == "null":
        return False
    parsed = urlsplit(origin)
    normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return normalized == _request_origin(scope, cfg)


def _wants_html(scope: dict[str, Any]) -> bool:
    return "text/html" in _header(scope, b"accept")


def _safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _set_cookie(value: str, cfg: Config, max_age: int) -> str:
    parts = [f"{COOKIE_NAME}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if cfg.cookie_domain:
        parts.append(f"Domain={cfg.cookie_domain}")
    if cfg.cookie_secure:
        parts.append("Secure")
    return "; ".join(parts)


def _set_csrf_cookie(value: str, cfg: Config, max_age: int) -> str:
    parts = [f"{CSRF_COOKIE_NAME}={value}", "Path=/", "SameSite=Lax", f"Max-Age={max_age}"]
    if cfg.cookie_domain:
        parts.append(f"Domain={cfg.cookie_domain}")
    if cfg.cookie_secure:
        parts.append("Secure")
    return "; ".join(parts)


def _security_headers() -> list[tuple[bytes, bytes]]:
    return [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    ]


async def _redirect(send, location: str, cookies: list[str] | None = None) -> None:
    headers = [(b"location", location.encode()), (b"content-length", b"0"), *_security_headers()]
    for c in cookies or []:
        headers.append((b"set-cookie", c.encode()))
    await send({"type": "http.response.start", "status": 302, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


async def _send(send, status: int, body: bytes, content_type: str, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    headers = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        *_security_headers(),
    ] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _login_page(error: str, next_path: str, login_path: str = "/login") -> bytes:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PVE 远程管理客户端 · 登录</title>
<style>
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1419;color:#e7eef6;font-family:'Segoe UI','Microsoft YaHei',-apple-system,sans-serif}}
form{{width:min(360px,88vw);background:#1a222c;border:1px solid #2a3644;border-radius:14px;padding:26px}}
h1{{font-size:17px;margin:0 0 4px}}p.sub{{margin:0 0 18px;color:#8b9bab;font-size:13px}}
label{{display:block;font-size:13px;color:#8b9bab;margin:12px 0 6px}}
input{{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid #2a3644;
background:#0f1419;color:#e7eef6;font-size:15px}}
button{{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;background:#2f81f7;color:#fff;
font-size:15px;font-weight:600;cursor:pointer}}button:active{{transform:scale(.99)}}
.err{{background:#3a1d21;border:1px solid #6d2a33;color:#ffb4b4;padding:9px 11px;border-radius:8px;font-size:13px;margin:0 0 6px}}
.hint{{margin-top:14px;font-size:12px;color:#6b7a8c;line-height:1.5}}
</style></head><body>
<form method="post" action="{html.escape(login_path, quote=True)}">
  <h1>PVE 远程管理客户端</h1>
  <p class="sub">请先登录后再管理远程主机</p>
  {err}
  <input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
  <label>用户名</label>
  <input name="user" autocomplete="username" autocapitalize="off" autofocus>
  <label>口令</label>
  <input name="password" type="password" autocomplete="current-password">
  <button type="submit">登录</button>
  <p class="hint">口令由 PVE_CLIENT_AUTH 环境变量配置。长时间不操作需要重新登录。</p>
</form></body></html>""".encode("utf-8")


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return body
        body += msg.get("body", b"")
        if len(body) > 16 * 1024:
            return b""
        if not msg.get("more_body"):
            return body


_login_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}
_login_blocked_until: dict[str, float] = {}


def _login_key(scope: dict[str, Any], cfg: Config) -> str:
    address = client_ip(scope, cfg)
    return str(address) if address is not None else "unknown"


def _login_retry_after(key: str, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    with _login_lock:
        until = _login_blocked_until.get(key, 0)
        if until <= current:
            _login_blocked_until.pop(key, None)
            return 0
        return max(1, int(until - current))


def _record_login_failure(key: str, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _login_lock:
        recent = [value for value in _login_failures.get(key, []) if current - value < _LOGIN_WINDOW]
        recent.append(current)
        _login_failures[key] = recent
        if len(recent) >= _LOGIN_LIMIT:
            _login_blocked_until[key] = current + _LOGIN_BLOCK_SECONDS
            _login_failures.pop(key, None)


def _clear_login_failures(key: str) -> None:
    with _login_lock:
        _login_failures.pop(key, None)
        _login_blocked_until.pop(key, None)


async def handle_login(scope, receive, send, cfg: Config, login_path: str = "/login") -> None:
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    if scope["method"] == "GET":
        if authorized(scope, cfg):
            await _redirect(send, _safe_next((query.get("next") or ["/"])[0]))
            return
        next_path = _safe_next((query.get("next") or ["/"])[0])
        error = "用户名或口令不正确" if query.get("e") else ""
        await _send(
            send,
            200,
            _login_page(error, next_path, login_path),
            "text/html; charset=utf-8",
            [(b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")],
        )
        return
    if scope["method"] != "POST":
        await _send(send, 405, b"method not allowed", "text/plain")
        return

    key = _login_key(scope, cfg)
    retry_after = _login_retry_after(key)
    if retry_after:
        await _send(
            send,
            429,
            _login_page("尝试次数过多，请稍后再试", "/", login_path),
            "text/html; charset=utf-8",
            [(b"retry-after", str(retry_after).encode())],
        )
        return
    raw_body = await _read_body(receive)
    if not raw_body:
        await _send(send, 400, b"invalid request", "text/plain; charset=utf-8")
        return
    form = parse_qs(raw_body.decode("utf-8", errors="replace"), max_num_fields=8)
    user = (form.get("user") or [""])[0]
    password = (form.get("password") or [""])[0]
    next_path = _safe_next((form.get("next") or ["/"])[0])
    if not check_credentials(user, password, cfg):
        _record_login_failure(key)
        await _redirect(send, f"{login_path}?e=1&next={quote(next_path)}")
        return
    _clear_login_failures(key)
    token = issue_session(cfg, user)
    cookies = [
        _set_cookie(token, cfg, cfg.session_seconds),
        _set_csrf_cookie(_csrf_value(token, cfg), cfg, cfg.session_seconds),
    ]
    await _redirect(send, next_path, cookies)


async def handle_logout(send, cfg: Config) -> None:
    await _redirect(
        send,
        "/login",
        [_set_cookie("", cfg, 0), _set_csrf_cookie("", cfg, 0)],
    )


class AuthMiddleware:
    """Gate every HTTP and WebSocket request behind the shared secret."""

    def __init__(
        self,
        app,
        *,
        public_paths: Iterable[str] = (),
        login_path: str = "/login",
        logout_path: str = "/logout",
        enforce_csrf: bool = True,
    ) -> None:
        self.app = app
        self.public_paths = frozenset(public_paths)
        self.login_path = login_path
        self.logout_path = logout_path
        self.enforce_csrf = enforce_csrf

    @staticmethod
    def _secure_send(send, cookies: list[str] | None = None):
        async def wrapped(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                existing = {key.lower() for key, _ in headers}
                for key, value in _security_headers():
                    if key not in existing:
                        headers.append((key, value))
                for cookie in cookies or []:
                    headers.append((b"set-cookie", cookie.encode()))
                message = {**message, "headers": headers}
            await send(message)
        return wrapped

    async def _reject_origin(self, scope, send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await _send(send, 403, b'{"detail":"invalid request origin"}', "application/json")

    async def __call__(self, scope, receive, send) -> None:
        cfg = config()
        if cfg is None or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if scope["type"] == "websocket":
            if not _same_origin(scope, cfg) and not _websocket_csrf_ok(scope, cfg):
                await self._reject_origin(scope, send)
                return
        elif method not in _SAFE_METHODS and not _same_origin(scope, cfg):
            await self._reject_origin(scope, send)
            return

        if is_lan_request(scope, cfg):
            await self.app(scope, receive, self._secure_send(send))
            return

        if scope["type"] == "http" and path == self.login_path:
            await handle_login(scope, receive, send, cfg, self.login_path)
            return
        if (
            scope["type"] == "http"
            and scope["method"] in ("GET", "HEAD")
            and path in self.public_paths
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and path == self.logout_path:
            await handle_logout(send, cfg)
            return

        session_token = _cookie(scope, COOKIE_NAME)
        session_ok = verify_session(session_token, cfg)
        basic_ok = _basic_ok(scope, cfg)
        if session_ok or basic_ok:
            if (
                self.enforce_csrf
                and scope["type"] == "http"
                and method not in _SAFE_METHODS
                and not path.startswith("/embed/")
                and session_ok
                and not _csrf_ok(scope, cfg)
            ):
                await _send(send, 403, b'{"detail":"CSRF token invalid"}', "application/json")
                return
            cookies = []
            expected_csrf = _csrf_value(session_token, cfg) if session_ok else ""
            if session_ok and _cookie(scope, CSRF_COOKIE_NAME) != expected_csrf:
                cookies.append(_set_csrf_cookie(expected_csrf, cfg, cfg.session_seconds))
            await self.app(scope, receive, self._secure_send(send, cookies))
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        # navigations (and anything asking for a page) get the login form;
        # API clients get a 401 they can act on
        if scope["method"] == "GET" and (_wants_html(scope) or path in ("", "/")):
            target = (
                self.login_path
                if path in ("", "/")
                else f"{self.login_path}?next={quote(path)}"
            )
            await _redirect(send, target)
            return
        await _send(send, 401, b'{"detail":"\\u672a\\u8ba4\\u8bc1"}', "application/json")


def install(
    app,
    *,
    public_paths: Iterable[str] = (),
    login_path: str = "/login",
    logout_path: str = "/logout",
    enforce_csrf: bool = True,
) -> None:
    """Add the gate, optionally exposing exact GET/HEAD paths on this app only."""
    if config() is not None:
        app.add_middleware(
            AuthMiddleware,
            public_paths=public_paths,
            login_path=login_path,
            logout_path=logout_path,
            enforce_csrf=enforce_csrf,
        )


def data_dir_hint() -> Path:
    return app_data_dir()
