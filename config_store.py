"""Connection profile storage. Secrets stay on this machine only."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .paths import app_data_dir, migrate_legacy_data

migrate_legacy_data()

DEFAULT_PUBLIC_URLS: dict[str, str] = {}


def _with_public_default(vmid: str, entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    if not out.get("public_url") and str(vmid) in DEFAULT_PUBLIC_URLS:
        out["public_url"] = DEFAULT_PUBLIC_URLS[str(vmid)]
    return out


def _data_dir() -> Path:
    return app_data_dir()


def _profiles_path() -> Path:
    return _data_dir() / "profiles.json"


def _key_path() -> Path:
    return _data_dir() / ".key"


def _fernet() -> Fernet:
    kp = _key_path()
    if not kp.exists():
        kp.write_bytes(Fernet.generate_key())
    return Fernet(kp.read_bytes())


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")


def _load() -> list[dict[str, Any]]:
    p = _profiles_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list[dict[str, Any]]) -> None:
    _profiles_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_profiles() -> list[dict[str, Any]]:
    secret_keys = ("password", "private_key", "key_passphrase")
    out = []
    for item in _load():
        pub = {k: v for k, v in item.items() if k not in secret_keys}
        pub["has_password"] = bool(item.get("password"))
        pub["has_key"] = bool(item.get("private_key"))
        pub["has_passphrase"] = bool(item.get("key_passphrase"))
        out.append(pub)
    return out


def get_profile(profile_id: str) -> dict[str, Any] | None:
    for item in _load():
        if item.get("id") == profile_id:
            return item
    return None


def save_profile(payload: dict[str, Any]) -> dict[str, Any]:
    items = _load()
    pid = payload.get("id") or str(uuid.uuid4())
    password = payload.get("password") or ""
    private_key = payload.get("private_key") or ""
    key_passphrase = payload.get("key_passphrase") or ""
    record: dict[str, Any] = {
        "id": pid,
        "name": payload.get("name") or payload.get("host") or "PVE",
        "host": payload.get("host") or "",
        "domain": payload.get("domain") or "",
        "port": int(payload.get("port") or 22),
        "username": payload.get("username") or "root",
        "auth_method": payload.get("auth_method") or "password",
        "password": _encrypt(password) if password else "",
        "private_key": _encrypt(private_key) if private_key else "",
        "key_passphrase": _encrypt(key_passphrase) if key_passphrase else "",
        "note": payload.get("note") or "",
        "accept_unknown_host": bool(payload.get("accept_unknown_host", True)),
    }
    for i, item in enumerate(items):
        if item.get("id") == pid:
            items[i] = record
            break
    else:
        items.append(record)
    _save(items)
    return {k: v for k, v in record.items() if k not in ("password", "private_key", "key_passphrase")}


def delete_profile(profile_id: str) -> bool:
    items = _load()
    nxt = [x for x in items if x.get("id") != profile_id]
    if len(nxt) == len(items):
        return False
    _save(nxt)
    return True


def _maybe_decrypt(value: str) -> str:
    """Decrypt stored secrets; accept plaintext from in-memory form payloads."""
    if not value:
        return value
    try:
        return _decrypt(value)
    except Exception:
        return value


def resolve_secrets(profile: dict[str, Any]) -> dict[str, Any]:
    out = dict(profile)
    if profile.get("password"):
        out["password"] = _maybe_decrypt(profile["password"])
    if profile.get("private_key"):
        out["private_key"] = _maybe_decrypt(profile["private_key"])
    if profile.get("key_passphrase"):
        out["key_passphrase"] = _maybe_decrypt(profile["key_passphrase"])
    return out


def _services_path() -> Path:
    return _data_dir() / "services.json"


def load_services() -> dict[str, Any]:
    p = _services_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_services(data: dict[str, Any]) -> None:
    _services_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_service_entry(profile_id: str, vmid: str) -> dict[str, Any]:
    data = load_services()
    key = f"{profile_id}:{vmid}"
    if data.get(key):
        return _with_public_default(vmid, data[key])
    # profile recreated → old service keys still hold the URL
    suffix = f":{vmid}"
    best: dict[str, Any] = {}
    for k, v in data.items():
        if k.endswith(suffix) and isinstance(v, dict) and (v.get("web_url") or v.get("label")):
            if not best.get("web_url") and v.get("web_url"):
                best = v
            elif not best:
                best = v
    return _with_public_default(vmid, best)


def upsert_service_entry(profile_id: str, vmid: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = load_services()
    key = f"{profile_id}:{vmid}"
    cur = data.get(key) or {}
    # inherit from any previous profile binding for same vmid
    if not cur:
        cur = dict(get_service_entry(profile_id, vmid) or {})
    for field in ("label", "web_url", "kind", "note", "public_url"):
        if field in payload:
            cur[field] = payload[field] or ""
    data[key] = cur
    save_services(data)
    return cur


def list_service_entries(profile_id: str) -> dict[str, dict[str, Any]]:
    data = load_services()
    prefix = f"{profile_id}:"
    out: dict[str, dict[str, Any]] = {}
    # first pass: exact profile
    for key, val in data.items():
        if key.startswith(prefix) and isinstance(val, dict):
            vmid = key.split(":", 1)[1]
            out[vmid] = _with_public_default(vmid, val)
    # second pass: fill missing vmids from other profile bindings
    for key, val in data.items():
        if ":" not in key or not isinstance(val, dict):
            continue
        _, vmid = key.rsplit(":", 1)
        if vmid not in out and (val.get("web_url") or val.get("label")):
            out[vmid] = _with_public_default(vmid, val)
    for vmid, public_url in DEFAULT_PUBLIC_URLS.items():
        if vmid in out and not out[vmid].get("public_url"):
            out[vmid]["public_url"] = public_url
    return out
