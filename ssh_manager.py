"""SSH session manager. Separate connections per purpose to avoid lock contention."""
from __future__ import annotations

import io
import threading
import time
from typing import Any

import paramiko

from . import config_store, native_bridge


class SSHError(Exception):
    pass


def _open_client(profile: dict[str, Any], timeout: int = 12) -> paramiko.SSHClient:
    secrets = config_store.resolve_secrets(profile)
    host = secrets.get("domain") or secrets.get("host")
    if not host:
        raise SSHError("请填写主机 IP 或域名")
    port = int(secrets.get("port") or 22)
    username = secrets.get("username") or "root"
    client = paramiko.SSHClient()
    policy = (
        paramiko.AutoAddPolicy()
        if secrets.get("accept_unknown_host", True)
        else paramiko.RejectPolicy()
    )
    client.set_missing_host_key_policy(policy)
    connect_kwargs: dict[str, Any] = {
        "hostname": host,
        "port": port,
        "username": username,
        "timeout": timeout,
        "banner_timeout": timeout + 5,
        "auth_timeout": timeout + 5,
        "allow_agent": False,
        "look_for_keys": False,
    }
    auth = secrets.get("auth_method") or "password"
    if auth == "key":
        key_data = secrets.get("private_key") or ""
        passphrase = secrets.get("key_passphrase") or None
        if not key_data:
            raise SSHError("请选择或粘贴私钥内容")
        try:
            connect_kwargs["pkey"] = _load_private_key(key_data, passphrase)
        except SSHError:
            raise
        except Exception as exc:
            raise SSHError(f"私钥解析失败: {exc}") from exc
    else:
        password = secrets.get("password") or ""
        if not password:
            raise SSHError("请输入密码或改用密钥登录")
        connect_kwargs["password"] = password
    try:
        client.connect(**connect_kwargs)
    except paramiko.AuthenticationException as exc:
        raise SSHError("认证失败：用户名/密码/密钥不正确") from exc
    except SSHError:
        raise
    except Exception as exc:
        raise SSHError(f"连接失败 ({host}:{port}): {exc}") from exc
    return client


def _close_quiet(client: paramiko.SSHClient | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


class Session:
    """One profile, independent SSH clients for exec / sftp / terminal."""

    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        self._exec_lock = threading.RLock()
        self._monitor_lock = threading.RLock()
        self._sftp_lock = threading.RLock()
        self._exec_client: paramiko.SSHClient | None = None
        self._monitor_client: paramiko.SSHClient | None = None
        self._sftp_client: paramiko.SFTPClient | None = None
        self._sftp_ssh: paramiko.SSHClient | None = None
        self.last_used = time.time()

    def refresh_profile(self, profile: dict[str, Any]) -> None:
        self.profile = profile

    def connect(self) -> None:
        """Ensure exec channel works (used by connect API / tests)."""
        if native_bridge.enabled():
            self.exec("hostname", timeout=12)
            return
        with self._exec_lock:
            _close_quiet(self._exec_client)
            self._exec_client = None
            self._exec_client = _open_client(self.profile)
            self.last_used = time.time()

    def ensure(self) -> paramiko.SSHClient:
        with self._exec_lock:
            client = self._exec_client
            if client is not None:
                transport = client.get_transport()
                if transport is not None and transport.is_active():
                    self.last_used = time.time()
                    return client
            _close_quiet(self._exec_client)
            self._exec_client = _open_client(self.profile)
            self.last_used = time.time()
            return self._exec_client

    def exec(self, command: str, timeout: int = 30) -> dict[str, Any]:
        if native_bridge.enabled():
            try:
                result = native_bridge.call("exec", self.profile, timeout=timeout + 10, command=command)
                self.last_used = time.time()
                return result
            except native_bridge.BridgeError as exc:
                raise SSHError(f"Swift SSH 命令失败: {exc}") from exc
        with self._exec_lock:
            client = self.ensure()
            try:
                stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                code = stdout.channel.recv_exit_status()
                self.last_used = time.time()
                return {"code": code, "stdout": out, "stderr": err}
            except Exception as exc:
                _close_quiet(self._exec_client)
                self._exec_client = None
                raise SSHError(f"命令执行失败: {exc}") from exc

    def exec_monitor(self, command: str, timeout: int = 30) -> dict[str, Any]:
        """Run polling work on a separate SSH connection.

        Monitoring must never hold the interactive/PVE-operation command lock;
        otherwise a slow metric probe makes create dialogs and actions appear stuck.
        """
        if native_bridge.enabled():
            try:
                result = native_bridge.call("exec", self.profile, channel="monitor", timeout=timeout + 10, command=command)
                self.last_used = time.time()
                return result
            except native_bridge.BridgeError as exc:
                raise SSHError(f"Swift SSH 监控失败: {exc}") from exc
        with self._monitor_lock:
            client = self._monitor_client
            transport = client.get_transport() if client is not None else None
            if transport is None or not transport.is_active():
                _close_quiet(client)
                client = _open_client(self.profile)
                self._monitor_client = client
            try:
                _, stdout, stderr = client.exec_command(command, timeout=timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                code = stdout.channel.recv_exit_status()
                self.last_used = time.time()
                return {"code": code, "stdout": out, "stderr": err}
            except Exception as exc:
                _close_quiet(self._monitor_client)
                self._monitor_client = None
                raise SSHError(f"监控通道执行失败: {exc}") from exc

    def sftp_client(self) -> paramiko.SFTPClient:
        with self._sftp_lock:
            if self._sftp_client is not None and self._sftp_ssh is not None:
                transport = self._sftp_ssh.get_transport()
                if transport is not None and transport.is_active():
                    return self._sftp_client
            self._close_sftp()
            self._sftp_ssh = _open_client(self.profile)
            try:
                self._sftp_client = self._sftp_ssh.open_sftp()
            except Exception as exc:
                _close_quiet(self._sftp_ssh)
                self._sftp_ssh = None
                raise SSHError(f"SFTP 打开失败: {exc}") from exc
            return self._sftp_client

    def open_shell(self, width: int = 120, height: int = 32):
        """Dedicated SSH client + interactive channel for terminal."""
        client = _open_client(self.profile)
        try:
            channel = client.invoke_shell(term="xterm-256color", width=width, height=height)
            channel.settimeout(0.2)
        except Exception as exc:
            _close_quiet(client)
            raise SSHError(f"打开终端失败: {exc}") from exc
        return client, channel

    def _close_sftp(self) -> None:
        if self._sftp_client is not None:
            try:
                self._sftp_client.close()
            except Exception:
                pass
            self._sftp_client = None
        _close_quiet(self._sftp_ssh)
        self._sftp_ssh = None

    def close(self) -> None:
        with self._exec_lock:
            _close_quiet(self._exec_client)
            self._exec_client = None
        with self._monitor_lock:
            _close_quiet(self._monitor_client)
            self._monitor_client = None
        with self._sftp_lock:
            self._close_sftp()


def _load_private_key(key_data: str, passphrase: str | None):
    data = key_data.replace("\r\n", "\n")
    if not data.endswith("\n"):
        data += "\n"
    bio = io.StringIO(data)
    for loader in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
        try:
            bio.seek(0)
            return loader.from_private_key(bio, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise SSHError("该私钥需要口令，请填写密钥口令") from None
        except Exception:
            continue
    try:
        from cryptography.hazmat.primitives import serialization

        priv = serialization.load_ssh_private_key(
            data.encode("utf-8"),
            password=passphrase.encode("utf-8") if passphrase else None,
        )
        return paramiko.PKey.from_private_key(io.StringIO(data), password=passphrase)
    except Exception:
        pass
    raise SSHError("无法识别的私钥格式（支持 RSA/ECDSA/Ed25519）")


class SessionPool:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._blocked: set[str] = set()
        self._lock = threading.Lock()

    def get(self, profile_id: str) -> Session:
        with self._lock:
            if profile_id in self._blocked:
                raise SSHError("连接已断开，请先点「连接」")
            sess = self._sessions.get(profile_id)
            profile = config_store.get_profile(profile_id)
            if not profile:
                raise SSHError("连接配置不存在")
            if sess is None:
                sess = Session(profile)
                self._sessions[profile_id] = sess
            else:
                sess.refresh_profile(profile)
            return sess

    def drop(self, profile_id: str) -> None:
        with self._lock:
            sess = self._sessions.pop(profile_id, None)
        if sess:
            sess.close()

    def block(self, profile_id: str) -> None:
        self.drop(profile_id)
        with self._lock:
            self._blocked.add(profile_id)

    def unblock(self, profile_id: str) -> None:
        with self._lock:
            self._blocked.discard(profile_id)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            sess.close()


pool = SessionPool()


def test_connection(profile: dict[str, Any]) -> dict[str, Any]:
    sess = Session(profile)
    try:
        sess.connect()
        info = sess.exec(
            "hostname; uname -a; cat /etc/os-release 2>/dev/null | head -5; "
            "pveversion 2>/dev/null | head -1 || true",
            timeout=12,
        )
        return {
            "ok": True,
            "host": profile.get("domain") or profile.get("host"),
            "port": profile.get("port"),
            "output": info["stdout"],
        }
    except SSHError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        sess.close()
