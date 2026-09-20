"""PVE operations executed over SSH (qm / pct / pvesh)."""
from __future__ import annotations

import copy
import json
import re
import threading
import time
import weakref
from typing import Any

from . import native_bridge
from .ssh_manager import SSHError, Session


_guest_cache: "weakref.WeakKeyDictionary[Session, tuple[float, list[dict[str, Any]]]]" = weakref.WeakKeyDictionary()
_guest_profile_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_guest_cache_lock = threading.RLock()


def _guest_cache_key(session: Session) -> str:
    """Stable cache key so a reconnect doesn't erase the last good meters."""
    profile = getattr(session, "profile", None)
    if not isinstance(profile, dict):
        return f"session:{id(session)}"
    if profile.get("id"):
        return f"profile:{profile['id']}"
    host = profile.get("domain") or profile.get("host") or ""
    return f"host:{host}:{profile.get('port') or 22}:{profile.get('username') or 'root'}"


def _cached_guest_sample(session: Session) -> tuple[float, list[dict[str, Any]]] | None:
    direct = _guest_cache.get(session)
    stable = _guest_profile_cache.get(_guest_cache_key(session))
    if direct and stable:
        return direct if direct[0] >= stable[0] else stable
    return direct or stable


def _store_guest_sample(session: Session, sampled_at: float, rows: list[dict[str, Any]]) -> None:
    snapshot = copy.deepcopy(rows)
    _guest_cache[session] = (sampled_at, snapshot)
    _guest_profile_cache[_guest_cache_key(session)] = (sampled_at, snapshot)


def _cached_guest_response(
    cached: tuple[float, list[dict[str, Any]]] | None,
    *,
    stale: bool,
) -> dict[str, Any] | None:
    if not cached:
        return None
    return {
        "metrics": copy.deepcopy(cached[1]),
        "cached": True,
        "stale": stale,
        "cache_age_seconds": round(max(0.0, time.monotonic() - cached[0]), 1),
    }


def _swift_pve(session: Session, operation: str, *, monitor: bool = False, timeout: int = 30, **kwargs: Any) -> dict[str, Any]:
    try:
        return native_bridge.call(
            operation, session.profile, channel="monitor" if monitor else "main", timeout=timeout,
            **kwargs,
        )
    except native_bridge.BridgeError as exc:
        raise SSHError(f"Swift PVE 操作失败: {exc}") from exc


def _cpu_percent(raw: Any) -> float:
    """PVE cpu is usually 0..1 ratio; some endpoints may already return percent."""
    try:
        v = float(raw or 0)
    except Exception:
        return 0.0
    if v <= 1.0:
        return round(v * 100.0, 1)
    return round(min(v, 100.0), 1)


def _row_from_item(item: dict[str, Any], gtype: str) -> dict[str, Any]:
    vmid = item.get("vmid")
    cpu = _cpu_percent(item.get("cpu"))
    mem = float(item.get("mem") or 0)
    maxmem = float(item.get("maxmem") or 0)
    disk = float(item.get("disk") or 0)
    maxdisk = float(item.get("maxdisk") or 0)
    # LXC sometimes reports maxdisk but disk only after guest agent; keep both
    disk_pct = round(disk * 100.0 / maxdisk, 1) if maxdisk > 0 else 0.0
    mem_pct = round(mem * 100.0 / maxmem, 1) if maxmem > 0 else 0.0
    return {
        "vmid": str(vmid) if vmid is not None else "",
        "type": gtype,
        "name": item.get("name") or item.get("hostname") or "",
        "status": item.get("status") or "unknown",
        "cpu_percent": cpu,
        "cpus": item.get("cpus") or 0,
        "mem": int(mem),
        "maxmem": int(maxmem),
        "mem_percent": mem_pct,
        "disk": int(disk),
        "maxdisk": int(maxdisk),
        "disk_percent": disk_pct,
        "netin": int(item.get("netin") or 0),
        "netout": int(item.get("netout") or 0),
        "netin_bps": 0.0,
        "netout_bps": 0.0,
        "net_sampled": False,
        "uptime": int(item.get("uptime") or 0),
    }


def _preserve_partial_row(
    row: dict[str, Any], item: dict[str, Any], previous: dict[str, Any] | None
) -> None:
    """Keep good fields when PVE briefly returns a partial resource row."""
    if not previous:
        return
    if "cpu" not in item:
        row["cpu_percent"] = previous.get("cpu_percent", row["cpu_percent"])
    if "cpus" not in item:
        row["cpus"] = previous.get("cpus", row["cpus"])
    if "mem" not in item:
        row["mem"] = previous.get("mem", row["mem"])
    if "maxmem" not in item:
        row["maxmem"] = previous.get("maxmem", row["maxmem"])
    row["mem_percent"] = round(row["mem"] * 100.0 / row["maxmem"], 1) if row["maxmem"] > 0 else 0.0
    if "disk" not in item:
        row["disk"] = previous.get("disk", row["disk"])
    if "maxdisk" not in item:
        row["maxdisk"] = previous.get("maxdisk", row["maxdisk"])
    row["disk_percent"] = round(row["disk"] * 100.0 / row["maxdisk"], 1) if row["maxdisk"] > 0 else 0.0
    if "uptime" not in item:
        row["uptime"] = previous.get("uptime", row["uptime"])


def _apply_network_rates(
    row: dict[str, Any], item: dict[str, Any], previous: dict[str, Any] | None, elapsed: float
) -> None:
    if "netin" not in item or "netout" not in item:
        if previous:
            for key in ("netin", "netout", "netin_bps", "netout_bps", "net_sampled"):
                if key in previous:
                    row[key] = previous[key]
        return
    if not previous or elapsed <= 0:
        return
    old_in = int(previous.get("netin") or 0)
    old_out = int(previous.get("netout") or 0)
    new_in = int(row.get("netin") or 0)
    new_out = int(row.get("netout") or 0)
    # Counters reset when a guest restarts; never display a negative spike.
    row["netin_bps"] = round(max(0, new_in - old_in) / elapsed, 1)
    row["netout_bps"] = round(max(0, new_out - old_out) / elapsed, 1)
    row["net_sampled"] = new_in >= old_in and new_out >= old_out


def guest_metrics(session: Session) -> dict[str, Any]:
    """Fetch all VM/CT usage in one fast PVE call, with stale-on-error data."""
    now = time.monotonic()
    with _guest_cache_lock:
        cached = _cached_guest_sample(session)
        if cached and now - cached[0] < 3.0:
            return _cached_guest_response(cached, stale=False) or {}

    metrics: list[dict[str, Any]] = []
    try:
        if native_bridge.enabled():
            res = _swift_pve(session, "pveMetrics", monitor=True, timeout=18)
        else:
            monitor_exec = getattr(session, "exec_monitor", None)
            if monitor_exec is None:
                monitor_exec = session.exec
            res = monitor_exec(
                "pvesh get /cluster/resources --type vm --output-format json 2>/dev/null",
                timeout=8,
            )
    except SSHError:
        with _guest_cache_lock:
            cached = _cached_guest_sample(session)
            fallback = _cached_guest_response(cached, stale=True)
        if fallback:
            return fallback
        raise

    raw = (res["stdout"] or "").strip()
    if raw.startswith("["):
        try:
            previous_by_id = {
                str(row.get("vmid")): row for row in (cached[1] if cached else [])
            }
            elapsed = max(0.001, now - cached[0]) if cached else 0.0
            for item in json.loads(raw):
                if not isinstance(item, dict):
                    continue
                gtype = "lxc" if item.get("type") == "lxc" else "qemu"
                row = _row_from_item(item, gtype)
                if row["vmid"]:
                    previous = previous_by_id.get(row["vmid"])
                    _preserve_partial_row(row, item, previous)
                    _apply_network_rates(row, item, previous, elapsed)
                    metrics.append(row)
        except Exception:
            pass
    if not metrics:
        with _guest_cache_lock:
            cached = _cached_guest_sample(session)
            fallback = _cached_guest_response(cached, stale=True)
        if fallback:
            return fallback
        raise SSHError("未能读取 PVE 子系统监控数据")

    metrics.sort(key=lambda row: int(row["vmid"]) if row["vmid"].isdigit() else 999999)
    with _guest_cache_lock:
        _store_guest_sample(session, now, metrics)
    return {"metrics": copy.deepcopy(metrics), "cached": False, "stale": False, "cache_age_seconds": 0.0}


def list_guests(session: Session) -> dict[str, Any]:
    if native_bridge.enabled():
        res = _swift_pve(session, "pveGuests", timeout=25)
        if res["code"] != 0:
            raise SSHError(res["stderr"] or "未能读取 PVE 客户端列表")
        vms: list[dict[str, Any]] = []
        cts: list[dict[str, Any]] = []
        for item in json.loads(res["stdout"] or "[]"):
            if not isinstance(item, dict) or item.get("vmid") is None:
                continue
            kind = "lxc" if item.get("type") == "lxc" else "qemu"
            row = {
                "vmid": str(item["vmid"]),
                "name": item.get("name") or item.get("hostname") or "",
                "status": item.get("status") or "unknown",
                "type": kind,
            }
            (cts if kind == "lxc" else vms).append(row)
        return {"vms": vms, "cts": cts}
    vms = _run_list(session, "qm list")
    cts = _run_list(session, "pct list", kind="lxc")
    if not vms and not cts:
        # cluster API fallback
        res = session.exec(
            "pvesh get /cluster/resources --type vm --output-format json 2>/dev/null",
            timeout=20,
        )
        text = (res["stdout"] or "").strip()
        if text.startswith("["):
            try:
                for item in json.loads(text):
                    vmid = str(item.get("vmid", ""))
                    if not vmid:
                        continue
                    kind = "lxc" if item.get("type") == "lxc" else "qemu"
                    row = {
                        "vmid": vmid,
                        "name": item.get("name") or item.get("hostname") or "",
                        "status": item.get("status") or "unknown",
                        "type": kind,
                    }
                    (cts if kind == "lxc" else vms).append(row)
            except Exception:
                pass
    return {"vms": vms, "cts": cts}


def _run_list(session: Session, cmd: str, kind: str = "qemu") -> list[dict[str, Any]]:
    res = session.exec(cmd, timeout=12)
    rows: list[dict[str, Any]] = []
    lines = [ln for ln in res["stdout"].splitlines() if ln.strip()]
    status_words = {"running", "stopped", "paused", "suspended"}
    for line in lines[1:]:
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        vmid = parts[0]
        status = "unknown"
        for token in parts[1:]:
            if token in status_words:
                status = token
                break
        # qm: VMID NAME STATUS ...
        # pct: VMID Status Lock Name
        name = ""
        if kind == "qemu":
            if len(parts) > 1 and parts[1] not in status_words and parts[1] != "-":
                name = parts[1]
        else:
            rest = parts[1:]
            if rest and rest[0] in status_words:
                rest = rest[1:]
            if rest and rest[0] == "-":
                rest = rest[1:]
            if rest:
                name = rest[0]
        rows.append({"vmid": vmid, "name": name, "status": status, "type": kind})
    return rows


def guest_status(session: Session, vmid: str, gtype: str) -> dict[str, Any]:
    if gtype == "lxc":
        res = session.exec(f"pct status {vmid}", timeout=15)
    else:
        res = session.exec(f"qm status {vmid}", timeout=15)
    status = "unknown"
    m = re.search(r"status:\s*(\w+)", res["stdout"])
    if m:
        status = m.group(1)
    return {"vmid": vmid, "type": gtype, "status": status, "output": res["stdout"]}


def guest_action(session: Session, vmid: str, gtype: str, action: str) -> dict[str, Any]:
    allowed = {
        "start": "start",
        "stop": "stop",
        "shutdown": "shutdown",
        "reboot": "reboot",
        "reset": "reset",
        "suspend": "suspend",
        "resume": "resume",
    }
    if action not in allowed:
        raise SSHError(f"不支持的操作: {action}")
    tool = "pct" if gtype == "lxc" else "qm"
    # LXC has no reset; map to stop+start is too destructive, reject clearly
    if gtype == "lxc" and action == "reset":
        raise SSHError("容器不支持 reset，请用强制停止后再启动")
    cmd = f"{tool} {action} {vmid}"
    res = (
        _swift_pve(session, "pveAction", vmid=vmid, guestKind=gtype, action=action, timeout=70)
        if native_bridge.enabled()
        else session.exec(cmd, timeout=60)
    )
    if not native_bridge.enabled() and res["code"] != 0 and gtype == "lxc" and action == "reboot":
        # older pct: stop + start
        res2 = session.exec(f"pct shutdown {vmid}; sleep 1; pct start {vmid}", timeout=60)
        return {
            "ok": res2["code"] == 0,
            "code": res2["code"],
            "stdout": res2["stdout"],
            "stderr": res2["stderr"] or res["stderr"],
        }
    return {
        "ok": res["code"] == 0,
        "code": res["code"],
        "stdout": res["stdout"],
        "stderr": res["stderr"],
    }


def create_vm(
    session: Session,
    *,
    vmid: str,
    name: str,
    cores: int = 2,
    memory: int = 2048,
    disk_gb: int = 32,
    storage: str = "local-lvm",
    iso: str = "",
    bridge: str = "vmbr0",
    net_model: str = "virtio",
) -> dict[str, Any]:
    if native_bridge.enabled():
        return _swift_pve(
            session, "pveCreateVM", timeout=130, vmid=vmid, name=name,
            cores=int(cores), memory=int(memory), diskGB=int(disk_gb),
            storage=storage, iso=iso, bridge=bridge, netModel=net_model,
        )
    if not vmid.isdigit():
        raise SSHError("VMID 必须是数字")
    if not name:
        raise SSHError("请填写虚拟机名称")
    cmds = [
        f"qm create {vmid} --name {sh_quote(name)} --memory {int(memory)} --cores {int(cores)} --net0 {net_model},bridge={bridge}",
    ]
    if iso:
        cmds.append(f"qm set {vmid} --ide2 local:iso/{sh_quote(iso)} --boot order=ide2")
        cmds.append(f"qm set {vmid} --ide0 {storage}:{int(disk_gb)}")
    else:
        cmds.append(f"qm set {vmid} --scsihw virtio-scsi-pci --scsi0 {storage}:{int(disk_gb)}")
    logs = []
    for cmd in cmds:
        res = session.exec(cmd, timeout=60)
        logs.append({"cmd": cmd, "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]})
        if res["code"] != 0:
            return {"ok": False, "logs": logs, "error": res["stderr"] or res["stdout"]}
    return {"ok": True, "logs": logs, "vmid": vmid}


def create_ct(
    session: Session,
    *,
    vmid: str,
    hostname: str,
    ostemplate: str,
    memory: int = 512,
    cores: int = 1,
    disk_gb: int = 8,
    storage: str = "local-lvm",
    password: str = "",
    unprivileged: bool = True,
    start: bool = False,
) -> dict[str, Any]:
    if native_bridge.enabled():
        return _swift_pve(
            session, "pveCreateCT", timeout=145, vmid=vmid, name=hostname,
            template=ostemplate, memory=int(memory), cores=int(cores),
            diskGB=int(disk_gb), storage=storage, containerPassword=password,
            unprivileged=unprivileged, startAfterCreate=start,
        )
    if not vmid.isdigit():
        raise SSHError("VMID 必须是数字")
    if not hostname:
        raise SSHError("请填写 CT 主机名")
    if not ostemplate:
        raise SSHError("请选择系统模板 (vztmpl)")
    flags = "--unprivileged 1" if unprivileged else "--unprivileged 0"
    pw = f"--password {sh_quote(password)}" if password else ""
    cmd = (
        f"pct create {vmid} {sh_quote(ostemplate)} "
        f"--hostname {sh_quote(hostname)} --memory {int(memory)} --cores {int(cores)} "
        f"--rootfs {storage}:{int(disk_gb)} --net0 name=eth0,bridge=vmbr0,ip=dhcp {flags} {pw}"
    )
    res = session.exec(cmd, timeout=120)
    out = {"ok": res["code"] == 0, "stdout": res["stdout"], "stderr": res["stderr"], "cmd": cmd}
    if res["code"] == 0 and start:
        st = guest_action(session, vmid, "lxc", "start")
        out["start"] = st
    return out


def helper_bundle(session: Session) -> dict[str, Any]:
    """One SSH round-trip for create-vm / create-ct form options."""
    res = _swift_pve(session, "pveHelpers", timeout=22) if native_bridge.enabled() else session.exec(
        "echo '===NEXTID==='; (pvesh get /cluster/nextid 2>/dev/null || echo 100); "
        "echo '===STORAGES==='; (pvesm status --output-format json 2>/dev/null || pvesm status 2>/dev/null); "
        "echo '===ISOS==='; (ls /var/lib/vz/template/iso 2>/dev/null || true); "
        "echo '===TEMPLATES==='; (ls /var/lib/vz/template/cache 2>/dev/null || true); "
        "echo '===END==='",
        timeout=12,
    )
    text = res["stdout"] or ""

    def sec(name: str) -> str:
        parts = text.split("===")
        for i, token in enumerate(parts):
            if token == name and i + 1 < len(parts):
                return parts[i + 1].strip()
        return ""

    nextid = sec("NEXTID").splitlines()[0].strip() if sec("NEXTID") else "100"
    storages: list[str] = []
    st_text = sec("STORAGES")
    if st_text.strip().startswith("["):
        try:
            for item in json.loads(st_text):
                if isinstance(item, dict) and item.get("storage"):
                    storages.append(str(item["storage"]))
        except Exception:
            pass
    if not storages:
        for line in st_text.splitlines()[1:]:
            parts = line.split()
            if parts:
                storages.append(parts[0])

    isos = []
    for line in sec("ISOS").splitlines():
        name = line.strip()
        if name.endswith((".iso", ".img", ".qcow2")):
            isos.append(name.split("/")[-1])

    templates = [
        f"local:vztmpl/{ln.strip()}"
        for ln in sec("TEMPLATES").splitlines()
        if ln.strip().endswith((".tar.gz", ".tar.xz", ".tar.zst"))
    ]
    if not storages:
        storages = ["local", "local-lvm"]
    return {
        "nextid": nextid or "100",
        "storages": storages,
        "isos": isos,
        "templates": templates,
    }


def list_storage(session: Session) -> list[str]:
    res = session.exec("pvesm status --output-format json 2>/dev/null || pvesm status", timeout=12)
    text = res["stdout"].strip()
    names: list[str] = []
    if text.startswith("["):
        try:
            for item in json.loads(text):
                if isinstance(item, dict) and item.get("storage"):
                    names.append(str(item["storage"]))
        except Exception:
            pass
    if not names:
        for line in text.splitlines()[1:]:
            parts = line.split()
            if parts:
                names.append(parts[0])
    return names


def list_isos(session: Session, storage: str = "local") -> list[str]:
    res = session.exec(
        f"ls /var/lib/vz/template/iso 2>/dev/null; "
        f"pvesm path {storage}:iso 2>/dev/null | head -1 | xargs -I{{}} ls {{}} 2>/dev/null",
        timeout=15,
    )
    files = []
    for line in res["stdout"].splitlines():
        line = line.strip()
        if line.endswith((".iso", ".img", ".qcow2")):
            files.append(line.split("/")[-1])
    # unique preserve order
    seen = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def list_templates(session: Session) -> list[str]:
    res = session.exec("ls /var/lib/vz/template/cache 2>/dev/null || true", timeout=15)
    return [ln.strip() for ln in res["stdout"].splitlines() if ln.strip().endswith((".tar.gz", ".tar.xz", ".tar.zst"))]


def next_vmid(session: Session) -> str:
    res = session.exec("pvesh get /cluster/nextid 2>/dev/null || echo 100", timeout=10)
    return res["stdout"].strip() or "100"


def node_summary(session: Session) -> dict[str, Any]:
    res = session.exec(
        "hostname; pveversion 2>/dev/null | head -1; "
        "pvesh get /nodes/$(hostname)/status --output-format json 2>/dev/null || echo {}",
        timeout=15,
    )
    lines = res["stdout"].splitlines()
    host = lines[0] if lines else ""
    ver = lines[1] if len(lines) > 1 else ""
    status: dict[str, Any] = {}
    for ln in lines[2:]:
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                status = json.loads(ln)
            except Exception:
                pass
    return {"hostname": host, "pveversion": ver, "status": status}


def sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"
