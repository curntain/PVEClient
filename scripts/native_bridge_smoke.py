"""Read-only smoke check for the Swift helper bridge on a Mac developer tree."""
from __future__ import annotations

import json
from pathlib import Path

from cryptography.fernet import Fernet

from app import native_bridge, pve_ops
from app.ssh_manager import Session


def main() -> None:
    root = Path.home() / "Library" / "Application Support" / "PVEClient"
    profile = json.loads((root / "profiles.json").read_text(encoding="utf-8"))[0]
    decrypt = Fernet((root / ".key").read_bytes()).decrypt
    for field in ("password", "private_key", "key_passphrase"):
        if profile.get(field):
            profile[field] = decrypt(profile[field].encode()).decode()
    result = native_bridge.call("exec", profile, command="hostname", timeout=15)
    print("host", result["stdout"].strip(), "code", result["code"])
    entries = native_bridge.call("fileList", profile, path="/var/lib/vz/template", timeout=15)
    print("files", len(entries))
    guests = native_bridge.call("pveGuests", profile, timeout=20)
    print("guests", len(json.loads(guests["stdout"])), "code", guests["code"])
    metrics = native_bridge.call("pveMetrics", profile, channel="monitor", timeout=20)
    print("metrics", len(json.loads(metrics["stdout"])), "code", metrics["code"])
    helpers = native_bridge.call("pveHelpers", profile, timeout=20)
    print("helpers", "===STORAGES===" in helpers["stdout"], "code", helpers["code"])
    session = Session(profile)
    grouped = pve_ops.list_guests(session)
    print("backend guests", len(grouped["vms"]) + len(grouped["cts"]))
    polled = pve_ops.guest_metrics(session)
    print("backend metrics", len(polled["metrics"]))
    options = pve_ops.helper_bundle(session)
    print("backend options", len(options["storages"]), len(options["isos"]), len(options["templates"]))
    strict = {**profile, "accept_unknown_host": False}
    try:
        native_bridge.call("exec", strict, command="hostname", timeout=15)
    except native_bridge.BridgeError as exc:
        assert "known_hosts" in str(exc), str(exc)
        print("host key: strict profile rejects an unknown host")
    else:
        raise AssertionError("Strict host-key mode accepted an unknown host")


if __name__ == "__main__":
    main()
