"""Local reverse proxies so admin UIs can load inside the app iframe.

Two modes are supported:

* root mode (``proxy_embed_root``) – a second loopback server whose *root* is
  the selected system. Admin UIs build absolute URLs (``/Action/login``,
  ``/cgi-bin/luci/...``) and route on the path, so this is the only way they
  behave as if they were opened directly. It is what the dashboard uses.
* prefixed mode (``proxy_embed``) – the older ``/embed/<profile>/<vmid>/...``
  path proxy, kept so old links keep working.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
import threading
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response

from . import config_store

# Stable per-profile/system cookie namespaces survive service restarts.

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "x-content-type-options",
    "strict-transport-security",
    "frame-options",
    # uvicorn adds its own Date/Server for the response we hand back
    "date",
    "server",
}

_NEXTCLOUD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)


def _html_error(title: str, detail: str, status: int = 502) -> HTMLResponse:
    body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;background:#0f1419;color:#e7eef6;padding:28px;line-height:1.6}}
.box{{max-width:640px;margin:0 auto;background:#1a222c;border:1px solid #2a3644;border-radius:12px;padding:22px}}
h1{{font-size:18px;margin:0 0 10px}} p{{margin:0 0 10px;color:#8b9bab;word-break:break-word}} code{{color:#8ec8ff}}
</style></head><body><div class="box"><h1>{title}</h1><p>{detail}</p>
<p>可点顶栏「外部打开」在浏览器中访问，或检查「配置」里的管理页地址是否正确。</p></div></body></html>"""
    return HTMLResponse(body, status_code=status)


def _normalize_base(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="未配置管理页地址")
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"管理页地址无效: {url}")
    if not parsed.path:
        url = url.rstrip("/") + "/"
    return url


def _proxy_prefix(profile_id: str, vmid: str) -> str:
    return f"/embed/{profile_id}/{vmid}/"


def _rewrite_location(loc: str, base: str, prefix: str) -> str:
    base_net = urlparse(base).netloc
    if not loc:
        return loc
    if loc.startswith("/") and not loc.startswith("//"):
        return prefix.rstrip("/") + loc
    abs_loc = urljoin(base, loc)
    parsed = urlparse(abs_loc)
    if parsed.netloc == base_net:
        return prefix + parsed.path.lstrip("/") + (f"?{parsed.query}" if parsed.query else "") + (f"#{parsed.fragment}" if parsed.fragment else "")
    return abs_loc


def _cookie_patch_script(cookie_prefix: str) -> str:
    """Make the embedded page see its own cookies under their original names.

    Cookies are stored namespaced (``__pve<run>_<vmid>_name``) so systems can
    never contaminate each other, but admin UIs read their session cookie from
    JavaScript (iKuai keeps its login in the JS-visible ``sess_key`` cookie), so
    ``document.cookie`` has to hide the prefix - and hide every other system's
    cookies while we are at it.
    """
    if not cookie_prefix:
        return ""
    p = json.dumps(cookie_prefix)
    return (
        "<script>(function(){var P=" + p + ";"
        "var d=Object.getOwnPropertyDescriptor(Document.prototype,'cookie');"
        "if(!d)return;Object.defineProperty(document,'cookie',{configurable:true,"
        "get:function(){var raw=d.get.call(document);if(!raw)return raw;var out=[];"
        "raw.split('; ').forEach(function(pair){var i=pair.indexOf('=');"
        "var n=i<0?pair:pair.slice(0,i),v=i<0?'':pair.slice(i);"
        "if(n.indexOf(P)===0){out.push(n.slice(P.length)+v);}});return out.join('; ');},"
        "set:function(val){var s=String(val),i=s.indexOf('='),"
        "n=(i<0?s:s.slice(0,i)).trim();"
        "if(n&&n.indexOf(P)!==0){s=P+s;}return d.set.call(document,s);}});"
        "var NativeWS=window.WebSocket;if(NativeWS){var WrappedWS=function(url,protocols){"
        "var u=String(url);try{var raw=d.get.call(document),token='';"
        "raw.split('; ').forEach(function(pair){var i=pair.indexOf('=');"
        "if(i>0&&pair.slice(0,i)==='pve_client_csrf')token=pair.slice(i+1);});"
        "if(token){var x=new URL(u,location.href);if(x.host===location.host&&(x.protocol==='ws:'||x.protocol==='wss:')){"
        "x.searchParams.set('__pve_ws_csrf',decodeURIComponent(token));u=x.href;}}}catch(e){}"
        "return protocols===undefined?new NativeWS(u):new NativeWS(u,protocols);};"
        "WrappedWS.prototype=NativeWS.prototype;"
        "try{Object.setPrototypeOf(WrappedWS,NativeWS);}catch(e){}window.WebSocket=WrappedWS;}})();</script>"
    )


def _mobile_compat_style(service_kind: str) -> str:
    if (service_kind or "").lower() != "ikuai":
        return ""
    return (
        "<style id=\"pve-mobile-compat\">"
        "@media(max-width:700px){html,body{overflow:auto!important;"
        "-webkit-overflow-scrolling:touch!important;touch-action:pan-x pan-y pinch-zoom!important;}"
        "#app,.ant-app,.ant-layout{overflow:visible!important;"
        "touch-action:pan-x pan-y pinch-zoom!important;}}</style>"
    )


def _rewrite_html(
    html: str,
    base: str,
    prefix: str,
    cookie_prefix: str = "",
    service_kind: str = "",
) -> str:
    base_net = urlparse(base).netloc
    html = re.sub(
        r'<meta[^>]+http-equiv=["\']?content-security-policy["\']?[^>]*>',
        "",
        html,
        flags=re.I,
    )
    inject = (
        (f'<base href="{prefix}">' if prefix != "/" else "")
        + _cookie_patch_script(cookie_prefix)
        + _mobile_compat_style(service_kind)
    )
    if (service_kind or "").lower() == "ikuai":
        # iKuai is a desktop-width application. In a top-level mobile WebView,
        # fit its complete layout initially and retain pinch zoom/panning.
        html = re.sub(r'<meta\b[^>]*name=["\']viewport["\'][^>]*>', '', html, flags=re.I)
        inject += '<meta name="viewport" content="width=1280,user-scalable=yes,minimum-scale=0.1,maximum-scale=5">'
    if re.search(r"<head[^>]*>", html, re.I):
        html = re.sub(r"(<head[^>]*>)", r"\1" + inject, html, count=1, flags=re.I)
    elif re.search(r"<html[^>]*>", html, re.I):
        html = re.sub(r"(<html[^>]*>)", r"\1<head>" + inject + "</head>", html, count=1, flags=re.I)
    else:
        html = inject + html

    def repl_url(m: re.Match) -> str:
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        if not val or val.startswith(("data:", "javascript:", "mailto:", "#")):
            return m.group(0)
        if val.startswith(prefix) or val.startswith("/embed/"):
            return m.group(0)
        if val.startswith("//"):
            scheme = urlparse(base).scheme or "http"
            val = f"{scheme}:{val}"
        absu = urljoin(base, val)
        p = urlparse(absu)
        if p.netloc == base_net:
            new = prefix + p.path.lstrip("/")
            if p.query:
                new += "?" + p.query
            return f"{attr}={quote}{new}{quote}"
        return m.group(0)

    # quoted href/src/action
    html = re.sub(
        r"""\b(href|src|action)=(['"])([^'"]+)\2""",
        repl_url,
        html,
        flags=re.I,
    )

    def repl_css(m: re.Match) -> str:
        q, val = m.group(1), m.group(2)
        if not val or val.startswith(("data:", "#", prefix, "/embed/")):
            return m.group(0)
        absu = urljoin(base, val)
        p = urlparse(absu)
        if p.netloc == base_net:
            new = prefix + p.path.lstrip("/")
            if p.query:
                new += "?" + p.query
            return f"url({q}{new}{q})"
        return m.group(0)

    html = re.sub(r"""url\((['"]?)([^'")]+)\1\)""", repl_css, html, flags=re.I)
    return html


def _cookie_prefix(vmid: str, profile_id: str = "") -> str:
    """Cookie-name prefix that keeps one system's cookies away from the rest."""
    identity = hashlib.sha256(f"{profile_id}:{vmid}".encode()).hexdigest()[:16]
    return f"__pve_{identity}_"


def _filter_cookie_header(cookie_header: str, prefix: str) -> str:
    """Forward only this system's own cookies, with our prefix stripped."""
    if not cookie_header:
        return ""
    kept = []
    for part in cookie_header.split(";"):
        p = part.strip()
        if not p or "=" not in p:
            continue
        name, _, value = p.partition("=")
        name = name.strip()
        if prefix and name.startswith(prefix):
            kept.append(f"{name[len(prefix):]}={value}")
        elif not prefix and name not in ("pve_client_session", "pve_client_csrf") and not name.startswith("__pve_"):
            kept.append(f"{name}={value}")
    return "; ".join(kept)


def _request_cookie(headers: Any, prefix: str) -> str | None:
    """Case-insensitive lookup of the Cookie header, filtered for this system."""
    raw = None
    for k, v in headers.items():
        if k.lower() == "cookie":
            raw = v
            break
    if raw is None:
        return None
    return _filter_cookie_header(raw, prefix)


def _filter_request_headers(
    headers: Any,
    base: str,
    cookie_prefix: str = "",
    service_kind: str = "",
) -> dict[str, str]:
    drop = {
        "host",
        "content-length",
        "connection",
        "origin",
        "referer",
        "cookie",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-for",
        "x-real-ip",
        "x-frame-options",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "sec-fetch-user",
    }
    out = {k: v for k, v in headers.items() if k.lower() not in drop}
    parsed = urlparse(base)
    out["Host"] = parsed.netloc
    origin = f"{parsed.scheme}://{parsed.netloc}"
    out["Origin"] = origin
    out["Referer"] = urljoin(base, "/")
    cookie = _request_cookie(headers, cookie_prefix)
    if cookie:
        out["Cookie"] = cookie
    # Nextcloud 33 rejects the macOS/iOS WKWebView product token before its
    # otherwise compatible WebKit engine can render the page.  Present this
    # one upstream as a current Chromium browser; keep every other appliance's
    # original user agent untouched.
    if (service_kind or "").lower() == "nextcloud":
        out["User-Agent"] = _NEXTCLOUD_USER_AGENT
    # Let httpx advertise exactly the encodings it can decode itself. Never
    # force "identity": appliances such as iKuai only ship pre-compressed
    # assets and answer 404 when the client does not accept gzip.
    out.pop("Accept-Encoding", None)
    out.pop("accept-encoding", None)
    return out


def _filter_response_headers(headers: Any, base: str, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk == "set-cookie":
            continue
        if lk == "location":
            out[k] = _rewrite_location(v, base, prefix)
            continue
        out[k] = v
    return out


def _rewrite_set_cookie(values: list[str], prefix: str) -> list[str]:
    out = []
    for raw in values:
        parts = raw.split(";")
        name_value = parts[0].strip()
        if not name_value:
            continue
        name, sep, value = name_value.partition("=")
        name = name.strip()
        if not name:
            continue
        # Renaming also drops the __Host-/__Secure- requirements, so removing
        # Domain/Secure below is safe for the http loopback origin.
        rebuilt = [f"{prefix}{name}{sep}{value}" if prefix else name_value]
        for part in parts[1:]:
            p = part.strip()
            pl = p.lower()
            if not p or pl.startswith("domain="):
                continue
            if pl.startswith("secure"):
                if not prefix:
                    rebuilt.append(p)
                continue
            if pl.startswith("samesite="):
                rebuilt.append(p if not prefix else "SameSite=Lax")
                continue
            rebuilt.append(p)
        out.append("; ".join(rebuilt))
    return out


async def _request_upstream(
    client: httpx.AsyncClient,
    request: Request,
    target: str,
    body: bytes,
    base: str,
    cookie_prefix: str = "",
    service_kind: str = "",
) -> httpx.Response:
    """Ask the appliance for ``target``.

    Redirects that stay on the same host and scheme are handed back to the
    browser, so path-routing UIs (LuCI, Nextcloud) keep the URL they expect.
    Only hops the browser could not make itself - another host, or an
    http→https upgrade on the same box - are followed here.
    """
    method = request.method
    cur_base = base
    resp = await client.request(
        method,
        target,
        headers=_filter_request_headers(
            request.headers, cur_base, cookie_prefix, service_kind
        ),
        content=body if body else None,
    )
    for _ in range(8):
        if not resp.is_redirect:
            return resp
        loc = resp.headers.get("location") or ""
        if not loc:
            return resp
        nxt = urljoin(str(resp.url), loc)
        cur = urlparse(str(resp.url))
        nxt_p = urlparse(nxt)
        if nxt_p.scheme == cur.scheme and nxt_p.netloc == cur.netloc:
            return resp
        if resp.status_code in (301, 302, 303):
            method = "GET"
            body = b""
        cur_base = f"{nxt_p.scheme}://{nxt_p.netloc}/"
        resp = await client.request(
            method,
            nxt,
            headers=_filter_request_headers(
                request.headers, cur_base, cookie_prefix, service_kind
            ),
            content=body if body else None,
        )
    return resp


async def _proxy(
    base: str,
    rel: str,
    prefix: str,
    request: Request,
    on_html: Callable[[], None] | None = None,
    on_final: Callable[[str], None] | None = None,
    cookie_prefix: str = "",
    service_kind: str = "",
) -> Response:
    """Fetch ``base`` + ``rel`` and rewrite the response to live under ``prefix``."""
    target = urljoin(base, rel)
    if urlparse(target).netloc != urlparse(base).netloc:
        target = urljoin(base, "/")

    body = await request.body()
    if request.url.query:
        target = target + ("&" if "?" in target else "?") + request.url.query

    try:
        async with httpx.AsyncClient(
            verify=False,
            follow_redirects=False,
            # generous read timeout: appliances keep long-poll requests
            # (iKuai polls status endpoints) open for tens of seconds
            timeout=httpx.Timeout(120.0, connect=8.0),
        ) as client:
            upstream = await _request_upstream(
                client,
                request,
                target,
                body,
                base,
                cookie_prefix,
                service_kind,
            )
    except httpx.TimeoutException as exc:
        return _html_error("管理系统超时", f"连接 <code>{base}</code> 超时：{exc}", 504)
    except httpx.HTTPError as exc:
        return _html_error("无法连接管理系统", f"连接 <code>{base}</code> 连接失败：{exc}", 502)

    # After redirects (e.g. http→https Nextcloud), rewrite against final origin.
    final = str(upstream.url)
    fp = urlparse(final)
    rewrite_base = f"{fp.scheme}://{fp.netloc}/"
    if on_final:
        try:
            on_final(rewrite_base)
        except Exception:
            pass

    resp_headers = _filter_response_headers(upstream.headers, final, prefix)
    set_cookies = []
    if hasattr(upstream.headers, "get_list"):
        try:
            set_cookies = list(upstream.headers.get_list("set-cookie"))
        except Exception:
            set_cookies = []
    if not set_cookies:
        set_cookies = [v for k, v in upstream.headers.multi_items() if k.lower() == "set-cookie"]
    rewritten_cookies = _rewrite_set_cookie(set_cookies, cookie_prefix)

    content = upstream.content
    ctype = (upstream.headers.get("content-type") or "").lower()
    is_html = "text/html" in ctype
    if is_html:
        resp_headers["cache-control"] = "no-store"
        resp_headers.pop("etag", None)
        resp_headers.pop("last-modified", None)
        try:
            text = content.decode(upstream.encoding or "utf-8", errors="replace")
            content = _rewrite_html(
                text, final, prefix, cookie_prefix, service_kind
            ).encode("utf-8")
            resp_headers.pop("content-length", None)
            resp_headers.pop("content-encoding", None)
        except Exception:
            pass

    if is_html and request.method == "GET" and upstream.status_code == 200 and on_html:
        try:
            on_html()
        except Exception:
            pass

    response = Response(content=content, status_code=upstream.status_code)
    for k, v in resp_headers.items():
        if k.lower() == "content-type":
            response.headers["content-type"] = v
        else:
            response.headers[k] = v
    for cookie in rewritten_cookies:
        response.headers.append("set-cookie", cookie)
    return response


async def proxy_embed(profile_id: str, vmid: str, path: str, request: Request) -> Response:
    """Prefixed proxy: /embed/<profile_id>/<vmid>/<path>."""
    entry = config_store.get_service_entry(profile_id, str(vmid))
    raw_url = entry.get("web_url") or ""
    if not raw_url:
        return _html_error("未配置管理页地址", "请在主页卡片点「配置」，填写该系统的 Web 管理地址（例如 http://10.0.0.1）。", 400)
    try:
        base = _normalize_base(raw_url)
    except HTTPException as exc:
        return _html_error("管理页地址无效", str(exc.detail), 400)
    return await _proxy(
        base,
        path or "",
        _proxy_prefix(profile_id, vmid),
        request,
        cookie_prefix=_cookie_prefix(vmid, profile_id),
        service_kind=entry.get("kind") or "",
    )

# --------------------------------------------------------------------------
# Root mode: every system gets its own loopback origin, so a system can never
# see another system's requests (and cookies/storage stay separate).
# --------------------------------------------------------------------------

_targets: dict[str, dict[str, Any]] = {}
_state_lock = threading.Lock()
_MAX_HISTORY = 30


def embed_key(profile_id: str, vmid: str) -> str:
    return f"{profile_id}:{vmid}"


def _snapshot(target: dict[str, Any]) -> dict[str, Any]:
    hist: list[str] = target.get("history") or []
    return {
        "key": target.get("key", ""),
        "profile_id": target.get("profile_id", ""),
        "vmid": target.get("vmid", ""),
        "label": target.get("label", ""),
        "base": target.get("base", ""),
        "external": target.get("base", ""),
        "public_url": _public_origin(target.get("public_url") or ""),
        "path": hist[-1] if hist else "",
        "can_back": len(hist) > 1,
    }


def _public_origin(url: str) -> str:
    """Normalise a configured public address into an origin (scheme://host/)."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}/"


def set_target(profile_id: str, vmid: str) -> dict[str, Any]:
    """Register (or refresh) the origin for one system and reset its history."""
    if not profile_id or not vmid:
        raise HTTPException(status_code=400, detail="缺少连接配置或系统 ID")
    entry = config_store.get_service_entry(profile_id, str(vmid)) or {}
    raw_url = entry.get("web_url") or ""
    if not raw_url:
        raise HTTPException(
            status_code=400,
            detail="请先在卡片「配置」里填写该系统的管理页地址（例如 http://10.0.0.1）",
        )
    base = _normalize_base(raw_url)
    key = embed_key(profile_id, str(vmid))
    target = {
        "key": key,
        "profile_id": profile_id,
        "vmid": str(vmid),
        "label": entry.get("label") or "",
        "base": base,
        "effective_base": "",
        "public_url": entry.get("public_url") or "",
        "kind": entry.get("kind") or "",
        "cookie_prefix": _cookie_prefix(str(vmid), profile_id),
        "history": [""],
    }
    with _state_lock:
        previous = _targets.get(key)
        if previous and previous.get("base") == base:
            target["effective_base"] = previous.get("effective_base", "")
        _targets[key] = target
        return _snapshot(target)


def target_state(key: str) -> dict[str, Any]:
    with _state_lock:
        target = _targets.get(key)
        return _snapshot(target) if target else _snapshot({})


def _remember_final(key: str, base: str, final: str) -> None:
    """Remember the origin a system really answers on (fnOS: 80 → :5666)."""
    with _state_lock:
        target = _targets.get(key)
        if not target:
            return
        try:
            base_host = (urlparse(base).hostname or "").lower()
            final_host = (urlparse(final).hostname or "").lower()
            public_host = (
                urlparse(_public_origin(target.get("public_url") or "")).hostname or ""
            ).lower()
            cookie_domain = (
                os.environ.get("PVE_CLIENT_COOKIE_DOMAIN") or ""
            ).strip().lower().lstrip(".")
            same_managed_domain = bool(
                cookie_domain
                and public_host.endswith("." + cookie_domain)
                and final_host.endswith("." + cookie_domain)
            )
            if (
                final_host != base_host
                and final_host != public_host
                and not same_managed_domain
            ):
                return
        except Exception:
            return
        target["effective_base"] = final


def _record_visit(key: str, path: str) -> None:
    clean = (path or "").lstrip("/")
    with _state_lock:
        target = _targets.get(key)
        if not target:
            return
        hist: list[str] = target["history"]
        if not hist:
            hist.append(clean)
        elif hist[-1] != clean:
            hist.append(clean)
            del hist[:-_MAX_HISTORY]


def go_back(key: str) -> dict[str, Any]:
    with _state_lock:
        target = _targets.get(key)
        if not target:
            return _snapshot({})
        hist: list[str] = target["history"]
        if len(hist) > 1:
            hist.pop()
        return _snapshot(target)


async def proxy_embed_root(key: str, path: str, request: Request) -> Response:
    """Proxy for a system's dedicated origin: everything maps onto its root."""
    with _state_lock:
        target = _targets.get(key)
        base = (target.get("effective_base") or target["base"]) if target else ""
        cookie_prefix = target.get("cookie_prefix", "") if target else ""
        service_kind = target.get("kind", "") if target else ""
    if not base:
        return _html_error(
            "尚未选择系统",
            "请回到仪表盘「运行系统 · 单独管理」，点某个系统的「软件内管理」按钮。",
            409,
        )
    if request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
        cookie_prefix = ""
    rel = path or ""
    return await _proxy(
        base,
        rel,
        "/",
        request,
        on_html=lambda: _record_visit(key, rel),
        on_final=lambda final: _remember_final(key, base, final),
        cookie_prefix=cookie_prefix,
        service_kind=service_kind,
    )


# --------------------------------------------------------------------------
# WebSocket pass-through (fnOS and friends keep a long-lived socket open).
# --------------------------------------------------------------------------


def _ws_uri(base: str, path: str, query: str) -> str:
    p = urlparse(base)
    scheme = "wss" if p.scheme == "https" else "ws"
    uri = f"{scheme}://{p.netloc}/{(path or '').lstrip('/')}"
    return f"{uri}?{query}" if query else uri


def _ws_headers(ws: WebSocket, base: str, cookie_prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    raw_cookie = None
    for k, v in ws.headers.items():
        if k.lower() == "cookie":
            raw_cookie = v
            break
    cookie = _filter_cookie_header(raw_cookie or "", cookie_prefix)
    if cookie:
        out["Cookie"] = cookie
    ua = ws.headers.get("user-agent")
    if ua:
        out["User-Agent"] = ua
    p = urlparse(base)
    out["Origin"] = f"{p.scheme}://{p.netloc}"
    return out


def _ssl_context(base: str) -> ssl.SSLContext | None:
    if urlparse(base).scheme != "https":
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _resolve_ws_redirect(
    base: str, path: str, query: str, ws: WebSocket, cookie_prefix: str = ""
) -> str | None:
    """The appliance may 302 the socket to another port (fnOS: 80 → :5666)."""
    url = urljoin(base, (path or "").lstrip("/"))
    if query:
        url = f"{url}?{query}"
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=8.0) as client:
            resp = await client.get(
                url, headers=_filter_request_headers(ws.headers, base, cookie_prefix)
            )
    except httpx.HTTPError:
        return None
    loc = resp.headers.get("location") or ""
    if not loc:
        return None
    return urljoin(str(resp.url), loc)


async def _pump_browser(ws: WebSocket, up: Any) -> None:
    while True:
        msg = await ws.receive()
        if msg.get("type") == "websocket.disconnect":
            return
        text = msg.get("text")
        data = msg.get("bytes")
        if text is not None:
            await up.send(text)
        elif data is not None:
            await up.send(data)


async def _pump_upstream(ws: WebSocket, up: Any) -> None:
    async for msg in up:
        if isinstance(msg, bytes):
            await ws.send_bytes(msg)
        else:
            await ws.send_text(msg)


async def proxy_embed_ws(key: str, path: str, websocket: WebSocket) -> None:
    """Pass a WebSocket through to the system behind ``key``."""
    with _state_lock:
        target = _targets.get(key)
        base = (target.get("effective_base") or target["base"]) if target else ""
        cookie_prefix = target.get("cookie_prefix", "") if target else ""
    if not base:
        await websocket.close(code=1011, reason="尚未选择系统")
        return
    if websocket.url.hostname not in ("127.0.0.1", "localhost", "::1"):
        cookie_prefix = ""

    # The client-only CSRF proof is consumed by AuthMiddleware and must not be
    # exposed to the appliance or alter its WebSocket endpoint semantics.
    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(
                websocket.url.query or "", keep_blank_values=True
            )
            if name != "__pve_ws_csrf"
        ]
    )
    subs = [
        s.strip()
        for s in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if s.strip()
    ]
    headers = _ws_headers(websocket, base, cookie_prefix)

    async def _connect(target_base: str, target_path: str, target_query: str):
        return await websockets.connect(
            _ws_uri(target_base, target_path, target_query),
            additional_headers=headers,
            subprotocols=subs or None,
            ssl=_ssl_context(target_base),
            max_size=None,
            open_timeout=15,
        )

    try:
        up = await _connect(base, path, query)
    except Exception:
        # follow an http(s) redirect of the socket path, then try once more
        moved = await _resolve_ws_redirect(base, path, query, websocket, cookie_prefix)
        if not moved:
            await websocket.close(code=1011, reason="无法连接系统")
            return
        p = urlparse(moved)
        try:
            up = await _connect(f"{p.scheme}://{p.netloc}/", p.path, p.query)
        except Exception:
            await websocket.close(code=1011, reason="无法连接系统")
            return

    try:
        try:
            await websocket.accept(subprotocol=up.subprotocol)
        except Exception:
            await websocket.accept()
        tasks = [
            asyncio.create_task(_pump_browser(websocket, up)),
            asyncio.create_task(_pump_upstream(websocket, up)),
        ]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        try:
            await up.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
