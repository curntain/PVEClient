"""Isolated auth regression tests: no saved profiles, servers or devices needed.

Run with: .venv/Scripts/python.exe -m unittest discover -s tests -p test_auth.py
"""
from __future__ import annotations

import unittest
import ipaddress
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import auth, env_file, paths

# Importing the application normally reads client.env and migrates profile data.
# Neither is relevant to these tests, which always provide their own auth config.
with (
    patch.object(env_file, "load_client_env"),
    patch.object(paths, "migrate_legacy_data"),
    patch.object(auth, "config", return_value=None),
):
    from app import embed_proxy, embed_server, server


class AuthTests(unittest.TestCase):
    # unittest.TestCase.enterContext was added after the system Python shipped
    # on older supported macOS releases.
    if not hasattr(unittest.TestCase, "enterContext"):
        def enterContext(self, cm):
            result = cm.__enter__()
            self.addCleanup(cm.__exit__, None, None, None)
            return result

    def setUp(self) -> None:
        auth._login_failures.clear()
        auth._login_blocked_until.clear()
        self.cfg = auth.Config("test-user", "test-password", "", False, b"x" * 48)
        self.config = self.enterContext(patch.object(auth, "config", return_value=self.cfg))
        self.profiles = self.enterContext(
            patch.object(server.config_store, "list_profiles", return_value=[])
        )

    def main_client(self) -> TestClient:
        return self.enterContext(TestClient(server.create_app(), follow_redirects=False))

    def test_health_is_public_uncached_and_read_only(self) -> None:
        client = self.main_client()
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "auth": True})
        self.assertEqual(response.headers["cache-control"], "no-store")
        response = client.head("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["cache-control"], "no-store")

        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(client.request(method, "/api/health").status_code, 401)
        for path in ("/api/health/", "/api/health/details", "/api/profiles", "/static/app.js"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)
        self.profiles.assert_not_called()

    def test_login_logout_and_basic_auth(self) -> None:
        client = self.main_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(client.get("/login").status_code, 200)

        response = client.post("/login", data={"user": self.cfg.user, "password": "wrong"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("e=1", response.headers["location"])
        self.assertNotIn(auth.COOKIE_NAME, client.cookies)
        self.assertEqual(client.get("/api/profiles").status_code, 401)

        response = client.post(
            "/login", data={"user": self.cfg.user, "password": self.cfg.password, "next": "/"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/")
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=Lax", response.headers["set-cookie"])
        self.assertIn(auth.COOKIE_NAME, client.cookies)
        self.assertIn(auth.CSRF_COOKIE_NAME, client.cookies)
        self.assertEqual(client.get("/api/profiles").json(), [])
        self.profiles.assert_called_once()

        response = client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertNotIn(auth.COOKIE_NAME, client.cookies)
        self.assertNotIn(auth.CSRF_COOKIE_NAME, client.cookies)
        self.assertEqual(client.get("/api/profiles").status_code, 401)
        self.assertEqual(
            client.get("/api/profiles", auth=(self.cfg.user, self.cfg.password)).status_code,
            200,
        )

    def test_cookie_api_writes_require_csrf_token(self) -> None:
        client = self.main_client()
        client.post(
            "/login", data={"user": self.cfg.user, "password": self.cfg.password, "next": "/"}
        )
        self.assertEqual(client.post("/api/disconnect/example").status_code, 403)
        csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
        response = client.post(
            "/api/disconnect/example",
            headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 200)

    def test_cross_site_write_is_rejected_even_with_basic_auth(self) -> None:
        client = self.main_client()
        response = client.post(
            "/api/disconnect/example",
            auth=(self.cfg.user, self.cfg.password),
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403)

    def test_websocket_csrf_token_allows_webview_origin_without_weakening_http(self) -> None:
        token = auth.issue_session(self.cfg, self.cfg.user)
        csrf = auth._csrf_value(token, self.cfg)
        scope = {
            "type": "websocket",
            "headers": [
                (
                    b"cookie",
                    f"{auth.COOKIE_NAME}={token}; {auth.CSRF_COOKIE_NAME}={csrf}".encode(),
                ),
                (b"origin", b"https://pve.example"),
                (b"host", b"fnos.example"),
            ],
            "query_string": f"type=main&__pve_ws_csrf={csrf}".encode(),
        }
        self.assertTrue(auth._websocket_csrf_ok(scope, self.cfg))
        scope["query_string"] = b"type=main&__pve_ws_csrf=wrong"
        self.assertFalse(auth._websocket_csrf_ok(scope, self.cfg))

    def test_login_attempts_are_rate_limited(self) -> None:
        client = self.main_client()
        for _ in range(auth._LOGIN_LIMIT):
            response = client.post(
                "/login", data={"user": self.cfg.user, "password": "wrong"}
            )
            self.assertEqual(response.status_code, 302)
        response = client.post(
            "/login", data={"user": self.cfg.user, "password": self.cfg.password}
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)

    def test_browser_websocket_origin_matches_asgi_scheme(self) -> None:
        for scheme, origin in (("ws", "http"), ("wss", "https")):
            scope = {
                "type": "websocket", "scheme": scheme,
                "client": ("203.0.113.10", 1234),
                "headers": [(b"host", b"fnos.example"),
                            (b"origin", f"{origin}://fnos.example".encode())],
            }
            self.assertTrue(auth._same_origin(scope, self.cfg))
            scope["headers"][1] = (b"origin", b"https://evil.example")
            self.assertFalse(auth._same_origin(scope, self.cfg))

    def test_lan_bypass_uses_only_trusted_proxy_chain(self) -> None:
        cfg = auth.Config(
            "u",
            "p",
            "",
            False,
            b"x" * 48,
            lan_bypass=True,
            lan_networks=(ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("127.0.0.0/8")),
            trusted_proxies=(ipaddress.ip_network("127.0.0.0/8"),),
        )

        def scope(peer, host="10.0.0.10:8765", forwarded=""):
            headers = [(b"host", host.encode())]
            if forwarded:
                headers.append((b"x-forwarded-for", forwarded.encode()))
            return {"client": (peer, 1234), "headers": headers}

        self.assertTrue(auth.is_lan_request(scope("10.0.0.50"), cfg))
        self.assertFalse(auth.is_lan_request(scope("8.8.8.8", forwarded="10.0.0.50"), cfg))
        self.assertTrue(auth.is_lan_request(scope("127.0.0.1", forwarded="10.0.0.50"), cfg))
        self.assertFalse(auth.is_lan_request(scope("127.0.0.1", forwarded="10.0.0.50, 8.8.8.8"), cfg))
        self.assertFalse(auth.is_lan_request(scope("127.0.0.1", host="pve.example"), cfg))
        self.assertTrue(auth.is_lan_request(scope("127.0.0.1", host="127.0.0.1:8765"), cfg))

    def test_auth_disabled(self) -> None:
        self.config.return_value = None
        client = self.main_client()
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "auth": False})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(client.get("/api/profiles").status_code, 200)
        self.assertEqual(client.get("/login").status_code, 404)
        self.assertEqual(client.get("/logout").status_code, 404)

    def test_embed_health_is_always_protected_when_auth_enabled(self) -> None:
        upstream = self.enterContext(
            patch.object(
                embed_proxy,
                "proxy_embed_root",
                new=AsyncMock(return_value=JSONResponse({"upstream": True})),
            )
        )
        client = self.enterContext(
            TestClient(embed_server.create_embed_app("isolated-test"), follow_redirects=False)
        )
        for method in ("GET", "HEAD", "POST"):
            with self.subTest(method=method):
                self.assertEqual(client.request(method, "/api/health").status_code, 401)
        upstream.assert_not_awaited()

        response = client.get("/api/health", auth=(self.cfg.user, self.cfg.password))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"upstream": True})
        upstream.assert_awaited_once()
        self.assertEqual(upstream.await_args.args[:2], ("isolated-test", "api/health"))

    def test_embed_appliance_login_route_does_not_collide_with_access_gate(self) -> None:
        upstream = self.enterContext(
            patch.object(
                embed_proxy,
                "proxy_embed_root",
                new=AsyncMock(return_value=JSONResponse({"upstream": True})),
            )
        )
        client = self.enterContext(
            TestClient(embed_server.create_embed_app("isolated-test"), follow_redirects=False)
        )

        response = client.get("/login", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "/_pve_client/login?next=/login",
        )
        upstream.assert_not_awaited()

        response = client.get("/_pve_client/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/_pve_client/login"', response.text)

        response = client.get("/login", auth=(self.cfg.user, self.cfg.password))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"upstream": True})
        self.assertEqual(upstream.await_args.args[:2], ("isolated-test", "login"))


if __name__ == "__main__":
    unittest.main()
