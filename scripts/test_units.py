import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import monitoring
from app.config_store import resolve_secrets, save_profile, list_profiles, delete_profile, get_profile

# monitoring section parse
text = """
===HOST===
pve
===UPTIME===
up 1 day
===LOAD===
0.10 0.20 0.30 1/100 1
===CPU===
8
12 100
Intel Xeon
===MEM===
8589934592 4294967296 4294967296
===DISK===
100000 50000 50000 50% /
===NET===
100 200
300 600
===VM===
VMID NAME STATUS
101 a running
===CT===
VMID Status Lock Name
102 stopped - ct
===DONE===
"""
# monkey: use private helpers
assert monitoring._section_line(text, "HOST") == "pve"
assert monitoring._section_line(text, "MEM") == "8589934592 4294967296 4294967296"
assert monitoring._parse_guests(monitoring._section(text, "VM"), "qemu")[0]["vmid"] == "101"
assert monitoring._parse_guests(monitoring._section(text, "CT"), "lxc")[0]["status"] == "stopped"

# secrets: plaintext passthrough
out = resolve_secrets({"password": "hello", "private_key": "", "key_passphrase": ""})
assert out["password"] == "hello"

print("unit checks ok")
