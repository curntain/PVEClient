"""Remote system metrics collected over SSH."""
from __future__ import annotations

import re
import threading
import time
import weakref
from typing import Any

from .ssh_manager import SSHError, Session


_metrics_cache: "weakref.WeakKeyDictionary[Session, tuple[float, dict[str, Any]]]" = weakref.WeakKeyDictionary()
_metrics_cache_lock = threading.RLock()

MONITOR_SCRIPT = r"""
set +e
echo "===HOST==="
hostname 2>/dev/null; echo
echo "===UPTIME==="
uptime 2>/dev/null; echo
echo "===LOAD==="
cat /proc/loadavg 2>/dev/null; echo
echo "===CPU==="
nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 1
# sample /proc/stat
read -r _ u1 n1 s1 i1 w1 x1 y1 z1 a1 f1 < /proc/stat
sleep 0.5
read -r _ u2 n2 s2 i2 w2 x2 y2 z2 a2 f2 < /proc/stat
busy=$(( (u2+n2+s2) - (u1+n1+s1) ))
idle=$(( (i2+w2) - (i1+w1) ))
total=$((busy+idle))
if [ "$total" -le 0 ]; then total=1; fi
echo "$busy $total"
grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ //'
echo
echo "===MEM==="
awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{if(t=="")t=0; if(a=="")a=0; used=t-a; print t*1024, used*1024, a*1024}' /proc/meminfo
echo
echo "===DISK==="
df -kP -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | awk 'NR>1 && $6!=""{printf "%.0f %.0f %.0f %s %s\n", $2*1024, $3*1024, $4*1024, $5, $6}'
echo
echo "===NET==="
awk 'NR>2{rx+=$2; tx+=$10} END{printf "%.0f %.0f\n", rx+0, tx+0}' /proc/net/dev
sleep 0.5
awk 'NR>2{rx+=$2; tx+=$10} END{printf "%.0f %.0f\n", rx+0, tx+0}' /proc/net/dev
echo
echo "===VM==="
(qm list 2>/dev/null || true)
echo
echo "===CT==="
(pct list 2>/dev/null || true)
echo
echo "===DONE==="
"""


def collect_metrics(session: Session) -> dict[str, Any]:
    try:
        result = session.exec_monitor(MONITOR_SCRIPT, timeout=12)
    except SSHError:
        with _metrics_cache_lock:
            cached = _metrics_cache.get(session)
        if cached:
            stale = dict(cached[1])
            stale["stale"] = True
            stale["error"] = "监控短暂中断，已显示上一次数据"
            stale["cache_age_seconds"] = round(time.monotonic() - cached[0], 1)
            return stale
        raise
    text = result["stdout"] or ""
    if "===HOST===" not in text and result.get("stderr"):
        # last resort tiny probe
        probe = session.exec_monitor("hostname; cat /proc/loadavg; free -m", timeout=8)
        text = "===HOST===\n" + (probe["stdout"] or "")

    data: dict[str, Any] = {
        "raw_ok": "===HOST===" in text,
        "host": _section_line(text, "HOST") or "",
        "uptime": " ".join(_section(text, "UPTIME").split()) or "",
        "load": _section_line(text, "LOAD") or "",
        "cpu_model": "",
        "cpu_cores": 0,
        "cpu_percent": 0.0,
        "mem_total": 0,
        "mem_used": 0,
        "mem_available": 0,
        "mem_percent": 0.0,
        "disks": [],
        "net_rx_bps": 0.0,
        "net_tx_bps": 0.0,
        "vms": [],
        "cts": [],
        "error": (result.get("stderr") or "").strip(),
    }

    cpu_block = [ln for ln in _section(text, "CPU").splitlines() if ln.strip() != ""]
    if cpu_block:
        try:
            data["cpu_cores"] = int(cpu_block[0].strip().split()[0])
        except Exception:
            pass
        if len(cpu_block) >= 2:
            try:
                busy, total = cpu_block[1].split()[:2]
                data["cpu_percent"] = round(int(busy) * 100.0 / max(int(total), 1), 1)
            except Exception:
                pass
        if len(cpu_block) >= 3:
            data["cpu_model"] = cpu_block[2].strip()

    mem_line = _section_line(text, "MEM")
    if mem_line:
        try:
            total, used, avail = mem_line.split()[:3]
            data["mem_total"] = int(float(total))
            data["mem_used"] = int(float(used))
            data["mem_available"] = int(float(avail))
            data["mem_percent"] = round(data["mem_used"] * 100.0 / max(data["mem_total"], 1), 1)
        except Exception:
            pass

    for line in _section(text, "DISK").splitlines():
        parts = line.split()
        if len(parts) >= 5:
            try:
                total, used, avail = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
                pct = parts[3].rstrip("%")
                data["disks"].append(
                    {
                        "total": total,
                        "used": used,
                        "available": avail,
                        "percent": float(pct) if pct.replace(".", "", 1).isdigit() else 0.0,
                        "mount": parts[4],
                    }
                )
            except Exception:
                continue

    net_lines = [ln for ln in _section(text, "NET").splitlines() if ln.strip()]
    if len(net_lines) >= 2:
        try:
            rx1, tx1 = map(float, net_lines[0].split()[:2])
            rx2, tx2 = map(float, net_lines[1].split()[:2])
            data["net_rx_bps"] = max(0.0, (rx2 - rx1) / 0.5)
            data["net_tx_bps"] = max(0.0, (tx2 - tx1) / 0.5)
        except Exception:
            pass

    data["vms"] = _parse_guests(_section(text, "VM"), "qemu")
    data["cts"] = _parse_guests(_section(text, "CT"), "lxc")
    data["stale"] = False
    with _metrics_cache_lock:
        _metrics_cache[session] = (time.monotonic(), dict(data))
    return data


def _section(text: str, name: str) -> str:
    parts = re.split(r"===", text)
    # parts like ['', 'HOST', '\nvalue\n', 'UPTIME', '\nvalue\n', ...]
    for i, token in enumerate(parts):
        if token == name and i + 1 < len(parts):
            return parts[i + 1].strip()
    return ""


def _section_line(text: str, name: str) -> str:
    block = _section(text, name).strip()
    return block.splitlines()[0] if block else ""


def _parse_guests(block: str, gtype: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in block.splitlines()[1:]:
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        status = "unknown"
        for token in parts:
            if token in ("running", "stopped", "paused"):
                status = token
        name = ""
        if len(parts) > 1 and parts[1] not in ("-", "stopped", "running"):
            name = parts[1]
        rows.append(
            {
                "type": gtype,
                "vmid": parts[0],
                "name": name,
                "status": status,
                "raw": line,
            }
        )
    return rows


def format_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if abs(v) < 1024 or u == units[-1]:
            return f"{v:.1f}{u}" if u != "B" else f"{int(v)}B"
        v /= 1024
    return f"{v:.1f}TB"
