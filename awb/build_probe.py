"""`awb build probe` (SB5): the network of a task unit, proven in units of the same form.

A part of the build runner (awb/buildrun.py, F1 step 6). The model endpoint and the token host of his login connect,
a TCP API host and GitHub do not; seal/verify.sh runs it and `awb seal check` reads the record it leaves.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from awb import build, config
from awb.build_unit_side import MODELS
from awb.build_units import (MODEL_HOST, PROBE_UNIT, TOKEN_HOST, Ended, Host, Spec, _hidden, _text, addresses,
                             board_read, board_write, model_allow, name_binds, resolvers)


# --------------------------------------------------------------------------- the fixed values

NO_RESOLVER = "no resolver for the task unit"
TCP_API_HOST = "iam.eu-de.otc.t-systems.com"    # seal/verify.sh names the same host
GITHUB_HOST = "github.com"
PROBE_SECONDS = 60
PROBE_FILE = "probe.tsv"


# --------------------------------------------------------------------------- the probe unit (SB5)

PROBE_PY = ("import socket, sys\n"
            "port = int(sys.argv[1]) if len(sys.argv) > 2 else 443\n"
            "try:\n"
            "    found = socket.getaddrinfo(sys.argv[-1], port, proto=socket.IPPROTO_TCP)\n"
            "except OSError:\n"
            "    sys.exit(2)\n"
            "for family, kind, proto, _, addr in found:\n"
            "    s = socket.socket(family, kind, proto)\n"
            "    s.settimeout(5)\n"
            "    try:\n"
            "        s.connect(addr)\n"
            "        sys.exit(0)\n"
            "    except OSError:\n"
            "        pass\n"
            "    finally:\n"
            "        s.close()\n"
            "sys.exit(1)\n")
"""`PROBE_PY [PORT] HOST`: exit 0 when a TCP connection to the port (443 by default) of the host opens within five
seconds, 1 when the name resolved and no connection opened, 2 when the name did not resolve."""
PROBE_EXIT = {1: "no connection", 2: "no name resolution"}
PROBE_UNPROVEN = "not proven: the unit could not reach the model endpoint"
PROBE_PYTHON = "/usr/bin/python3"
PROBE_TARGETS = {"model": MODEL_HOST, "token": TOKEN_HOST, "tcp": TCP_API_HOST, "github": GITHUB_HOST}
PROBE_REACH = ("model", "token")
PROBE_SSH_PORT = 22
PROBE_LOCAL = {"loopback": "127.0.0.1", "stub": "127.0.0.53"}
"""The host's own addresses a task unit must not reach on any port (finding of 2026-10-12: sshd listens on
0.0.0.0:22, so the stub's address answered); the third check takes the host's own address (`host_address`)."""


def host_address() -> str | None:
    """The host's own address on its default route: a UDP socket connected to a documentation address sends
    nothing and names the source address the host would use. None without a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            addr = s.getsockname()[0]
    except OSError:
        return None
    return None if addr == "0.0.0.0" else addr


def probe_target(key: str) -> list[str] | None:
    """The probe's arguments for a check: a host name (port 443) or port 22 and an address of the host itself;
    None when the host has no address to try."""
    if key in PROBE_TARGETS:
        return [PROBE_TARGETS[key]]
    addr = PROBE_LOCAL[key] if key in PROBE_LOCAL else host_address()
    return None if addr is None else [str(PROBE_SSH_PORT), addr]


def probe_lines(model: str) -> list[tuple[str, str]]:
    """(key, line) of each check of the probe unit, in order; the token host of his login only with build_model =
    login. seal/verify.sh prints the same lines."""
    out = [("unit", "the probe unit with a task's own properties runs as the build user"),
           ("model", "the probe unit reaches the model endpoint (%s)" % MODEL_HOST)]
    if model == "login":
        out.append(("token", "the probe unit reaches the token host of his login (%s)" % TOKEN_HOST))
    out += [("tcp", "the probe unit cannot reach a TCP API host (%s)" % TCP_API_HOST),
            ("github", "the probe unit cannot reach %s" % GITHUB_HOST),
            ("loopback", "the probe unit cannot connect to the host's loopback (%s:%d)" % (PROBE_LOCAL["loopback"],
                                                                                         PROBE_SSH_PORT)),
            ("stub", "the probe unit cannot connect to the resolver stub's address (%s:%d)" % (PROBE_LOCAL["stub"],
                                                                                             PROBE_SSH_PORT)),
            ("host", "the probe unit cannot connect to the host's own address on port %d" % PROBE_SSH_PORT)]
    return out


def probe_spec(host: Host, argv: list[str], allow: list[str], stdout: Path | None = None,
               binds: list[tuple[str, str]] | None = None) -> Spec:
    """A unit with the network, the hidden places and the environment of a task's session (`Runner.spawn`): the
    address list of model_allow, a hosts file and a resolv.conf without a nameserver over /etc/hosts and
    /etc/resolv.conf (`name_binds`), every clone, home and the records hidden, /etc/awb inaccessible."""
    return Spec(PROBE_UNIT, argv, PROBE_SECONDS, "/", "/tmp", hidden=_hidden(host, None), allow=allow,
                binds=list(binds or []), stdout=stdout,
                env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"})


def _probe_wait(host: Host, spec: Spec) -> Ended:
    handle = host.units.start(spec)
    while True:
        e = handle.poll()
        if e is not None:
            return e
        time.sleep(0.2)


def run_probe(host: Host, user: str) -> list[tuple[str, bool, str]]:
    """(key, passed, line) per check. The control runs `id -un` first: a probe counts only after the unit was shown
    to run as the build user (a unit that never starts would make every refused connection pass). A refused
    connection passes only as the probe's own exit 1 (the name resolved, no connection opened) and only after the
    model endpoint connected in the same run: a unit that can resolve no name refuses every host, so a refusal alone
    proves nothing. A failed line names the probe's exit (no name resolution or no connection)."""
    lines = probe_lines(host.model)
    if host.resolve is None and not resolvers():
        return [(k, False, "%s (%s)" % (ln, NO_RESOLVER)) for k, ln in lines]
    found = model_allow(host)
    if found is None:
        return [(k, False, "%s (the model endpoint does not resolve)" % ln) for k, ln in lines]
    allow, names = found
    # the hosts the unit must not reach stand in its hosts file too, never in its allow list: a refusal is then the
    # unit's address filter and not a name that does not resolve
    for name in (TCP_API_HOST, GITHUB_HOST) if host.resolve is None else ():
        far = addresses(name)
        if far is not None:
            names = {**names, name: far}
    with tempfile.TemporaryDirectory(prefix="awb-build-probe-") as tmp:
        binds = name_binds(Path(tmp), names)
        out = Path(tmp) / "id.out"
        e = _probe_wait(host, probe_spec(host, ["/usr/bin/id", "-un"], allow, stdout=out, binds=binds))
        who = (_text(out) or "").strip()
        ran = e.result == "success" and e.code == 0 and who == user
        res = [(lines[0][0], ran, lines[0][1])]
        res += _probe_checks(host, lines[1:], allow, binds) if ran else [
            (key, False, line + " (the probe unit did not run)") for key, line in lines[1:]]
    return res


def _probe_checks(host: Host, lines: list[tuple[str, str]], allow: list[str],
                  binds: list[tuple[str, str]]) -> list[tuple[str, bool, str]]:
    """The checks after the control, each in a unit of its own: reach the endpoints, then the refusals."""
    res = []
    reached = False
    for key, line in lines:
        target = probe_target(key)
        if target is None:
            res.append((key, False, line + " (no address of the host found)"))
            continue
        e = _probe_wait(host, probe_spec(host, [PROBE_PYTHON, "-I", "-c", PROBE_PY, *target], allow, binds=binds))
        why = PROBE_EXIT.get(e.code) if e.result == "exit-code" else None
        if key in PROBE_REACH:
            ok = e.result == "success" and e.code == 0
            reached = reached or (ok and key == "model")
            res.append((key, ok, line if ok else "%s (%s)" % (line, why or e.result)))
        elif not reached:
            res.append((key, False, "%s (%s)" % (line, PROBE_UNPROVEN)))
        elif e.result == "exit-code" and e.code == 1:
            res.append((key, True, line))
        else:
            res.append((key, False, "%s (%s)" % (line, "connected" if e.result == "success" else why or e.result)))
    return res


def write_probe(host: Host, results: list[tuple[str, bool, str]]) -> bool:
    """<shared>/build/probe.tsv: the time, the build model and pass or fail per key; what `awb seal check` reads."""
    failed = sum(1 for _, ok, _ in results if not ok)
    rows = ["# written by awb build probe (seal/verify.sh): the checks of the probe unit, keys and times only",
            "probe\t%s\t%s\t%d\t%d" % (host.now().strftime(build._STAMP), host.model, len(results) - failed,
                                          failed)]
    rows += ["check\t%s\t%s" % (key, "pass" if ok else "fail") for key, ok, _ in results]
    return board_write(host.board, PROBE_FILE, ("\n".join(rows) + "\n").encode("utf-8"))


def read_probe(board: Path | None) -> dict | None:
    """The record of the last probe, every cell matched: {"time", "model", "checks": {key: passed}}; None without
    a readable record."""
    text = board_read(board, PROBE_FILE)
    if text is None:
        return None
    out: dict = {"time": None, "model": None, "checks": {}}
    for row in text.splitlines():
        cells = row.split("\t")
        if cells[0] == "probe" and len(cells) == 5 and cells[2] in MODELS:
            try:
                datetime.strptime(cells[1], build._STAMP)
            except ValueError:
                continue
            out["time"], out["model"] = cells[1], cells[2]
        elif cells[0] == "check" and len(cells) == 3 and cells[1] in {k for k, _ in probe_lines("login")} \
                and cells[2] in ("pass", "fail"):
            out["checks"][cells[1]] = cells[2] == "pass"
    return out if out["time"] else None


def probe_check(board: Path | None) -> tuple[bool, str, str]:
    """For `awb seal check` (the work user cannot start a unit): (passed, what, note) from the record of the last
    probe that seal/verify.sh ran. Passes only when every check of its build model passed."""
    rec = read_probe(board)
    if rec is None:
        return False, "the probe unit with a task's own properties passed its checks", \
            "no record yet: sudo seal/verify.sh runs it"
    want = [k for k, _ in probe_lines(rec["model"])]
    bad = [k for k in want if rec["checks"].get(k) is not True]
    what = "the probe unit with a task's own properties passed its %d checks (sudo seal/verify.sh, %s)" % (
        len(want), rec["time"].replace("T", " ")[:16])
    return not bad, what, ("failed: %s" % ", ".join(bad)) if bad else ""


def probe_main() -> int:
    """`awb build probe`: the probe unit with a task's own properties, as the owner through the runner's one
    systemd-run form; PASS or FAIL per line, the record for `awb seal check`. seal/verify.sh runs it."""
    if config.is_work_user() or config.is_build_user():
        print("awb build: the probe is the owner's", file=sys.stderr)
        return 2
    user = config.build_user()
    if not user:
        print("awb build: this host has no build user", file=sys.stderr)
        return 2
    try:
        host = Host.from_config()
    except Exception as err:
        print("awb build: the probe cannot start (%s)" % type(err).__name__, file=sys.stderr)
        return 2
    results = run_probe(host, user)
    for _, ok, line in results:
        print("%s  %s" % ("PASS" if ok else "FAIL", line))
    failed = sum(1 for _, ok, _ in results if not ok)
    if not write_probe(host, results):
        print("# the record for awb seal check could not be written")
    print("# %d checks, %d failed" % (len(results), failed))
    return 1 if failed else 0
