from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, embed_proxy, embed_server


class EmbedProxyMobileTests(unittest.TestCase):
    def test_reset_appliance_session_keeps_client_access_cookies(self) -> None:
        with patch.object(auth, "config", return_value=None):
            client = TestClient(embed_server.create_embed_app("test:103"))
            response = client.get(
                "/_pve_client/reset-appliance-session",
                cookies={
                    auth.COOKIE_NAME: "keep-session",
                    auth.CSRF_COOKIE_NAME: "keep-csrf",
                    "oc_sessionPassphrase": "remove-me",
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(any(value.startswith("oc_sessionPassphrase=") for value in cookies))
        self.assertFalse(any(value.startswith(auth.COOKIE_NAME + "=") for value in cookies))
        self.assertFalse(any(value.startswith(auth.CSRF_COOKIE_NAME + "=") for value in cookies))

    def test_root_proxy_preserves_relative_form_navigation(self) -> None:
        html = embed_proxy._rewrite_html(
            '<html><head></head><body><form action="login"></form></body></html>',
            'https://cloud.example/index.php/login', '/', '__test_'
        )
        self.assertNotIn('<base href="/">', html)
        self.assertIn('action="/index.php/login"', html)
        self.assertEqual(embed_proxy._rewrite_location(
            'files#recent', 'https://cloud.example/apps/dashboard/', '/'),
            '/apps/dashboard/files#recent')

    def test_proxy_does_not_forward_outer_host_metadata(self) -> None:
        headers = embed_proxy._filter_request_headers({
            'x-forwarded-host': 'proxy.example', 'x-forwarded-proto': 'http',
            'cookie': 'pve_client_session=secret; __test_sid=appliance'
        }, 'https://cloud.example/', '__test_')
        self.assertNotIn('x-forwarded-host', headers)
        self.assertNotIn('x-forwarded-proto', headers)
        self.assertEqual(headers['Cookie'], 'sid=appliance')

    def test_nextcloud_uses_supported_browser_identity_only_for_nextcloud(self) -> None:
        raw = {"User-Agent": "WKWebView/old"}
        nextcloud = embed_proxy._filter_request_headers(
            raw, "https://cloud.example/", service_kind="nextcloud"
        )
        other = embed_proxy._filter_request_headers(
            raw, "https://router.example/", service_kind="ikuai"
        )
        self.assertIn("Chrome/142", nextcloud["User-Agent"])
        self.assertEqual(other["User-Agent"], "WKWebView/old")

    def test_public_origin_uses_native_appliance_cookies_only(self) -> None:
        raw = 'pve_client_session=secret; pve_client_csrf=token; sess_key=device; __pve_old=stale'
        self.assertEqual(embed_proxy._filter_cookie_header(raw, ''), 'sess_key=device')
        rewritten = embed_proxy._rewrite_set_cookie(
            ['sess_key=value; Path=/; Secure; HttpOnly; SameSite=Strict'], ''
        )[0]
        self.assertIn('Secure', rewritten)
        self.assertIn('SameSite=Strict', rewritten)

    def test_ikuai_html_gets_mobile_scrolling_and_websocket_token_hook(self) -> None:
        html = embed_proxy._rewrite_html(
            "<html><head></head><body></body></html>",
            "http://10.0.0.1/",
            "/",
            "__pvetest_100_",
            "ikuai",
        )
        self.assertIn("pve-mobile-compat", html)
        self.assertIn("touch-action:pan-x pan-y pinch-zoom", html)
        self.assertIn("__pve_ws_csrf", html)
        self.assertIn("window.WebSocket=WrappedWS", html)

    def test_effective_origin_accepts_only_configured_managed_domain(self) -> None:
        key = "test-profile:103"
        target = {
            "key": key,
            "base": "http://10.0.0.103/",
            "effective_base": "",
            "public_url": "https://nextcloud.example.com/",
            "history": [""],
        }
        with embed_proxy._state_lock:
            embed_proxy._targets[key] = target
        self.addCleanup(embed_proxy._targets.pop, key, None)
        with patch.dict(os.environ, {"PVE_CLIENT_COOKIE_DOMAIN": ".example.com"}):
            embed_proxy._remember_final(
                key, "http://10.0.0.103/", "https://pan.example.com/"
            )
            self.assertEqual(target["effective_base"], "https://pan.example.com/")
            embed_proxy._remember_final(
                key, "http://10.0.0.103/", "https://attacker.example/"
            )
            self.assertEqual(target["effective_base"], "https://pan.example.com/")


if __name__ == "__main__":
    unittest.main()
