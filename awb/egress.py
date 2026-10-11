"""The egress filter of the work user (seal/egress/, D-M16 of presentations/modes/DESIGN.md on the sealed host).

    awb egress status [--json]                    any user: the mode, the load and the counts of the last 24 hours
    sudo awb egress load                          root: resolve the hosts of ALLOW.md, load the table or refresh its
                                                  address sets, record the counters (setup.sh, awb-egress.service and
                                                  its timer every 5 minutes)
    sudo awb egress enforce [--accept CLASS]...   the owner with sudo: refused unless 24 hours of counts allow it
    sudo awb egress count                         the owner with sudo: back to counting

The table `inet awb_egress` (seal/egress/work-egress.nft) sends every packet of the work user's uid (work_user of
/etc/awb/paths.conf) through a chain of its own; every other uid passes the base chain untouched: the key service,
the vault daemon and the owner run as the owner, the Ask page and the project chat as awb-ask. In that chain
loopback (the resolver, the local services) and established connections pass, a new connection on tcp 443 to an
address of the allow list (the host names of seal/egress/ALLOW.md, resolved by root) passes, and every other new
connection is counted in the named counter of its class (CLASSES) and then accepted in the mode count or refused in
the mode enforce.

The mode is the line `egress = count|enforce` of /etc/awb/paths.conf, read from that root file alone and never from
AWB_CONF; without the line it is count. Another word or a second line is no mode: load refuses and the loaded table
stays as it is.

The state lives in /var/lib/awb-egress (root, readable for every user; classes, counts and times, never the address
of a connection): addresses.json (every address a listed host resolved to and when it was last seen, kept 7 days, so
a host that answers with a changing set keeps the addresses it gave of late), history.tsv (one line per load: the
time, the mode and what each class counted since the line before) and status.json (what `awb egress status`, `awb
seal check` and `awb deploy status` show). The counters of nftables count from the load of the table; a load that
replaces the table records them first, so a deploy loses no count. A reboot loses what came after the last load (at
most 5 minutes) and shows as a gap in the history.

`enforce` is refused unless the history covers the last 24 hours in the mode count without a gap of more than 30
minutes and no class counted a connection in that time, but probe (the planted connection of verify.sh to
192.0.2.0/24 and 2001:db8::/32) and the classes the owner names with --accept, because he expects to lose them.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from awb import config

TABLE = "awb_egress"
MODES = ("count", "enforce")
DEFAULT_MODE = "count"
CLASSES = ("probe", "metadata", "dns", "ntp", "tcp_api", "github", "ssh", "mail", "web", "other_tcp", "other_udp",
           "icmp", "other")
"""The classes of a connection outside the allow list, in the order of the rules of the chain work."""
EXPECTED = frozenset({"probe"})
"""Classes whose counts never stop `enforce`: the planted connection of verify.sh."""
COUNTER_PREFIX = "c_"
"""A named counter of the table is the class with this prefix (c_web): a bare icmp is a keyword of nft."""
ALLOWED = "allowed"
"""The counter of the new connections to the allow list: shown, never a class."""
WATCH = ("tcp_api", "github")
"""Classes ALLOW.md fills by address: counted, never allowed."""
SETS = ("allow",) + WATCH
STATE_DIR = Path("/var/lib/awb-egress")
SOURCE = Path(__file__).resolve().parents[1] / "seal" / "egress"
TEMPLATE = "work-egress.nft"
ALLOW_FILE = "ALLOW.md"
NFT = "/usr/sbin/nft"
WINDOW = 24 * 3600
MAX_GAP = 30 * 60
STALE = 15 * 60
KEEP_ADDRESSES = 7 * 86400
KEEP_HISTORY = 8 * 86400
RESOLVE_TIMEOUT = 10.0
VERDICTS = {
    "count": "        accept",
    "enforce": "        meta l4proto tcp reject with tcp reset\n        reject with icmpx admin-prohibited",
}

_EGRESS_RE = re.compile(r"^[ \t]*egress[ \t]*=[ \t]*(.*?)[ \t]*$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_ROW_RE = re.compile(r"^\|\s*`([^`\s]+)`\s*\|\s*`(tcp 443|any)`\s*\|\s*`([a-z_]+)`\s*\|")
_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


class EgressError(Exception):
    """Exit 2: nothing was changed by the step that raised it. The message carries no value of the caller."""


class Refused(Exception):
    """Exit 1: `enforce` refused on the counts; the mode stays count."""


@dataclass
class Host:
    """What the commands touch, replaced by the tests."""
    conf: Path | None = None            # None: config.HOST_CONF at call time
    state: Path | None = None           # None: STATE_DIR at call time
    source: Path | None = None          # None: SOURCE at call time
    run: Callable = subprocess.run
    now: Callable = time.time
    resolve: Callable | None = None     # None: _resolve
    geteuid: Callable = os.geteuid
    environ: dict = field(default_factory=lambda: dict(os.environ))
    chown_root: bool = True             # a root write gives the file to root:root (the tests run as a user)

    @property
    def conf_path(self) -> Path:
        return Path(self.conf if self.conf is not None else config.HOST_CONF)

    @property
    def state_dir(self) -> Path:
        return Path(self.state if self.state is not None else STATE_DIR)

    @property
    def source_dir(self) -> Path:
        return Path(self.source if self.source is not None else SOURCE)


# --------------------------------------------------------------------------- the mode


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def parse_mode(text: str | None) -> tuple[str | None, str]:
    """(mode, note) of the text of a host file: count without a file or without the line; (None, why) for another
    word or a second line."""
    if text is None:
        return DEFAULT_MODE, "no host file"
    found = [m.group(1) for m in map(_EGRESS_RE.match, text.splitlines()) if m]
    if not found:
        return DEFAULT_MODE, "no egress line, the default"
    if len(found) > 1:
        return None, "two egress lines"
    if found[0] not in MODES:
        return None, "an egress line that is neither count nor enforce"
    return found[0], ""


def mode(host: Host | None = None) -> str | None:
    """The mode the root host file names (never AWB_CONF): count by default, None when the line is no mode."""
    return parse_mode(_read((host or Host()).conf_path))[0]


def has_line(host: Host | None = None) -> bool:
    """True when the root host file carries an egress line: the seal of this host installed the filter."""
    text = _read((host or Host()).conf_path) or ""
    return any(_EGRESS_RE.match(line) for line in text.splitlines())


def conf_value(text: str, key: str) -> str | None:
    m = re.search(r"(?m)^[ \t]*%s[ \t]*=[ \t]*(\S+)[ \t]*$" % re.escape(key), text)
    return m.group(1) if m else None


def _atomic_write(path: Path, text: str, host: Host, mode_bits: int = 0o644, gid: int = 0) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), mode_bits)
            if host.chown_root and host.geteuid() == 0:
                os.fchown(fh.fileno(), 0, gid)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_mode(host: Host, new: str) -> None:
    """Put `egress = NEW` into the root host file in place of any egress line, atomically; every other line stays."""
    if new not in MODES:
        raise EgressError("the mode is count or enforce")
    path = host.conf_path
    text = _read(path)
    if text is None:
        raise EgressError("the host file cannot be read: run seal/setup.sh first")
    lines = [line for line in text.splitlines() if not _EGRESS_RE.match(line)]
    lines.append("egress = %s" % new)
    # the host file keeps its mode, never a group or other write bit, and its group (640 root:awb since SB1 of the
    # self-build, closed to other users)
    st = path.stat()
    _atomic_write(path, "\n".join(lines) + "\n", host, st.st_mode & 0o755, st.st_gid)


# --------------------------------------------------------------------------- the allow list


@dataclass(frozen=True)
class Entry:
    dest: str       # a host name or a network
    port: str       # "tcp 443" for the allow set, "any" for a watched class
    set: str        # allow, tcp_api or github


def _network(text: str):
    try:
        return ipaddress.ip_network(text, strict=True)
    except ValueError:
        return None


def read_allow(text: str) -> list[Entry]:
    """The machine rows of ALLOW.md: a table row whose first three cells are in backticks (destination, `tcp 443`
    or `any`, set). Every other line is prose. A row that names an unknown set, an allowed row on another port or a
    destination that is neither a host name nor a network stops the load."""
    out: list[Entry] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        dest, port, kind = m.groups()
        if kind not in SETS:
            raise EgressError("ALLOW.md: a row names a set other than %s" % ", ".join(SETS))
        if (kind == "allow") != (port == "tcp 443"):
            raise EgressError("ALLOW.md: an allowed row is tcp 443, a watched row is any")
        if not (_HOST_RE.match(dest) or _network(dest)):
            raise EgressError("ALLOW.md: a row names neither a host name nor a network")
        out.append(Entry(dest, port, kind))
    if not any(e.set == "allow" for e in out):
        raise EgressError("ALLOW.md lists no allowed host")
    return out


def _resolve(name: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos if i[0] in (socket.AF_INET, socket.AF_INET6) and "%" not in i[4][0]})


def resolve_all(names: list[str], resolve: Callable, timeout: float = RESOLVE_TIMEOUT) -> dict[str, list[str] | None]:
    """{name: addresses} for every name, None for a name without an answer within `timeout` (it keeps its cache)."""
    pool = ThreadPoolExecutor(max_workers=8)
    futures = {n: pool.submit(resolve, n) for n in names}
    done, _ = wait_for(list(futures.values()), timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)
    out: dict[str, list[str] | None] = {}
    for name, fut in futures.items():
        if fut in done and fut.exception() is None:
            addrs = [a for a in (fut.result() or []) if _network(a) is not None]
            out[name] = addrs
        else:
            out[name] = None
    return out


def update_cache(cache: dict, entries: list[Entry], resolve: Callable, now: float) -> tuple[dict, int]:
    """The address cache {host: {address: last seen}} after one round of resolving: a host keeps the addresses it
    gave within KEEP_ADDRESSES, a host no longer listed goes. Returns the cache and the number of hosts without an
    answer this round."""
    hosts = sorted({e.dest for e in entries if _network(e.dest) is None})
    answers = resolve_all(hosts, resolve)
    out: dict[str, dict[str, float]] = {}
    missing = 0
    for h in hosts:
        seen = {a: t for a, t in (cache.get(h) or {}).items()
                if isinstance(t, (int, float)) and now - t <= KEEP_ADDRESSES and _network(a) is not None}
        got = answers.get(h)
        if not got:
            missing += 1
        for a in got or []:
            seen[a] = now
        out[h] = seen
    return out, missing


def _plain(net) -> str:
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


def set_elements(entries: list[Entry], cache: dict) -> dict[str, list[str]]:
    """{set name: elements} for the six sets (allow4, allow6, tcp_api4, ...): addresses and networks, collapsed so
    that no two elements of an interval set overlap."""
    nets: dict[str, list] = {"%s%s" % (s, v): [] for s in SETS for v in "46"}
    for e in entries:
        found = [_network(e.dest)] if _network(e.dest) else [_network(a) for a in (cache.get(e.dest) or {})]
        for n in found:
            if n is not None:
                nets["%s%d" % (e.set, n.version)].append(n)
    return {k: [_plain(n) for n in ipaddress.collapse_addresses(v)] if v else [] for k, v in nets.items()}


# --------------------------------------------------------------------------- the rules


def render_rules(template: str, work_user: str, mode_word: str) -> str:
    """The template with the work user and the verdict of the mode; refused when a placeholder stays."""
    if mode_word not in MODES:
        raise EgressError("the mode is count or enforce")
    if not _USER_RE.match(work_user or ""):
        raise EgressError("the work user is no user name")
    text = template.replace("@WORK_USER@", work_user).replace("@VERDICT@", VERDICTS[mode_word])
    if re.search(r"@[A-Z_]+@", text):
        raise EgressError("the template keeps a placeholder after rendering")
    return text


def element_lines(elements: dict[str, list[str]]) -> list[str]:
    return ["add element inet %s %s { %s }" % (TABLE, name, ", ".join(values))
            for name, values in sorted(elements.items()) if values]


def render(template: str, work_user: str, mode_word: str, elements: dict[str, list[str]]) -> str:
    """The whole file `nft -f` loads as one transaction: the table, then its addresses."""
    rules = render_rules(template, work_user, mode_word).rstrip("\n")
    return "\n".join([rules] + element_lines(elements)) + "\n"


def refresh_text(elements: dict[str, list[str]]) -> str:
    """One transaction that replaces the addresses of every set and leaves the rules and the counters alone."""
    return "\n".join(["flush set inet %s %s" % (TABLE, name) for name in sorted(elements)]
                     + element_lines(elements)) + "\n"


# --------------------------------------------------------------------------- nftables


def _nft(host: Host, args: list[str], text: str | None = None) -> subprocess.CompletedProcess:
    return host.run([NFT] + args, input=text, capture_output=True, text=True, timeout=60)


def table_state(host: Host) -> dict | None:
    """{"counters": {name: packets}, "verdict": "count" or "enforce"} of the loaded table, None when there is none."""
    r = _nft(host, ["-j", "list", "table", "inet", TABLE])
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    counters: dict[str, int] = {}
    verdict = None
    for item in data.get("nftables", []) if isinstance(data, dict) else []:
        c = item.get("counter") if isinstance(item, dict) else None
        if isinstance(c, dict) and c.get("table") == TABLE and isinstance(c.get("packets"), int) \
                and str(c.get("name")).startswith(COUNTER_PREFIX):
            counters[str(c["name"])[len(COUNTER_PREFIX):]] = c["packets"]
        rule = item.get("rule") if isinstance(item, dict) else None
        if isinstance(rule, dict) and rule.get("chain") == "refused":
            words = json.dumps(rule.get("expr"))
            verdict = "enforce" if '"reject"' in words else (verdict or "count")
    return {"counters": counters, "verdict": verdict}


def _first_line(text: str) -> str:
    line = next((x for x in (text or "").splitlines() if x.strip()), "")
    return re.sub(r"[^\x20-\x7e]", " ", line)[:200]


# --------------------------------------------------------------------------- the state


def _now_text(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime(_TIME_FMT)


def _parse_time(text) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, _TIME_FMT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _read_json(path: Path) -> dict:
    text = _read(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_history(host: Host) -> list[tuple[float, str, dict[str, int]]]:
    """(time, mode, counts) per line of history.tsv, oldest first; a line that does not parse is left out."""
    out = []
    for line in (_read(host.state_dir / "history.tsv") or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        t = _parse_time(parts[0])
        if t is None or parts[1] not in MODES:
            continue
        counts: dict[str, int] = {}
        if parts[2] != "-":
            for pair in parts[2].split(","):
                k, _, v = pair.partition("=")
                if (k in CLASSES or k == ALLOWED) and v.isdigit():
                    counts[k] = int(v)
        out.append((t, parts[1], counts))
    return sorted(out, key=lambda x: x[0])


def _write_history(host: Host, rows: list[tuple[float, str, dict[str, int]]]) -> None:
    lines = ["%s\t%s\t%s" % (_now_text(t), m, ",".join("%s=%d" % kv for kv in sorted(c.items())) or "-")
             for t, m, c in rows]
    _atomic_write(host.state_dir / "history.tsv", "\n".join(lines) + ("\n" if lines else ""), host)


def record(host: Host, st: dict, counters: dict[str, int], mode_word: str, now: float) -> dict[str, int]:
    """Append what each class counted since the last record; a counter below its last value counts from zero (the
    table was loaded again). Returns the counts of the line."""
    last = st.get("last") if isinstance(st.get("last"), dict) else {}
    delta: dict[str, int] = {}
    for name in CLASSES + (ALLOWED,):
        cur = counters.get(name, 0)
        prev = last.get(name, 0) if isinstance(last.get(name, 0), int) else 0
        d = cur - prev if cur >= prev else cur
        if d > 0:
            delta[name] = d
    st["last"] = {name: counters.get(name, 0) for name in CLASSES + (ALLOWED,)}
    rows = [r for r in read_history(host) if now - r[0] <= KEEP_HISTORY]
    rows.append((now, mode_word, delta))
    _write_history(host, rows)
    return delta


def window(rows: list[tuple[float, str, dict[str, int]]], now: float, count_since: float | None) -> dict:
    """The last 24 hours of the history: {"covered", "why", "counts", "allowed"}. Covered means: counting since 24
    hours at least, a line at or before the start of the window, no gap of more than MAX_GAP between two lines or
    between the last line and now, every line of the window in the mode count."""
    start = now - WINDOW
    inside = [r for r in rows if r[0] > start]
    counts: dict[str, int] = {}
    allowed = 0
    for _, _, c in inside:
        for k, v in c.items():
            if k == ALLOWED:
                allowed += v
            else:
                counts[k] = counts.get(k, 0) + v
    why = ""
    before = [r for r in rows if r[0] <= start]
    if count_since is None:
        why = "the filter does not count now"
    elif count_since > start:
        why = "counting since %s, less than 24 hours" % _now_text(count_since)
    elif not before:
        why = "the history begins inside the last 24 hours"
    else:
        times = [before[-1][0]] + [r[0] for r in inside] + [now]
        gap = max(b - a for a, b in zip(times, times[1:]))
        if gap > MAX_GAP:
            why = "a gap of %d minutes in the history (the timer did not run or the host was down)" % (gap // 60)
        elif any(m != "count" for _, m, _ in inside):
            why = "the window holds a time in the mode enforce"
    return {"covered": not why, "why": why, "counts": dict(sorted(counts.items())), "allowed": allowed}


def unexpected(counts: dict[str, int], accepted: set[str]) -> dict[str, int]:
    return {k: v for k, v in counts.items() if v and k not in EXPECTED and k not in accepted}


# --------------------------------------------------------------------------- the commands


def _require_root(host: Host, what: str) -> None:
    if host.geteuid() != 0:
        raise EgressError("run it as root: sudo awb egress %s" % what)


def _require_owner(host: Host, text: str, what: str) -> None:
    _require_root(host, what)
    owner = conf_value(text, "owner")
    if not owner or host.environ.get("SUDO_USER") != owner:
        raise EgressError("run it with sudo as the owner of the host file: sudo awb egress %s" % what)


def _work_user(text: str) -> str:
    user = conf_value(text, "work_user")
    owner = conf_value(text, "owner")
    if not user or not _USER_RE.match(user):
        raise EgressError("the host file names no work user")
    if user == owner or user == "root":
        raise EgressError("the work user must be neither the owner nor root")
    try:
        if pwd.getpwnam(user).pw_uid == 0:
            raise EgressError("the work user must not have uid 0")
    except KeyError:
        raise EgressError("the work user of the host file does not exist") from None
    return user


def load(host: Host, *, force_reload: bool = False) -> dict:
    """Resolve, then load the table when its rules changed (recording the counters of the old one first) or refresh
    its address sets in place, record the counters and write status.json. The mode comes from the host file."""
    _require_root(host, "load")
    text = _read(host.conf_path)
    if text is None:
        raise EgressError("the host file cannot be read: run seal/setup.sh first")
    mode_word, why = parse_mode(text)
    if mode_word is None:
        raise EgressError("the host file has %s: the loaded table stays as it is" % why)
    user = _work_user(text)
    template = _read(host.source_dir / TEMPLATE)
    allow_text = _read(host.source_dir / ALLOW_FILE)
    if template is None or allow_text is None:
        raise EgressError("seal/egress of the installed release cannot be read")
    entries = read_allow(allow_text)
    rules = render_rules(template, user, mode_word)
    digest = hashlib.sha256(rules.encode("utf-8")).hexdigest()
    host.state_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    st = _read_json(host.state_dir / "status.json")
    now = host.now()
    cache, missing = update_cache(_read_json(host.state_dir / "addresses.json"), entries,
                                  host.resolve or _resolve, now)
    _atomic_write(host.state_dir / "addresses.json", json.dumps(cache, indent=1, sort_keys=True) + "\n", host)
    elements = set_elements(entries, cache)
    current = table_state(host)
    if current is not None and not force_reload and st.get("rules_sha256") == digest \
            and current.get("verdict") == mode_word:
        r = _nft(host, ["-f", "-"], refresh_text(elements))
        if r.returncode != 0:
            raise EgressError("nft refused the address sets: %s" % _first_line(r.stderr))
        action = "refreshed"
    else:
        if current is not None:
            record(host, st, current["counters"], st.get("mode") if st.get("mode") in MODES else mode_word, now)
        path = host.state_dir / "work-egress.nft"
        _atomic_write(path, render(template, user, mode_word, elements), host)
        r = _nft(host, ["-f", str(path)])
        if r.returncode != 0:
            _save_status(host, st)
            raise EgressError("nft refused the rules, the table before stays: %s" % _first_line(r.stderr))
        st["last"] = {}
        st["loaded_at"] = _now_text(now)
        st["rules_sha256"] = digest
        if mode_word == "count" and (st.get("mode") != "count" or not st.get("count_since")):
            st["count_since"] = _now_text(now)
        if mode_word == "enforce":
            st["count_since"] = None
        st["mode"] = mode_word
        action = "loaded"
    after = table_state(host)
    if after is not None:
        record(host, st, after["counters"], mode_word, now)
    st["refreshed_at"] = _now_text(now)
    st["hosts"] = {"listed": len({e.dest for e in entries if _network(e.dest) is None}), "unanswered": missing}
    st["window"] = window(read_history(host), now, _parse_time(st.get("count_since")))
    _save_status(host, st)
    return {"action": action, "mode": mode_word, "unanswered": missing}


def _save_status(host: Host, st: dict) -> None:
    _atomic_write(host.state_dir / "status.json", json.dumps(st, indent=1, sort_keys=True) + "\n", host)


def enforce(host: Host, accepted: set[str]) -> str:
    """Switch to enforce when the last 24 hours allow it; refused (Refused) otherwise, the mode stays count."""
    bad = sorted(accepted - set(CLASSES))
    if bad:
        raise EgressError("--accept takes a class: %s" % ", ".join(CLASSES))
    text = _read(host.conf_path)
    if text is None:
        raise EgressError("the host file cannot be read: run seal/setup.sh first")
    _require_owner(host, text, "enforce")
    current, why = parse_mode(text)
    if current is None:
        raise EgressError("the host file has %s: fix it to egress = count first" % why)
    if current == "enforce":
        load(host)
        return "egress: the mode is enforce already; the table is loaded"
    load(host)
    st = _read_json(host.state_dir / "status.json")
    if table_state(host) is None:
        raise Refused("the egress table is not loaded: sudo awb egress load, then read the counts")
    w = window(read_history(host), host.now(), _parse_time(st.get("count_since")))
    if not w["covered"]:
        raise Refused("the counts do not cover the last 24 hours (%s): the mode stays count" % w["why"])
    left = unexpected(w["counts"], accepted)
    if left:
        raise Refused("the last 24 hours counted %s: allow what is needed in seal/egress/ALLOW.md and deploy, or "
                      "name a class you expect to lose with --accept; the mode stays count"
                      % ", ".join("%s %d" % kv for kv in sorted(left.items())))
    write_mode(host, "enforce")
    try:
        load(host)
    except EgressError:
        write_mode(host, "count")
        load(host, force_reload=True)
        raise
    taken = {k: v for k, v in w["counts"].items() if k in accepted}
    note = ("; accepted: %s" % ", ".join("%s %d" % kv for kv in sorted(taken.items()))) if taken else ""
    return "egress: enforce, every new connection of the work user outside the list is refused%s" % note


def count(host: Host) -> str:
    text = _read(host.conf_path)
    if text is None:
        raise EgressError("the host file cannot be read: run seal/setup.sh first")
    _require_owner(host, text, "count")
    write_mode(host, "count")
    load(host)
    return "egress: count, nothing is refused; the 24 hours start again now"


# --------------------------------------------------------------------------- what every user can read


def status(host: Host | None = None) -> dict:
    """The status file with the mode of the host file and its age: what status_lines and the seal check show."""
    host = host or Host()
    st = _read_json(host.state_dir / "status.json")
    host_mode, why = parse_mode(_read(host.conf_path))
    refreshed = _parse_time(st.get("refreshed_at"))
    return {"host_mode": host_mode, "host_why": why, "line": has_line(host), "loaded": bool(st.get("loaded_at")),
            "mode": st.get("mode") if st.get("mode") in MODES else None, "loaded_at": st.get("loaded_at"),
            "refreshed_at": st.get("refreshed_at"),
            "stale": refreshed is None or host.now() - refreshed > STALE,
            "window": st.get("window") if isinstance(st.get("window"), dict) else None,
            "hosts": st.get("hosts") if isinstance(st.get("hosts"), dict) else None}


def status_lines(host: Host | None = None) -> list[str]:
    s = status(host)
    if s["host_mode"] is None:
        return ["egress: the host file has %s: sudo awb egress count sets it right" % s["host_why"]]
    if not s["loaded"]:
        return ["egress: no filter loaded on this host (mode %s in the host file; seal/setup.sh loads it)"
                % s["host_mode"]]
    lines = ["egress: mode %s, loaded %s, refreshed %s%s" % (
        s["mode"], s["loaded_at"], s["refreshed_at"],
        " (older than 15 minutes: systemctl status awb-egress.timer)" if s["stale"] else "")]
    if s["mode"] != s["host_mode"]:
        lines.append("egress: the host file says %s, the table runs %s: sudo awb egress load" % (s["host_mode"],
                                                                                                s["mode"]))
    w = s["window"] or {}
    counts = w.get("counts") if isinstance(w.get("counts"), dict) else {}
    counted = ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items()) if isinstance(v, int) and v)
    lines.append("egress: last 24 hours%s: allowed %s; counted outside the list: %s" % (
        "" if w.get("covered") else " (%s)" % (w.get("why") or "no history yet"),
        w.get("allowed", 0), counted or "none"))
    return lines


def check(host: Host | None = None) -> tuple[bool, str] | None:
    """For `awb seal check`: None when the host file carries no egress line (the filter is not part of this
    host's seal), else (ok, what): loaded, refreshed within 15 minutes and in the mode of the host file."""
    s = status(host)
    if not s["line"]:
        return None
    ok = s["host_mode"] is not None and s["loaded"] and not s["stale"] and s["mode"] == s["host_mode"]
    return ok, "the egress filter is loaded and refreshed in the mode of the host file (%s)" % (s["host_mode"]
                                                                                               or "no mode")


def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    from awb.cli import SafeParser

    host = host or Host()
    ap = SafeParser(prog="awb egress", description="The egress filter of the work user (seal/egress/ALLOW.md).")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    st = sub.add_parser("status", help="the mode, the load and the counts of the last 24 hours")
    st.add_argument("--json", action="store_true")
    sub.add_parser("load", help="root: resolve the list, load the table or refresh its sets, record the counters")
    en = sub.add_parser("enforce", help="the owner with sudo: refuse every connection outside the list")
    en.add_argument("--accept", nargs="+", default=[], metavar="CLASS", help="a class you expect to lose")
    sub.add_parser("count", help="the owner with sudo: count again, refuse nothing")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        if args.command == "status":
            if args.json:
                print(json.dumps(status(host), sort_keys=True))
            else:
                for line in status_lines(host):
                    print(line)
            return 0
        if args.command == "load":
            done = load(host)
            print("egress: %s in the mode %s%s" % (done["action"], done["mode"],
                                                 ", %d hosts without an answer kept their addresses"
                                                 % done["unanswered"] if done["unanswered"] else ""))
            return 0
        if args.command == "enforce":
            print(enforce(host, set(args.accept)))
            return 0
        if args.command == "count":
            print(count(host))
            return 0
    except Refused as err:
        print("awb egress: %s" % err, file=sys.stderr)
        return 1
    except EgressError as err:
        print("awb egress: %s" % err, file=sys.stderr)
        return 2
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
