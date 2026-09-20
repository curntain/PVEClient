from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import pve_ops
from app.ssh_manager import SSHError


class FakeSession:
    def __init__(self, payload=None, error=None, profile=None):
        self.payload = payload
        self.error = error
        self.calls = 0
        self.profile = profile

    def exec_monitor(self, command, timeout=30):
        self.calls += 1
        if self.error:
            raise self.error
        return {"code": 0, "stdout": json.dumps(self.payload), "stderr": ""}


class GuestMonitoringStabilityTests(unittest.TestCase):
    def setUp(self):
        with pve_ops._guest_cache_lock:
            pve_ops._guest_cache.clear()
            pve_ops._guest_profile_cache.clear()

    def test_all_guest_metrics_use_one_cluster_call_and_cache(self):
        session = FakeSession([
            {"vmid": 100, "type": "qemu", "status": "running", "cpu": 0.25,
             "mem": 512, "maxmem": 1024, "disk": 10, "maxdisk": 100},
            {"vmid": 103, "type": "lxc", "status": "running", "cpu": 0.1,
             "mem": 256, "maxmem": 512, "disk": 20, "maxdisk": 100},
        ])

        first = pve_ops.guest_metrics(session)
        second = pve_ops.guest_metrics(session)

        self.assertEqual(session.calls, 1)
        self.assertEqual([row["vmid"] for row in first["metrics"]], ["100", "103"])
        self.assertEqual(first["metrics"][0]["cpu_percent"], 25.0)
        self.assertTrue(second["cached"])
        self.assertFalse(second["stale"])

    def test_returns_last_good_values_when_poll_temporarily_fails(self):
        session = FakeSession([{"vmid": 100, "type": "qemu", "status": "running"}])
        good = pve_ops.guest_metrics(session)
        with pve_ops._guest_cache_lock:
            pve_ops._guest_cache[session] = (0.0, good["metrics"])
            pve_ops._guest_profile_cache[pve_ops._guest_cache_key(session)] = (0.0, good["metrics"])
        session.error = SSHError("temporary")

        with patch.object(pve_ops.time, "monotonic", return_value=10.0):
            stale = pve_ops.guest_metrics(session)

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["metrics"], good["metrics"])

    def test_network_totals_become_rates_on_second_sample(self):
        session = FakeSession([
            {"vmid": 100, "type": "qemu", "status": "running", "netin": 1000, "netout": 2000},
        ])
        with patch.object(pve_ops.time, "monotonic", return_value=10.0):
            first = pve_ops.guest_metrics(session)
        self.assertFalse(first["metrics"][0]["net_sampled"])

        session.payload = [
            {"vmid": 100, "type": "qemu", "status": "running", "netin": 6000, "netout": 4000},
        ]
        with patch.object(pve_ops.time, "monotonic", return_value=15.0):
            second = pve_ops.guest_metrics(session)

        row = second["metrics"][0]
        self.assertTrue(row["net_sampled"])
        self.assertEqual(row["netin_bps"], 1000.0)
        self.assertEqual(row["netout_bps"], 400.0)

    def test_partial_rows_keep_last_good_resource_values(self):
        session = FakeSession([
            {"vmid": 100, "type": "qemu", "status": "running", "cpu": 0.5,
             "mem": 512, "maxmem": 1024, "disk": 25, "maxdisk": 100},
        ])
        with patch.object(pve_ops.time, "monotonic", return_value=10.0):
            pve_ops.guest_metrics(session)
        session.payload = [{"vmid": 100, "type": "qemu", "status": "running"}]
        with patch.object(pve_ops.time, "monotonic", return_value=15.0):
            result = pve_ops.guest_metrics(session)

        row = result["metrics"][0]
        self.assertEqual(row["cpu_percent"], 50.0)
        self.assertEqual(row["mem_percent"], 50.0)
        self.assertEqual(row["disk_percent"], 25.0)

    def test_last_good_values_survive_session_recreation(self):
        profile = {"id": "same-profile", "host": "10.0.0.254", "port": 22}
        first_session = FakeSession(
            [{"vmid": 100, "type": "qemu", "status": "running", "cpu": 0.2}],
            profile=profile,
        )
        with patch.object(pve_ops.time, "monotonic", return_value=10.0):
            good = pve_ops.guest_metrics(first_session)

        replacement = FakeSession(error=SSHError("temporary"), profile=profile)
        with patch.object(pve_ops.time, "monotonic", return_value=20.0):
            stale = pve_ops.guest_metrics(replacement)

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["metrics"], good["metrics"])


if __name__ == "__main__":
    unittest.main()
