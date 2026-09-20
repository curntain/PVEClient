"""Regression checks for remote iframe diagnostics; no browser or device required."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.manual import remote_e2e as remote


class RemoteDiagnosticsTests(unittest.TestCase):
    def test_origin_waits_past_async_blank_frame(self):
        class DelayedFrame:
            def __init__(self):
                self.values = iter(["about:blank", "about:blank", f"http://{remote.EMBED_HOST}:{remote.PROXY_PORT}/"])

            def eval(self, expression):
                return next(self.values)

        with patch.object(remote.time, "sleep"):
            self.assertEqual(remote.wait_for_embed_origin(DelayedFrame()), f"http://{remote.EMBED_HOST}:{remote.PROXY_PORT}/")

    def test_frame_matching_uses_hostname_not_substring(self):
        tree = {"frame": {"url": f"http://evil.test/?url={remote.EMBED_HOST}"}, "childFrames": [
            {"frame": {"id": "embed", "url": f"http://{remote.EMBED_HOST}/Action/login"}}
        ]}
        self.assertEqual(remote.find_frame(tree, remote.EMBED_HOST)["id"], "embed")

    def test_reports_transport_failure_without_http_response(self):
        cdp = object.__new__(remote.CDP)
        cdp.request_urls = {"a": "http://ikuai.example.test/app.js"}
        events = [{"method": "Network.loadingFailed", "params": {"requestId": "a", "errorText": "net::ERR_FAILED"}}]
        self.assertIn("ERR_FAILED", remote.network_problems(cdp, events)[0])

    def test_only_ignores_expected_navigation_abort(self):
        cdp = object.__new__(remote.CDP)
        cdp.request_urls = {}
        events = [
            {"method": "Network.loadingFailed", "params": {"requestId": "a", "errorText": "net::ERR_ABORTED", "canceled": True}},
            {"method": "Network.loadingFailed", "params": {"requestId": "b", "errorText": "net::ERR_BLOCKED_BY_RESPONSE", "canceled": True}},
        ]
        problems = remote.network_problems(cdp, events)
        self.assertEqual(len(problems), 1)
        self.assertIn("BLOCKED_BY_RESPONSE", problems[0])

    def test_clears_contexts_when_navigation_invalidates_them(self):
        cdp = object.__new__(remote.CDP)
        cdp.contexts = {"frame": 12}
        cdp.events = []
        cdp._record({"method": "Runtime.executionContextsCleared"})
        self.assertEqual(cdp.contexts, {})


if __name__ == "__main__":
    unittest.main()
