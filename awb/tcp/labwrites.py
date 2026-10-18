"""The lab_writes line of keys.conf for the recipes: `python -m awb.tcp.labwrites` (run by `awb keys lab-writes
--from-recipes`, which starts it as a child so that the key service imports neither lab nor recheckwatch)."""
from __future__ import annotations

from awb.tcp import lab, recheckwatch
from awb.tcp.keys import WRITE_METHODS


def pairs() -> list[tuple[str, str]]:
    """The write calls of every recipe of recipes/ and of the recheck watch as service:path-prefix pairs, each
    once, sorted; a path is cut before its first ${...}."""
    found: set[tuple[str, str]] = set()

    def add(call: dict) -> None:
        if str(call.get("method")).upper() in WRITE_METHODS and isinstance(call.get("service"), str) and \
                isinstance(call.get("path"), str):
            found.add((call["service"], call["path"].partition("${")[0]))

    def walk(obj) -> None:
        if isinstance(obj, dict):
            add(obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for f in sorted(lab.recipes_dir().glob("*.json")):
        walk(lab.load(f))
    for call in recheckwatch.DELETES.values():
        add(call)
    return sorted(found)


def main() -> int:
    print("lab_writes = " + ",".join("%s:%s" % pair for pair in pairs()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
