"""The vendor download hosts of `awb lab obs fetch` (task VF): rules/vendor-hosts.txt, read by the client (`awb lab obs
hosts`) and by the key service (awb/tcp/labobs.py). A plain line is a download host, a line `portal HOST` a host
behind a vendor login. Exact host names, no wildcard."""
from __future__ import annotations

from pathlib import Path

VENDOR_FILE = Path(__file__).resolve().parent.parent / "rules" / "vendor-hosts.txt"


def read(path: Path | None = None) -> tuple[list[str], list[str]]:
    """(download hosts, portal hosts), read at each call so a change needs no restart."""
    try:
        text = Path(path or VENDOR_FILE).read_text(encoding="utf-8")
    except OSError:
        return [], []
    hosts: list[str] = []
    portals: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if not line:
            continue
        if line.startswith("portal "):
            portals.append(line.split(None, 1)[1].strip())
        else:
            hosts.append(line)
    return hosts, portals
