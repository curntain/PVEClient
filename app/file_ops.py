"""SFTP file operations."""
from __future__ import annotations

import posixpath
import stat
import base64
from typing import Any

from . import native_bridge
from .ssh_manager import Session, SSHError

MAX_EDIT_BYTES = 2 * 1024 * 1024  # 2MB editor limit
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512MB


def _native(session: Session, operation: str, **kwargs: Any) -> Any:
    try:
        return native_bridge.call(operation, session.profile, timeout=120, **kwargs)
    except native_bridge.BridgeError as exc:
        raise SSHError(f"Swift 文件操作失败: {exc}") from exc


def _entry_from_native(entry: dict[str, Any]) -> dict[str, Any]:
    mode = int(entry.get("permissions") or 0)
    return {
        "name": entry.get("name") or "",
        "path": entry.get("path") or "",
        "is_dir": bool(entry.get("is_dir")),
        "size": int(entry.get("size") or 0),
        "mtime": float(entry.get("mtime") or 0),
        "mode": stat.filemode(mode),
        "mode_octal": oct(mode)[-3:] if mode else "000",
    }


def _norm(path: str) -> str:
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return posixpath.normpath(path)


def _collect(sftp, path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for attr in sftp.listdir_attr(path):
        name = attr.filename
        if name in (".", ".."):
            continue
        full = posixpath.join(path, name)
        mode = attr.st_mode or 0
        is_dir = stat.S_ISDIR(mode)
        entries.append(
            {
                "name": name,
                "path": full,
                "is_dir": is_dir,
                "size": attr.st_size or 0,
                "mtime": attr.st_mtime or 0,
                "mode": stat.filemode(mode),
                "mode_octal": oct(mode)[-3:] if mode else "000",
            }
        )
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return entries


def list_dir(session: Session, path: str = "/") -> list[dict[str, Any]]:
    path = _norm(path)
    if native_bridge.enabled():
        entries = [_entry_from_native(row) for row in _native(session, "fileList", path=path)]
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return entries
    try:
        sftp = session.sftp_client()
        return _collect(sftp, path)
    except IOError as exc:
        try:
            session._close_sftp()
            sftp = session.sftp_client()
            return _collect(sftp, path)
        except Exception as exc2:
            raise SSHError(f"无法列出目录 {path}: {exc2}") from exc2
    except SSHError:
        raise


def read_text(session: Session, path: str) -> dict[str, Any]:
    path = _norm(path)
    if native_bridge.enabled():
        attributes = _native(session, "fileStat", path=path)
        size = int(attributes.get("size") or 0)
        if size > MAX_EDIT_BYTES:
            raise SSHError(f"文件过大（{size} 字节），超过编辑器 2MB 上限")
        row = _native(session, "fileRead", path=path)
        data = base64.b64decode(row["data"])
        if len(data) > MAX_EDIT_BYTES:
            raise SSHError(f"文件过大（{len(data)} 字节），超过编辑器 2MB 上限")
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = data.decode("utf-8", errors="replace")
            encoding = "utf-8-lossy"
        return {"path": path, "content": content, "encoding": encoding, "size": len(data)}
    sftp = session.sftp_client()
    try:
        with sftp.open(path, "rb") as f:
            size = f.stat().st_size
            if size > MAX_EDIT_BYTES:
                raise SSHError(f"文件过大（{size} 字节），超过编辑器 2MB 上限")
            data = f.read()
    except IOError as exc:
        raise SSHError(f"读取失败: {exc}") from exc
    try:
        text = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        encoding = "utf-8-lossy"
    return {"path": path, "content": text, "encoding": encoding, "size": len(data)}


def write_text(session: Session, path: str, content: str) -> dict[str, Any]:
    path = _norm(path)
    if native_bridge.enabled():
        data = content.encode("utf-8")
        if len(data) > MAX_EDIT_BYTES:
            raise SSHError("内容过大，无法写入")
        row = _native(session, "fileWrite", path=path, data=base64.b64encode(data).decode("ascii"))
        return {"path": path, "bytes": row["bytes"]}
    sftp = session.sftp_client()
    data = content.encode("utf-8")
    if len(data) > MAX_EDIT_BYTES:
        raise SSHError("内容过大，无法写入")
    try:
        # write via temp then rename for a bit more safety
        tmp = path + ".pveclient.tmp"
        with sftp.open(tmp, "wb") as f:
            f.write(data)
        try:
            sftp.chmod(tmp, 0o644)
        except Exception:
            pass
        try:
            sftp.posix_rename(tmp, path)
        except Exception:
            # fallback
            try:
                sftp.remove(path)
            except Exception:
                pass
            sftp.rename(tmp, path)
    except IOError as exc:
        raise SSHError(f"写入失败: {exc}") from exc
    return {"path": path, "bytes": len(data)}


def mkdir(session: Session, path: str) -> dict[str, Any]:
    path = _norm(path)
    if native_bridge.enabled():
        return _native(session, "fileMkdir", path=path)
    sftp = session.sftp_client()
    try:
        sftp.mkdir(path)
    except IOError as exc:
        raise SSHError(f"创建目录失败: {exc}") from exc
    return {"path": path}


def remove(session: Session, path: str, recursive: bool = False) -> dict[str, Any]:
    path = _norm(path)
    if path in ("/", "/root", "/home", "/etc", "/var", "/usr"):
        raise SSHError("拒绝删除关键系统路径")
    if native_bridge.enabled():
        if recursive:
            return _native(session, "fileRemoveRecursive", path=path)
        return _native(session, "fileRemove", path=path)
    sftp = session.sftp_client()
    try:
        attr = sftp.stat(path)
        if stat.S_ISDIR(attr.st_mode):
            if recursive:
                _rmtree(sftp, path)
            else:
                sftp.rmdir(path)
        else:
            sftp.remove(path)
    except IOError as exc:
        raise SSHError(f"删除失败: {exc}") from exc
    return {"path": path}


def _rmtree(sftp, path: str) -> None:
    for attr in sftp.listdir_attr(path):
        child = posixpath.join(path, attr.filename)
        if stat.S_ISDIR(attr.st_mode or 0):
            _rmtree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def rename(session: Session, src: str, dst: str) -> dict[str, Any]:
    src = _norm(src)
    dst = _norm(dst)
    if native_bridge.enabled():
        return _native(session, "fileRename", path=src, to=dst)
    sftp = session.sftp_client()
    try:
        sftp.posix_rename(src, dst)
    except Exception:
        try:
            sftp.rename(src, dst)
        except IOError as exc:
            raise SSHError(f"重命名失败: {exc}") from exc
    return {"src": src, "dst": dst}


def upload(session: Session, remote_path: str, local_path: str) -> dict[str, Any]:
    remote_path = _norm(remote_path)
    if native_bridge.enabled():
        return _native(session, "fileUpload", path=remote_path, localPath=str(local_path))
    sftp = session.sftp_client()
    try:
        sftp.put(local_path, remote_path)
        size = sftp.stat(remote_path).st_size
    except IOError as exc:
        raise SSHError(f"上传失败: {exc}") from exc
    return {"path": remote_path, "size": size}


def download(session: Session, remote_path: str, local_path: str) -> dict[str, Any]:
    remote_path = _norm(remote_path)
    if native_bridge.enabled():
        return _native(session, "fileDownload", path=remote_path, localPath=str(local_path))
    sftp = session.sftp_client()
    try:
        sftp.get(remote_path, local_path)
    except IOError as exc:
        raise SSHError(f"下载失败: {exc}") from exc
    return {"path": remote_path, "local": local_path}


def stat_path(session: Session, path: str) -> dict[str, Any]:
    path = _norm(path)
    if native_bridge.enabled():
        entry = _entry_from_native(_native(session, "fileStat", path=path))
        return {key: entry[key] for key in ("path", "is_dir", "size", "mtime", "mode")}
    sftp = session.sftp_client()
    try:
        attr = sftp.stat(path)
    except IOError as exc:
        raise SSHError(f"路径不存在: {path}") from exc
    mode = attr.st_mode or 0
    return {
        "path": path,
        "is_dir": stat.S_ISDIR(mode),
        "size": attr.st_size or 0,
        "mtime": attr.st_mtime or 0,
        "mode": stat.filemode(mode),
    }


def mkdir_p(session: Session, path: str) -> None:
    path = _norm(path)
    if native_bridge.enabled():
        _native(session, "fileMkdirP", path=path)
        return
    sftp = session.sftp_client()
    parts = [p for p in path.split("/") if p]
    cur = ""
    for part in parts:
        cur = cur + "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            try:
                sftp.mkdir(cur)
            except IOError:
                pass
