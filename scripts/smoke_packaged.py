r"""Smoke-test a Windows build with a temporary, empty user data directory.

Usage: .venv\Scripts\python.exe scripts/smoke_packaged.py path/to/client.exe
Only the process started by this script is terminated; existing clients and
their saved profiles are untouched. No SSH connections are made.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix="pve_build_smoke_") as temp:
        data = Path(temp) / "PVEClient"
        data.mkdir()
        # AUTH is deliberately the first key to verify the UTF-8 BOM fix in
        # the actual frozen executable, not just the source parser.
        (data / "client.env").write_text(
            "PVE_CLIENT_AUTH=smoke:local-test-only\n"
            f"PVE_CLIENT_PORT={port}\nPVE_CLIENT_HOST=127.0.0.1\n",
            encoding="utf-8-sig",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PVE_CLIENT_")}
        env.update(LOCALAPPDATA=temp, APPDATA=temp)
        proc = subprocess.Popen(
            [str(exe), "--no-window"], env=env, cwd=exe.parent,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 25
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError(f"Packaged process exited early: {proc.returncode}")
                    try:
                        health = client.get("/api/health")
                        break
                    except httpx.TransportError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Packaged server did not become ready within 25 seconds")
                        time.sleep(0.1)
                assert health.status_code == 200, health.status_code
                assert health.json() == {"ok": True, "auth": True}, health.text
                assert health.headers.get("cache-control") == "no-store"
                assert client.get("/").status_code == 302
                assert client.get("/api/profiles").status_code == 401
                login = client.post("/login", data={"user": "smoke", "password": "local-test-only", "next": "/"})
                assert login.status_code == 302
                profiles = client.get("/api/profiles")
                assert profiles.status_code == 200 and profiles.json() == [], "Expected isolated empty profiles"
                page = client.get("/").text
                script = client.get("/static/app.js").text
                styles = client.get("/static/style.css").text
                assert 'id="logoutLink"' in page
                assert 'id="vmIsoUpload"' in page and 'id="ctTemplateUpload"' in page
                assert "setupAuthUI" in script
                assert "refreshGuestMetrics" in script and "metricsInFlight" in script
                assert "overflow: hidden" in styles and "height: 100vh" in styles
                assert "#view-dashboard > .cards" in styles
                assert "#view-files > .toolbar" in styles
                assert client.get("/logout").status_code == 302
                assert client.get("/api/profiles").status_code == 401
                assert client.get("/api/profiles", auth=("smoke", "local-test-only")).status_code == 200
            print("PASS: packaged startup, auth, fixed layout, upload UI, stable polling assets")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
