"""Loopback integration test for the test-only WebSocket reverse proxy."""
from __future__ import annotations

import asyncio
import socket
import threading
import unittest
from unittest.mock import patch

import uvicorn
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

from tests.manual import remote_proxy


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class RemoteProxyWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_handshake_frames_and_upstream_close(self) -> None:
        seen = {}

        async def echo(websocket):
            seen["path"] = websocket.request.path
            seen["cookie"] = websocket.request.headers.get("cookie")
            seen["authorization"] = websocket.request.headers.get("authorization")
            seen["origin"] = websocket.request.headers.get("origin")
            seen["protocol"] = websocket.subprotocol
            try:
                async for payload in websocket:
                    if payload == "close-upstream":
                        await websocket.close(code=4001, reason="device closed")
                    else:
                        await websocket.send(payload)
            except ConnectionClosedError:
                pass

        upstream = await serve(echo, "127.0.0.1", 0, subprotocols=["device-v1"])
        upstream_port = upstream.sockets[0].getsockname()[1]
        proxy_port = free_port()
        config = uvicorn.Config(
            remote_proxy.app, host="127.0.0.1", port=proxy_port,
            log_level="error", log_config=None,
        )
        proxy_server = uvicorn.Server(config)
        thread = threading.Thread(target=proxy_server.run, daemon=True)
        route = {"127.0.0.1": f"http://127.0.0.1:{upstream_port}"}
        try:
            with patch.object(remote_proxy, "ROUTES", route):
                thread.start()
                for _ in range(100):
                    if await asyncio.to_thread(port_ready, proxy_port):
                        break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("proxy server did not start")

                async with connect(
                    f"ws://127.0.0.1:{proxy_port}/socket?q=one",
                    origin="http://ikuai.example.test:8800",
                    subprotocols=["device-v1"],
                    additional_headers={"Cookie": "session=abc", "Authorization": "Basic dGVzdA=="},
                ) as client:
                    await client.send("hello")
                    self.assertEqual(await client.recv(), "hello")
                    await client.send(b"\x00\x01")
                    self.assertEqual(await client.recv(), b"\x00\x01")
                    await client.send("close-upstream")
                    with self.assertRaises(ConnectionClosedError) as closed:
                        await client.recv()
                    self.assertEqual(closed.exception.rcvd.code, 4001)
                    self.assertEqual(closed.exception.rcvd.reason, "device closed")

                self.assertEqual(seen["path"], "/socket?q=one")
                self.assertEqual(seen["cookie"], "session=abc")
                self.assertEqual(seen["authorization"], "Basic dGVzdA==")
                self.assertEqual(seen["origin"], "http://ikuai.example.test:8800")
                self.assertEqual(seen["protocol"], "device-v1")
        finally:
            proxy_server.should_exit = True
            await asyncio.to_thread(thread.join, 10)
            upstream.close()
            await upstream.wait_closed()


if __name__ == "__main__":
    unittest.main()
