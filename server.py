"""FastAPI application: REST + WebSocket terminal."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config_store, embed_proxy, embed_server, file_ops, monitoring, native_bridge, pve_ops
from .env_file import load_client_env
from .ssh_manager import SSHError, pool, test_connection

# let a client.env supply PVE_CLIENT_* settings when the app is started without
# main.py (e.g. `uvicorn app.server:app` in development)
load_client_env()


def _static_dir() -> Path:
    import sys

    if getattr(sys, "frozen", False):
        candidates = [
            Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "static",
            Path(sys.executable).parent / "static",
            Path(sys.executable).parent / "_internal" / "static",
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]
    return Path(__file__).resolve().parent.parent / "static"


STATIC_DIR = _static_dir()


async def _swift_terminal_websocket(websocket: WebSocket, profile: dict[str, Any]) -> None:
    """Forward browser terminal input/output through a dedicated Swift TTY."""
    try:
        terminal = await asyncio.to_thread(native_bridge.open_terminal, profile)
    except native_bridge.BridgeError as exc:
        await websocket.send_text(json.dumps({"type": "error", "data": str(exc)}))
        await websocket.close()
        return
    await websocket.send_text(json.dumps({"type": "ready", "data": "终端已连接"}))

    async def forward_output() -> None:
        try:
            while True:
                event = await asyncio.to_thread(terminal.read_event, 0.25)
                if not event:
                    continue
                kind = event.get("event")
                if kind == "data":
                    await websocket.send_text(json.dumps({"type": "data", "data": event.get("data") or ""}))
                elif kind == "error":
                    await websocket.send_text(json.dumps({"type": "error", "data": event.get("data") or "终端出错"}))
                    break
                elif kind == "closed":
                    break
        except Exception:
            pass
        finally:
            try:
                await websocket.send_text(json.dumps({"type": "closed", "data": "会话已结束"}))
            except Exception:
                pass

    output_task = asyncio.create_task(forward_output())
    try:
        while True:
            input_task = asyncio.create_task(websocket.receive_text())
            done, _ = await asyncio.wait({input_task, output_task}, return_when=asyncio.FIRST_COMPLETED)
            if output_task in done:
                input_task.cancel()
                await asyncio.gather(input_task, return_exceptions=True)
                break
            msg = input_task.result()
            try:
                payload = json.loads(msg)
            except Exception:
                payload = {"type": "data", "data": msg}
            kind = payload.get("type")
            if kind == "resize":
                await asyncio.to_thread(terminal.resize, int(payload.get("cols") or 120), int(payload.get("rows") or 32))
            elif kind == "data" and payload.get("data"):
                await asyncio.to_thread(terminal.send, str(payload["data"]))
            elif kind == "close":
                break
    except (WebSocketDisconnect, native_bridge.BridgeError):
        pass
    finally:
        await asyncio.to_thread(terminal.close)
        output_task.cancel()
        await asyncio.gather(output_task, return_exceptions=True)


class ProfileIn(BaseModel):
    id: str | None = None
    name: str = ""
    host: str = ""
    domain: str = ""
    port: int = 22
    username: str = "root"
    auth_method: str = "password"
    password: str = ""
    private_key: str = ""
    key_passphrase: str = ""
    note: str = ""
    accept_unknown_host: bool = True


class ExecIn(BaseModel):
    profile_id: str
    command: str
    timeout: int = 30


class FileWriteIn(BaseModel):
    profile_id: str
    path: str
    content: str


class FileMkdirIn(BaseModel):
    profile_id: str
    path: str


class FileRemoveIn(BaseModel):
    profile_id: str
    path: str
    recursive: bool = False


class FileRenameIn(BaseModel):
    profile_id: str
    src: str
    dst: str


class CreateVmIn(BaseModel):
    profile_id: str
    vmid: str
    name: str
    cores: int = 2
    memory: int = 2048
    disk_gb: int = 32
    storage: str = "local-lvm"
    iso: str = ""
    bridge: str = "vmbr0"
    net_model: str = "virtio"


class CreateCtIn(BaseModel):
    profile_id: str
    vmid: str
    hostname: str
    ostemplate: str
    memory: int = 512
    cores: int = 1
    disk_gb: int = 8
    storage: str = "local-lvm"
    password: str = ""
    unprivileged: bool = True
    start: bool = False


class GuestActionIn(BaseModel):
    profile_id: str
    vmid: str
    gtype: str = "qemu"
    action: str


class ServiceLinkIn(BaseModel):
    profile_id: str
    vmid: str
    label: str = ""
    web_url: str = ""
    kind: str = ""
    note: str = ""
    public_url: str = ""


class EmbedOpenIn(BaseModel):
    profile_id: str
    vmid: str


class EmbedKeyIn(BaseModel):
    key: str


def _session(profile_id: str):
    try:
        return pool.get(profile_id)
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def create_app() -> FastAPI:
    app = FastAPI(title="PVE Remote Client", docs_url=None, redoc_url=None)
    auth.install(app, public_paths=("/api/health",))

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>PVE Client</h1>", status_code=200)
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    def api_health(request: Request) -> Any:
        """Unauthenticated capability probe used by the login-aware UI."""
        return JSONResponse(
            {"ok": True, "auth": auth.auth_required(request.scope)},
            headers={"Cache-Control": "no-store"},
        )

    # ----- profiles -----
    @app.get("/api/profiles")
    def api_list_profiles() -> Any:
        return config_store.list_profiles()

    @app.post("/api/profiles")
    def api_save_profile(body: ProfileIn) -> Any:
        return config_store.save_profile(body.model_dump())

    @app.delete("/api/profiles/{profile_id}")
    def api_delete_profile(profile_id: str) -> Any:
        pool.drop(profile_id)
        ok = config_store.delete_profile(profile_id)
        if not ok:
            raise HTTPException(status_code=404, detail="配置不存在")
        return {"ok": True}

    @app.post("/api/profiles/test")
    def api_test_profile(body: ProfileIn) -> Any:
        payload = body.model_dump()
        # if password empty and profile exists, reuse stored secrets for test convenience
        if payload.get("id") and not payload.get("password") and not payload.get("private_key"):
            stored = config_store.get_profile(payload["id"])
            if stored:
                for k in ("password", "private_key", "key_passphrase"):
                    if stored.get(k):
                        payload[k] = stored[k]
        return test_connection(payload)

    @app.post("/api/connect/{profile_id}")
    def api_connect(profile_id: str) -> Any:
        pool.unblock(profile_id)
        sess = _session(profile_id)
        try:
            sess.connect()
            res = sess.exec("hostname; whoami; date", timeout=15)
            return {"ok": True, "output": res["stdout"]}
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/disconnect/{profile_id}")
    def api_disconnect(profile_id: str) -> Any:
        pool.block(profile_id)
        return {"ok": True}

    # ----- monitoring -----
    @app.get("/api/metrics/{profile_id}")
    def api_metrics(profile_id: str) -> Any:
        sess = _session(profile_id)
        try:
            return monitoring.collect_metrics(sess)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/node/{profile_id}")
    def api_node(profile_id: str) -> Any:
        sess = _session(profile_id)
        try:
            return pve_ops.node_summary(sess)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ----- guests -----
    @app.get("/api/guests/{profile_id}")
    def api_guests(profile_id: str) -> Any:
        sess = _session(profile_id)
        try:
            guests = pve_ops.list_guests(sess)
            links = config_store.list_service_entries(profile_id)
            for row in guests.get("vms", []) + guests.get("cts", []):
                extra = links.get(str(row.get("vmid"))) or {}
                row["label"] = extra.get("label") or ""
                row["web_url"] = extra.get("web_url") or ""
                row["kind"] = extra.get("kind") or ""
                row["note"] = extra.get("note") or ""
                row["public_url"] = extra.get("public_url") or ""
            return guests
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/guests-metrics/{profile_id}")
    def api_guests_metrics(profile_id: str) -> Any:
        sess = _session(profile_id)
        try:
            return pve_ops.guest_metrics(sess)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.api_route(
        "/embed/{profile_id}/{vmid}/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def api_embed_proxy(profile_id: str, vmid: str, path: str, request: Request) -> Any:
        return await embed_proxy.proxy_embed(profile_id, vmid, path, request)

    # ----- in-app system management (one dedicated loopback origin each) -----
    @app.post("/api/embed/open")
    def api_embed_open(body: EmbedOpenIn) -> Any:
        info = embed_proxy.set_target(body.profile_id, str(body.vmid))
        key, port = embed_server.ensure_embed_server(body.profile_id, str(body.vmid))
        info["key"] = key
        info["port"] = port
        info["origin"] = f"http://127.0.0.1:{port}/"
        info["local_origin"] = info["origin"]
        info["public_origin"] = info.get("public_url") or ""
        return info

    @app.get("/api/embed/state")
    def api_embed_state(key: str) -> Any:
        info = embed_proxy.target_state(key)
        port = embed_server.embed_port(key)
        info["key"] = key
        info["port"] = port
        info["origin"] = f"http://127.0.0.1:{port}/" if port else ""
        info["local_origin"] = info["origin"]
        info["public_origin"] = info.get("public_url") or ""
        return info

    @app.post("/api/embed/back")
    def api_embed_back(body: EmbedKeyIn) -> Any:
        info = embed_proxy.go_back(body.key)
        port = embed_server.embed_port(body.key)
        info["key"] = body.key
        info["port"] = port
        info["origin"] = f"http://127.0.0.1:{port}/" if port else ""
        info["local_origin"] = info["origin"]
        info["public_origin"] = info.get("public_url") or ""
        return info

    @app.get("/api/services/{profile_id}")
    def api_list_services(profile_id: str) -> Any:
        return config_store.list_service_entries(profile_id)

    @app.post("/api/services")
    def api_save_service(body: ServiceLinkIn) -> Any:
        return config_store.upsert_service_entry(
            body.profile_id,
            str(body.vmid),
            {
                "label": body.label,
                "web_url": body.web_url,
                "kind": body.kind,
                "note": body.note,
                "public_url": body.public_url,
            },
        )

    @app.post("/api/guests/action")
    def api_guest_action(body: GuestActionIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return pve_ops.guest_action(sess, body.vmid, body.gtype, body.action)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/pve/helpers/{profile_id}")
    def api_pve_helpers(profile_id: str) -> Any:
        sess = _session(profile_id)
        try:
            return pve_ops.helper_bundle(sess)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/guests/create-vm")
    def api_create_vm(body: CreateVmIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return pve_ops.create_vm(
                sess,
                vmid=body.vmid,
                name=body.name,
                cores=body.cores,
                memory=body.memory,
                disk_gb=body.disk_gb,
                storage=body.storage,
                iso=body.iso,
                bridge=body.bridge,
                net_model=body.net_model,
            )
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/guests/create-ct")
    def api_create_ct(body: CreateCtIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return pve_ops.create_ct(
                sess,
                vmid=body.vmid,
                hostname=body.hostname,
                ostemplate=body.ostemplate,
                memory=body.memory,
                cores=body.cores,
                disk_gb=body.disk_gb,
                storage=body.storage,
                password=body.password,
                unprivileged=body.unprivileged,
                start=body.start,
            )
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ----- files -----
    @app.get("/api/fs/list")
    def api_fs_list(profile_id: str, path: str = "/") -> Any:
        sess = _session(profile_id)
        try:
            return {"path": file_ops._norm(path), "entries": file_ops.list_dir(sess, path)}
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/fs/read")
    def api_fs_read(profile_id: str, path: str) -> Any:
        sess = _session(profile_id)
        try:
            return file_ops.read_text(sess, path)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fs/write")
    def api_fs_write(body: FileWriteIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return file_ops.write_text(sess, body.path, body.content)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fs/mkdir")
    def api_fs_mkdir(body: FileMkdirIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return file_ops.mkdir(sess, body.path)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fs/remove")
    def api_fs_remove(body: FileRemoveIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return file_ops.remove(sess, body.path, body.recursive)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fs/rename")
    def api_fs_rename(body: FileRenameIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return file_ops.rename(sess, body.src, body.dst)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fs/upload")
    async def api_fs_upload(
        profile_id: str,
        remote_dir: str = "/root",
        file: UploadFile = File(...),
    ) -> Any:
        sess = _session(profile_id)
        suffix = Path(file.filename or "upload.bin").name
        tmp_dir = tempfile.mkdtemp(prefix="pveclient_up_")
        local = Path(tmp_dir) / suffix
        try:
            with local.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    output.write(chunk)
            remote_path = file_ops._norm(str(Path(remote_dir) / suffix).replace("\\", "/"))
            file_ops.mkdir_p(sess, remote_dir)
            return file_ops.upload(sess, remote_path, str(local))
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            try:
                local.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except Exception:
                pass

    @app.get("/api/fs/download")
    def api_fs_download(profile_id: str, path: str) -> Any:
        from starlette.background import BackgroundTask

        sess = _session(profile_id)
        tmp_dir = tempfile.mkdtemp(prefix="pveclient_dl_")
        # avoid illegal filename chars on Windows
        raw_name = Path(path).name or "download.bin"
        name = "".join(ch if ch.isalnum() or ch in "._- ()（）" else "_" for ch in raw_name) or "download.bin"
        local = Path(tmp_dir) / "payload.bin"
        try:
            file_ops.download(sess, path, str(local))
        except SSHError as exc:
            try:
                local.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except Exception:
                pass
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def _cleanup() -> None:
            try:
                local.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except Exception:
                pass

        return FileResponse(
            str(local),
            filename=name,
            media_type="application/octet-stream",
            background=BackgroundTask(_cleanup),
        )

    # ----- exec -----
    @app.post("/api/exec")
    def api_exec(body: ExecIn) -> Any:
        sess = _session(body.profile_id)
        try:
            return sess.exec(body.command, timeout=body.timeout)
        except SSHError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ----- terminal websocket -----
    @app.websocket("/ws/terminal/{profile_id}")
    async def ws_terminal(websocket: WebSocket, profile_id: str) -> None:
        await websocket.accept()
        if not config_store.get_profile(profile_id):
            await websocket.send_text(json.dumps({"type": "error", "data": "连接配置不存在"}))
            await websocket.close()
            return
        try:
            sess = pool.get(profile_id)
        except SSHError as exc:
            await websocket.send_text(json.dumps({"type": "error", "data": str(exc)}))
            await websocket.close()
            return

        await websocket.send_text(json.dumps({"type": "data", "data": "正在建立 SSH 终端…\r\n"}))
        if native_bridge.terminal_enabled():
            await _swift_terminal_websocket(websocket, sess.profile)
            return
        try:
            client, channel = await asyncio.to_thread(sess.open_shell, 120, 32)
        except SSHError as exc:
            await websocket.send_text(json.dumps({"type": "error", "data": str(exc)}))
            await websocket.close()
            return
        except Exception as exc:
            await websocket.send_text(json.dumps({"type": "error", "data": f"终端连接失败: {exc}"}))
            await websocket.close()
            return

        stop = threading.Event()
        loop = asyncio.get_running_loop()

        def reader() -> None:
            try:
                while not stop.is_set():
                    try:
                        if channel.recv_ready():
                            data = channel.recv(8192)
                            if not data:
                                break
                            text = data.decode("utf-8", errors="replace")
                            fut = asyncio.run_coroutine_threadsafe(
                                websocket.send_text(json.dumps({"type": "data", "data": text})),
                                loop,
                            )
                            fut.result(timeout=5)
                        elif channel.closed or channel.eof_received:
                            break
                        else:
                            time.sleep(0.03)
                    except Exception:
                        break
            finally:
                try:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_text(json.dumps({"type": "closed", "data": "会话已结束"})),
                        loop,
                    )
                except Exception:
                    pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        await websocket.send_text(json.dumps({"type": "ready", "data": "终端已连接"}))

        try:
            while True:
                msg = await websocket.receive_text()
                try:
                    payload = json.loads(msg)
                except Exception:
                    payload = {"type": "data", "data": msg}
                if payload.get("type") == "resize":
                    try:
                        channel.resize_pty(
                            width=int(payload.get("cols") or 120),
                            height=int(payload.get("rows") or 32),
                        )
                    except Exception:
                        pass
                elif payload.get("type") == "data":
                    data = payload.get("data") or ""
                    if data:
                        try:
                            channel.send(data)
                        except Exception as exc:
                            await websocket.send_text(json.dumps({"type": "error", "data": str(exc)}))
                            break
                elif payload.get("type") == "close":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            try:
                channel.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    return app


app = create_app()
