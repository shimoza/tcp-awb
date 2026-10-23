"""The lab_writes line of keys.conf for the recipes: `python -m awb.tcp.labwrites` (run by `awb keys lab-writes
--from-recipes`, which starts it as a child so that the key service imports neither lab nor recheckwatch)."""
from __future__ import annotations

import re

from awb.tcp import lab, recheckwatch
from awb.tcp.keys import WRITE_METHODS

_PLACEHOLDER_RE = re.compile(r"^\$\{([a-z_][a-z0-9_]{0,40})\}$")


class LabWritesError(Exception):
    """A write call whose path the triple form cannot say: a ${...} that is not a whole segment."""


def template(path: str, query=None) -> str:
    """The path of a write call as a lab_writes path: each whole ${name} segment becomes {name} (one segment,
    K9), the query keys follow after ? in sorted order, joined by &."""
    segments = []
    for seg in path.split("/"):
        m = _PLACEHOLDER_RE.match(seg)
        if m:
            segments.append("{%s}" % m.group(1))
        elif "${" in seg:
            raise LabWritesError("a ${...} that is not a whole segment: %s" % path)
        else:
            segments.append(seg)
    out = "/".join(segments)
    keys = sorted(query) if isinstance(query, dict) else []
    return out + ("?" + "&".join(keys) if keys else "")


def triples() -> list[tuple[str, str, str]]:
    """The write calls of every recipe of recipes/ and of the recheck watch as METHOD service path triples, each
    shape once, sorted."""
    found: set[tuple[str, str, str]] = set()

    def add(call: dict) -> None:
        method = str(call.get("method")).upper()
        if method in WRITE_METHODS and isinstance(call.get("service"), str) and isinstance(call.get("path"), str):
            found.add((method, call["service"], template(call["path"], call.get("query"))))

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
    # one triple per shape: /servers/{probe}/action and /servers/{server}/action are the same write
    out: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for t in sorted(found):
        out.setdefault(shape(t), t)
    return sorted(out.values())


def shape(triple: tuple[str, str, str]) -> tuple[str, str, str]:
    """The triple with every {name} written {}: two triples of one shape match the same writes."""
    method, service, path = triple
    return method, service, re.sub(r"\{[a-z_][a-z0-9_]*\}", "{}", path)


def main() -> int:
    print("lab_writes = " + ", ".join(" ".join(t) for t in triples()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
