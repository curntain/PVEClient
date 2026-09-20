"""Optional macOS JSON-lines bridge to the Swift SSH/SFTP/PVE core.

Only the existing local backend talks to the helper. SSH secrets are sent via
the child's private stdin pipe and are never included in logs or responses.
"""
from __future__ import annotations

import atexit
import json
import os
import platform
import queue
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config_store


def enabled() -> bool:
    candidate = os.environ.get("PVE_CLIENT_NATIVE_HELPER") or ""
    return bool(candidate and Path(candidate).is_file())


def terminal_enabled() -> bool:
    if not enabled() or sys.platform != "darwin":
        return False
    version = platform.mac_ver()[0]
    try:
        return int(version.split(".")[0]) >= 15
    except (ValueError, IndexError):
        return False


class BridgeError(Exception):
    pass


def _profile_payload(profile: dict[str, Any]) -> dict[str, Any]:
    secrets = config_store.resolve_secrets(profile)
    if os.environ.get("PVE_CLIENT_NATIVE_DIAG") == "1":
        print(
            "native profile: "
            f"host={secrets.get('domain') or secrets.get('host')!r} "
            f"port={secrets.get('port')!r} "
            f"user={secrets.get('username')!r} "
            f"password_decrypted={bool(secrets.get('password')) and not str(secrets.get('password')).startswith('gAAAA')}",
            file=sys.stderr,
            flush=True,
        )
    return {
        "id": secrets.get("id") or "temporary",
        "host": secrets.get("domain") or secrets.get("host") or "",
        "port": int(secrets.get("port") or 22),
        "username": secrets.get("username") or "root",
        "password": secrets.get("password") or None,
        "privateKey": secrets.get("private_key") or None,
        "keyPassphrase": secrets.get("key_passphrase") or None,
        "authMethod": secrets.get("auth_method") or "password",
        "acceptUnknownHost": bool(secrets.get("accept_unknown_host", True)),
    }


class _Helper:
    def __init__(self, executable: str):
        self.executable = executable
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 0

    def _start(self) -> subprocess.Popen[str]:
        process = self._process
        if process and process.poll() is None:
            return process
        if process:
            self.close()
        process = subprocess.Popen(
            [self.executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            close_fds=True,
        )
        self._process = process
        ready = self._readline(process, 15)
        try:
            event = json.loads(ready)
        except Exception as exc:
            self.close()
            raise BridgeError("Swift 助手启动响应无效") from exc
        if event.get("event") != "ready":
            self.close()
            raise BridgeError("Swift 助手未就绪")
        return process

    @staticmethod
    def _readline(process: subprocess.Popen[str], timeout: int) -> str:
        assert process.stdout is not None
        if not select.select([process.stdout], [], [], timeout)[0]:
            raise BridgeError("Swift 助手请求超时")
        line = process.stdout.readline()
        if not line:
            raise BridgeError("Swift 助手已退出")
        return line

    def call(self, operation: str, profile: dict[str, Any], timeout: int = 30, **kwargs: Any) -> Any:
        with self._lock:
            process = self._start()
            self._next_id += 1
            payload = {
                "id": self._next_id,
                "operation": operation,
                "profile": _profile_payload(profile),
                **kwargs,
            }
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
                response = json.loads(self._readline(process, timeout))
                if response.get("id") != self._next_id:
                    raise BridgeError(
                        f"Swift 助手响应序号不匹配（期望 {self._next_id}，收到 {response.get('id')}）"
                    )
                if not response.get("ok"):
                    if os.environ.get("PVE_CLIENT_NATIVE_DIAG") == "1":
                        print(f"native failure: {response.get('debug')!r}", file=sys.stderr, flush=True)
                    raise BridgeError(response.get("error") or "Swift 操作失败")
                return response.get("result")
            except (BrokenPipeError, EOFError, json.JSONDecodeError, BridgeError) as exc:
                if not isinstance(exc, BridgeError) or "Swift 操作失败" in str(exc):
                    self.close()
                raise BridgeError(str(exc)) from exc

    def close(self) -> None:
        process = self._process
        self._process = None
        if process and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()


class NativeTerminal:
    """One private Swift TTY process per browser terminal tab."""

    def __init__(self, executable: str, profile: dict[str, Any]):
        self._process = subprocess.Popen(
            [executable, "--terminal"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, close_fds=True,
        )
        self._write_lock = threading.Lock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        reader = threading.Thread(target=self._read_events, daemon=True)
        reader.start()
        try:
            self._write(_profile_payload(profile))
            ready = self.read_event(timeout=20)
            if not ready or ready.get("event") != "ready":
                raise BridgeError((ready or {}).get("data") or "Swift SSH 终端未就绪")
        except Exception:
            self.close()
            raise

    def _read_events(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        self._events.put(event)
                except json.JSONDecodeError:
                    self._events.put({"event": "error", "data": "Swift 终端响应无效"})
        finally:
            self._events.put({"event": "closed"})

    def _write(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            assert self._process.stdin is not None
            try:
                self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise BridgeError(f"Swift 终端已关闭: {exc}") from exc

    def read_event(self, timeout: float = 0.2) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def send(self, data: str) -> None:
        self._write({"type": "data", "data": data})

    def resize(self, cols: int, rows: int) -> None:
        self._write({"type": "resize", "cols": cols, "rows": rows})

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()


def open_terminal(profile: dict[str, Any]) -> NativeTerminal:
    executable = os.environ.get("PVE_CLIENT_NATIVE_HELPER") or ""
    if not terminal_enabled():
        raise BridgeError("Swift SSH 终端未启用")
    return NativeTerminal(executable, profile)


_helpers: dict[str, _Helper] = {}
_helpers_lock = threading.Lock()


def call(operation: str, profile: dict[str, Any], *, channel: str = "main", timeout: int = 30, **kwargs: Any) -> Any:
    executable = os.environ.get("PVE_CLIENT_NATIVE_HELPER") or ""
    if not executable or not Path(executable).is_file():
        raise BridgeError("Swift 助手不存在")
    with _helpers_lock:
        helper = _helpers.get(channel)
        if helper is None or helper.executable != executable:
            helper = _Helper(executable)
            _helpers[channel] = helper
    return helper.call(operation, profile, timeout=timeout, **kwargs)


def close_all() -> None:
    with _helpers_lock:
        helpers = list(_helpers.values())
        _helpers.clear()
    for helper in helpers:
        helper.close()


atexit.register(close_all)
