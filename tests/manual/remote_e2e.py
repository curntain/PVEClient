"""End-to-end remote-access test: fake public domains + reverse proxy + real browser.

Verifies what the docs promise:
  * the UI at http://(fake domain) requires the app login;
  * after logging in, the embed iframe uses the *public* subdomain
    (not 127.0.0.1, which would be the viewer's own machine);
  * the session cookie is shared from pve.example.test to ikuai.example.test;
  * the embedded iKuai login page really renders.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from websockets.sync.client import connect

APP_HOST = "pve.example.test"
EMBED_HOST = "ikuai.example.test"
PROXY_PORT = 8800
DBG = 9350
EDGES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
SHOTS = os.path.join(tempfile.gettempdir(), "embed_shots")
USER = os.environ.get("PVE_TEST_USER", "admin")
PASSWORD = os.environ.get("PVE_TEST_PASSWORD", "remotepass")

ok = True


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not condition:
        ok = False


class CDP:
    def __init__(self, url):
        self.ws = connect(url, max_size=128 * 1024 * 1024, open_timeout=20)
        self.n = 0
        self.events = []
        self.request_urls = {}
        self.contexts = {}

    def _record(self, msg):
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            self.request_urls[params.get("requestId")] = params.get("request", {}).get("url", "")
        elif method == "Runtime.executionContextCreated":
            context = params.get("context", {})
            aux = context.get("auxData", {})
            if aux.get("isDefault") and aux.get("frameId"):
                self.contexts[aux["frameId"]] = context.get("id")
        elif method == "Runtime.executionContextDestroyed":
            dead = params.get("executionContextId")
            self.contexts = {frame: cid for frame, cid in self.contexts.items() if cid != dead}
        elif method == "Runtime.executionContextsCleared":
            self.contexts.clear()
        self.events.append(msg)

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        end = time.monotonic() + 20
        while time.monotonic() < end:
            msg = json.loads(self.ws.recv(timeout=max(0.01, end - time.monotonic())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg
            self._record(msg)
        raise TimeoutError(method)

    def eval(self, expr, context_id=None):
        params = {"expression": expr, "awaitPromise": True, "returnByValue": True}
        if context_id is not None:
            params["contextId"] = context_id
        try:
            r = self.call("Runtime.evaluate", **params)
        except RuntimeError as exc:
            # Navigation may replace a context between the readiness check and
            # evaluation. Let the next poll discover its new context.
            if "Cannot find context" in str(exc) or "Execution context was destroyed" in str(exc):
                return None
            raise
        res = r.get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError("JavaScript evaluation failed: " + str(res["exceptionDetails"].get("text", "exception")))
        return res.get("result", {}).get("value")

    def drain(self):
        out, self.events = self.events, []
        return out

    def close(self):
        self.ws.close()


def wait_for(pred, timeout=45.0, interval=0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


def targets():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DBG}/json/list", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def find_frame(frame_tree, host):
    frame = (frame_tree or {}).get("frame", {})
    if urlsplit(frame.get("url", "")).hostname == host:
        return frame
    for child in (frame_tree or {}).get("childFrames", []) or []:
        found = find_frame(child, host)
        if found:
            return found
    return None


def iframe_login_ready(cdp):
    tree = cdp.call("Page.getFrameTree").get("result", {}).get("frameTree", {})
    frame = find_frame(tree, EMBED_HOST)
    if not frame:
        return None
    context_id = cdp.contexts.get(frame.get("id"))
    if not context_id:
        return None
    result = cdp.eval(
        "(() => {"
        # The client's own auth gate also has a password field. It isn't an
        # iKuai login page, and an empty SPA container isn't proof of rendering.
        "if(document.title.includes('PVE 远程管理客户端')) return null;"
        "const visible=el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';"
        "const fields=[...document.querySelectorAll('input[type=password], input[name=username], input[placeholder*=用户名]')];"
        "if(!fields.some(visible)) return null;"
        "return {title:document.title, url:location.href};"
        "})()",
        context_id=context_id,
    )
    return result if isinstance(result, dict) else None


def wait_for_embed_origin(cdp, timeout=30):
    expected = f"http://{EMBED_HOST}:{PROXY_PORT}"

    def ready():
        value = cdp.eval("document.getElementById('embedFrame')?.src")
        if isinstance(value, str) and value.startswith(expected + "/"):
            return value
        return None

    return wait_for(ready, timeout=timeout)


def network_problems(cdp, events):
    problems = []
    for ev in events:
        method = ev.get("method")
        params = ev.get("params", {})
        if method == "Network.responseReceived":
            response = params.get("response", {})
            status = response.get("status", 0)
            url = response.get("url", "")
            if status >= 400:
                problems.append(f"HTTP {status} {url[:120]}")
        elif method == "Network.loadingFailed":
            url = cdp.request_urls.get(params.get("requestId"), "")
            # Chromium reports deliberate navigation cancellation as ERR_ABORTED;
            # it isn't a broken resource and would otherwise create false alarms.
            if not (params.get("canceled") and params.get("errorText") == "net::ERR_ABORTED"):
                reason = params.get("errorText") or params.get("blockedReason") or "unknown"
                problems.append(f"LOAD {reason} {(url or 'request=' + str(params.get('requestId')))[:120]}")
        elif method == "Network.webSocketFrameError":
            problems.append("WS " + params.get("errorMessage", "unknown error"))
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            problems.append("JS-EXC " + str(detail.get("text") or detail.get("exception", {}).get("description", ""))[:160])
    return problems


def main() -> int:
    global ok
    ok = True
    edge = next((p for p in EDGES if os.path.exists(p)), None)
    if not edge:
        print("未找到 Microsoft Edge，无法运行浏览器端到端验证")
        return 2
    profile = tempfile.mkdtemp(prefix="edge_remote_")
    os.makedirs(SHOTS, exist_ok=True)
    proc = None
    cdp = None
    try:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             f"--remote-debugging-port={DBG}", f"--user-data-dir={profile}",
             f"--host-resolver-rules=MAP {APP_HOST} 127.0.0.1,MAP {EMBED_HOST} 127.0.0.1",
             "--no-proxy-server", "--window-size=1440,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target_list = wait_for(targets, 30)
        if not target_list:
            print("Edge 调试端口未就绪")
            return 2
        page = next((x for x in target_list if x.get("type") == "page"), None)
        if not page:
            print("Edge 未创建可调试页面")
            return 2
        cdp = CDP(page["webSocketDebuggerUrl"])
        for m in ("Page.enable", "Runtime.enable", "Network.enable"):
            cdp.call(m)
        try:
            cdp.call("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        except Exception:
            pass

        url = f"http://{APP_HOST}:{PROXY_PORT}/"
        cdp.call("Page.navigate", url=url)
        login_ready = wait_for(lambda: cdp.eval("location.pathname === '/login' && !!document.querySelector('form input[name=password]')"))
        print("== 远程访问主界面 ==")
        print("  location:", cdp.eval("location.href"))
        check("未登录被挡到登录页", login_ready is True, str(cdp.eval("location.href")))
        check("登录页可见", "PVE 远程管理客户端" in (cdp.eval("document.body.innerText") or ""))
        if not login_ready:
            return 1

        # log in like a browser form submit
        cdp.eval(
            "(() => { const f=document.querySelector('form');"
            f"f.querySelector('input[name=user]').value={json.dumps(USER)};"
            f"f.querySelector('input[name=password]').value={json.dumps(PASSWORD)};"
            "f.submit(); return 'submitted'; })()"
        )
        print("\n== 登录后 ==")
        print("  location:", cdp.eval("location.href"))
        cards = wait_for(lambda: cdp.eval("document.querySelectorAll('.service-card').length"), timeout=30)
        check("进入仪表盘并列出系统卡片", bool(cards), f"cards={cards}")
        if not cards:
            return 1
        cookies = cdp.call("Network.getAllCookies").get("result", {}).get("cookies", [])
        sess = [c for c in cookies if c["name"] == "pve_client_session"]
        print("  会话 Cookie:", json.dumps([{k: c.get(k) for k in ("domain", "path", "session", "secure")} for c in sess], ensure_ascii=False))
        check("会话 Cookie 覆盖 .example.test", any(c.get("domain") == ".example.test" for c in sess))

        # open the embedded system -> must use the PUBLIC subdomain
        cdp.drain()
        clicked = cdp.eval(
            "(() => { const c=[...document.querySelectorAll('.service-card')].find(x=>x.dataset.vmid==='100');"
            "if(!c) return 'no-card'; c.querySelector('[data-svc-open]').click(); return 'clicked'; })()"
        )
        check("找到 VMID 100 并点击管理入口", clicked == "clicked", str(clicked))
        frame = wait_for_embed_origin(cdp)
        print("\n== 内嵌管理系统 ==")
        print("  点击:", clicked, " iframe:", frame)
        check("iframe 用公网子域名（不是 127.0.0.1）", bool(frame), str(frame or "未加载公网地址"))
        shown = cdp.eval("document.getElementById('embedUrl').value")
        print("  上游地址显示:", shown)

        # Cross-origin DOM isn't readable from the parent page. CDP exposes a
        # default execution context for each frame, which lets us wait for an
        # actual iKuai login element instead of guessing with a fixed sleep.
        rendered = wait_for(lambda: iframe_login_ready(cdp), timeout=45) if frame else None
        check("iframe 内 iKuai 登录页已渲染", bool(rendered), str(rendered or "45 秒内未找到登录元素"))
        shot = None
        for attempt in range(3):
            try:
                shot = cdp.call("Page.captureScreenshot", format="png", fromSurface=True).get("result", {}).get("data")
                if shot:
                    break
            except Exception as exc:
                print(f"  截图第 {attempt + 1} 次失败: {exc}")
                time.sleep(4)
        if shot:
            p = os.path.join(SHOTS, "remote_access_ikuai.png")
            with open(p, "wb") as fh:
                fh.write(base64.b64decode(shot))
            print("  截图:", p)

        problems = network_problems(cdp, cdp.drain())
        check("公网路径无加载失败、4xx/5xx 与 JS 异常", not problems, "; ".join(problems[:6]))
        return 0 if ok else 1
    finally:
        try:
            if cdp:
                cdp.close()
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
