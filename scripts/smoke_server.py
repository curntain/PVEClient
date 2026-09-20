import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.server import app


def main() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/", timeout=0.3)
            break
        except Exception:
            time.sleep(0.05)
    html = urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8", "replace")
    js = urllib.request.urlopen(base + "/static/app.js", timeout=3).read()
    css = urllib.request.urlopen(base + "/static/style.css", timeout=3).read()
    profiles = urllib.request.urlopen(base + "/api/profiles", timeout=3).read()
    print("index", len(html), "has_title", "PVE" in html)
    print("js", len(js), "css", len(css), "profiles", profiles.decode())
    server.should_exit = True
    time.sleep(0.2)
    print("server ok")


if __name__ == "__main__":
    main()
