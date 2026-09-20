"""Test-only reverse proxy: routes by Host header, like Caddy/cloudflared would.

pve.example.test   -> app UI      (127.0.0.1:8850)
ikuai.example.test -> embed origin for VMID 100 (127.0.0.1:9200)
"""
from __future__ import annotations

import asyncio
import inspect

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from websockets import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

ROUTES = {
    "pve.example.test": "http://127.0.0.1:8850",
    # embed port = PVE_CLIENT_EMBED_BASE_PORT (9200) + vmid (100)
    "ikuai.example.test": "http://127.0.0.1:9300",
}
METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]

app = FastAPI()
client = httpx.AsyncClient(follow_redirects=False, timeout=120.0)


def upstream_for(host: str) -> str | None:
    return ROUTES.get(host.split(":")[0].lower())


@app.api_route("/{path:path}", methods=METHODS)
async def proxy(path: str, request: Request) -> Response:
    host = request.headers.get("host") or ""
    base = upstream_for(host)
    if not base:
        return Response(f"unknown host: {host}", status_code=502)

    url = f"{base}/{path}"
    if request.url.query:
        url += "?" + request.url.query

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding", "connection")
    }
    headers["host"] = base.split("://", 1)[1]
    body = await request.body()
    upstream = await client.request(request.method, url, headers=headers, content=body or None)

    keep = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding", "connection", "set-cookie")
    }
    response = Response(content=upstream.content, status_code=upstream.status_code, headers=keep)
    for k, v in upstream.headers.multi_items():
        if k.lower() == "set-cookie":
            response.headers.append("set-cookie", v)
    return response


@app.websocket("/{path:path}")
async def proxy_websocket(websocket: WebSocket, path: str) -> None:
    """Pass WebSockets too, matching a real Caddy/cloudflared deployment."""
    base = upstream_for(websocket.headers.get("host") or "")
    if not base:
        await websocket.accept()
        await websocket.close(code=1008, reason="unknown host")
        return

    url = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + f"/{path}"
    if websocket.url.query:
        url += "?" + websocket.url.query

    forwarded = {}
    for name in ("cookie", "authorization", "user-agent"):
        if websocket.headers.get(name):
            forwarded[name] = websocket.headers[name]
    protocols = [part.strip() for part in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if part.strip()]
    kwargs = {"max_size": None, "subprotocols": protocols or None}
    origin = websocket.headers.get("origin")
    if origin:
        kwargs["origin"] = origin
    header_arg = "additional_headers" if "additional_headers" in inspect.signature(websocket_connect).parameters else "extra_headers"
    kwargs[header_arg] = forwarded

    accepted = False
    try:
        async with websocket_connect(url, **kwargs) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)
            accepted = True

            async def browser_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close(
                            code=message.get("code") or 1000,
                            reason=message.get("reason") or "",
                        )
                        return
                    payload = message.get("text")
                    if payload is None:
                        payload = message.get("bytes")
                    if payload is not None:
                        await upstream.send(payload)

            async def upstream_to_browser() -> None:
                try:
                    async for payload in upstream:
                        if isinstance(payload, str):
                            await websocket.send_text(payload)
                        else:
                            await websocket.send_bytes(payload)
                finally:
                    try:
                        await websocket.close(
                            code=upstream.close_code or 1000,
                            reason=upstream.close_reason or "",
                        )
                    except RuntimeError:
                        pass

            tasks = [asyncio.create_task(browser_to_upstream()), asyncio.create_task(upstream_to_browser())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception:
        if not accepted:
            await websocket.accept()
        try:
            await websocket.close(code=1011, reason="upstream websocket failed")
        except Exception:
            pass
