"""Tests of the egress filter of the work user: seal/egress/ALLOW.md, seal/egress/work-egress.nft, awb/egress.py,
the units and the steps of seal/setup.sh and seal/verify.sh.

Nothing here loads a rule: the commands get a stand-in for nft that keeps the table in memory. The rendered rules go
through `nft -c` (check only) where this user may run it, as root in a container does; elsewhere that part skips.
"""
from __future__ import annotations

import getpass
import json
import os
import pwd
import re
import subprocess
from pathlib import Path

import pytest

from awb import config, deploy, egress, hooks, seal
from tests.test_seal import USER, commands, fakebin, no_marks, run_script  # noqa: F401 (fakebin is a fixture)

REPO = Path(__file__).resolve().parent.parent
SEAL = REPO / "seal"
EGRESS = SEAL / "egress"
SETUP = SEAL / "setup.sh"
VERIFY = SEAL / "verify.sh"
ME = getpass.getuser()
HOUR = 3600.0
T0 = 1_791_000_000.0     # a fixed start, 2026-10-03


# --------------------------------------------------------------------------- a stand-in for nft


class FakeNft:
    """nft -f FILE, nft -f - and nft -j list table, with the table in memory. Never runs a command."""

    def __init__(self, fail_on: str | None = None):
        self.loaded: str | None = None
        self.counters: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.refreshes: list[str] = []
        self.fail_on = fail_on

    def bump(self, **counts: int) -> None:
        for k, v in counts.items():
            self.counters[k] = self.counters.get(k, 0) + v

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None):
        assert argv[0] == egress.NFT
        args = argv[1:]
        self.calls.append(args)
        if args == ["-j", "list", "table", "inet", egress.TABLE]:
            if self.loaded is None:
                return subprocess.CompletedProcess(argv, 1, "", "Error: No such file or directory\n")
            items = [{"metainfo": {"version": "1.0.9"}}]
            for name in egress.CLASSES + (egress.ALLOWED,):
                items.append({"counter": {"family": "inet", "name": "c_" + name, "table": egress.TABLE,
                                          "packets": self.counters.get(name, 0), "bytes": 0}})
            verdict = [{"reject": {"type": "tcp reset"}}] if "reject with tcp reset" in self.loaded else \
                [{"accept": None}]
            items.append({"rule": {"family": "inet", "table": egress.TABLE, "chain": "refused", "expr": verdict}})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"nftables": items}), "")
        if args[:1] == ["-f"] and args[1] == "-":
            assert "delete table" not in input and "flush set" in input
            self.refreshes.append(input)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:1] == ["-f"]:
            body = Path(args[1]).read_text(encoding="utf-8")
            if self.fail_on and self.fail_on in body:
                return subprocess.CompletedProcess(argv, 1, "", "Error: Could not process rule\n")
            self.loaded = body
            self.counters = {}
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError("an nft call the module never makes: %s" % args)


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


ADDRS = {"api.anthropic.com": ["160.79.104.10", "2607:6bc0::10"], "calculator.otc-service.com": ["80.158.5.54"],
         "iam.eu-de.otc.t-systems.com": ["100.125.40.50"], "github.com": ["140.82.121.3"]}


def _resolve(name: str) -> list[str]:
    return list(ADDRS.get(name, ["203.0.113.%d" % (len(name) % 200 + 1)]))


def _conf(path: Path, extra: str = "") -> Path:
    path.write_text("# written by seal/setup.sh\nowner = owner-%s\nwork_user = %s\nshared = /srv/x\n%s"
                    % (ME, ME, extra), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path):
    if pwd.getpwnam(ME).pw_uid == 0:
        pytest.skip("the work user of these tests is the current user, which must not be root")
    conf = _conf(tmp_path / "paths.conf")
    nft = FakeNft()
    clock = Clock()
    host = egress.Host(conf=conf, state=tmp_path / "state", source=EGRESS, run=nft, now=clock, resolve=_resolve,
                       geteuid=lambda: 0, environ={"SUDO_USER": "owner-%s" % ME}, chown_root=False)
    return host, nft, clock, conf


def _run_for(host, nft, clock, hours: float, every: float = 300.0, **bumps) -> None:
    """A load every `every` seconds for `hours`, the counters bumped by `bumps` before the first one."""
    nft.bump(**bumps)
    end = clock.t + hours * HOUR
    while clock.t < end:
        clock.t += every
        egress.load(host)


# --------------------------------------------------------------------------- the allow list


def _entries():
    return egress.read_allow((EGRESS / "ALLOW.md").read_text(encoding="utf-8"))


def test_the_allow_list_names_the_client_and_the_price_api_and_keeps_tenants_and_buckets_off():
    entries = _entries()
    allowed = {e.dest for e in entries if e.set == "allow"}
    assert allowed == {"api.anthropic.com", "claude.ai", "platform.claude.com", "downloads.claude.ai",
                       "http-intake.logs.us5.datadoghq.com", "browser-intake-us5-datadoghq.com",
                       "calculator.otc-service.com"}
    assert all(e.port == "tcp 443" for e in entries if e.set == "allow")
    assert not [d for d in allowed if d.endswith("t-systems.com") or "obs." in d or "github" in d]
    api = {e.dest for e in entries if e.set == "tcp_api"}
    for service in ("iam", "ecs", "evs", "vpc", "elb", "nat", "dns", "ims", "obs"):
        assert "%s.eu-de.otc.t-systems.com" % service in api, service
    assert "100.125.0.0/16" in api
    assert {e.dest for e in entries if e.set == "github"} >= {"github.com", "api.github.com"}
    assert len({e.dest for e in entries}) == len(entries)
    text = (EGRESS / "ALLOW.md").read_text(encoding="utf-8")
    for word in ("registry.npmjs.org", "key service", "lease", "Unix sockets", "127.0.0.53", "How it was measured"):
        assert word in text, word


@pytest.mark.parametrize("row, message", [
    ("| `api.anthropic.com` | `tcp 443` | `open` | x |", "a set other than"),
    ("| `api.anthropic.com` | `any` | `allow` | x |", "an allowed row is tcp 443"),
    ("| `github.com` | `tcp 443` | `github` | x |", "an allowed row is tcp 443"),
    ("| `/etc/passwd` | `tcp 443` | `allow` | x |", "neither a host name nor a network"),
    ("| `10.0.0.1/8` | `tcp 443` | `allow` | x |", "neither a host name nor a network"),
])
def test_a_bad_row_of_the_allow_list_stops_the_load(row, message):
    good = "| `api.anthropic.com` | `tcp 443` | `allow` | the API |\n"
    with pytest.raises(egress.EgressError, match=message):
        egress.read_allow(good + row + "\n")


def test_an_allow_list_without_an_allowed_row_stops_the_load():
    with pytest.raises(egress.EgressError, match="no allowed host"):
        egress.read_allow("| `github.com` | `any` | `github` | x |\n| prose | row |\n")


def test_the_sets_collapse_overlaps_and_keep_both_families():
    entries = [egress.Entry("api.anthropic.com", "tcp 443", "allow"), egress.Entry("100.125.0.0/16", "any", "tcp_api"),
               egress.Entry("iam.eu-de.otc.t-systems.com", "any", "tcp_api")]
    cache = {"api.anthropic.com": {"160.79.104.10": T0, "2607:6bc0::10": T0},
             "iam.eu-de.otc.t-systems.com": {"100.125.40.50": T0, "80.158.72.18": T0}}
    sets = egress.set_elements(entries, cache)
    assert sets["allow4"] == ["160.79.104.10"] and sets["allow6"] == ["2607:6bc0::10"]
    assert sets["tcp_api4"] == ["80.158.72.18", "100.125.0.0/16"]
    assert sets["github4"] == [] and sets["github6"] == []


def test_a_host_without_an_answer_keeps_its_addresses_for_seven_days():
    entries = [egress.Entry("api.anthropic.com", "tcp 443", "allow")]
    old = {"api.anthropic.com": {"160.79.104.10": T0 - 6 * 86400, "160.79.104.99": T0 - 8 * 86400},
           "gone.example.org": {"192.0.2.9": T0}}
    cache, missing = egress.update_cache(old, entries, lambda n: [], T0)
    assert missing == 1
    assert cache == {"api.anthropic.com": {"160.79.104.10": T0 - 6 * 86400}}
    cache, missing = egress.update_cache(cache, entries, lambda n: ["160.79.104.11"], T0 + 60)
    assert missing == 0 and set(cache["api.anthropic.com"]) == {"160.79.104.10", "160.79.104.11"}


# --------------------------------------------------------------------------- the rules


def _rendered(mode: str, user: str = "awb") -> str:
    template = (EGRESS / "work-egress.nft").read_text(encoding="utf-8")
    sets = egress.set_elements(_entries(), {h: {a: T0 for a in _resolve(h)} for h in
                                            {e.dest for e in _entries()}})
    return egress.render(template, user, mode, sets)


def _chain(text: str, name: str) -> list[str]:
    m = re.search(r"\n    chain %s \{\n(.*?)\n    \}" % name, text, re.S)
    assert m, name
    return [x.strip() for x in m.group(1).splitlines() if x.strip() and not x.strip().startswith("#")]


def _nft_check(text: str, tmp_path: Path) -> None:
    """nft -c -f where this user may run it; it never loads anything."""
    f = tmp_path / "rules.nft"
    f.write_text(text, encoding="utf-8")
    try:
        r = subprocess.run(["nft", "-c", "-f", str(f)], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        pytest.skip("nft is not installed")
    if "Operation not permitted" in r.stderr:
        pytest.skip("nft -c needs CAP_NET_ADMIN here (checked by hand as root on 2026-10-11)")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("mode", egress.MODES)
def test_the_rendered_rules_count_or_refuse_by_mode_for_ipv4_and_ipv6(mode, tmp_path):
    text = _rendered(mode)
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.strip().startswith("#")]
    assert lines[:3] == ["table inet awb_egress", "delete table inet awb_egress", "table inet awb_egress {"]
    assert not re.search(r"@[A-Z_]+@", text)
    refused = _chain(text, "refused")
    if mode == "count":
        assert refused == ["accept"]
        assert "reject" not in text
    else:
        assert refused == ["meta l4proto tcp reject with tcp reset", "reject with icmpx admin-prohibited"]
    work = _chain(text, "work")
    assert work[:4] == ['oifname "lo" accept', "ct state established,related accept",
                        'ip daddr @allow4 tcp dport 443 counter name "c_allowed" accept',
                        'ip6 daddr @allow6 tcp dport 443 counter name "c_allowed" accept']
    for name in egress.CLASSES:
        assert "    counter c_%s { }" % name in text, name
        assert any('counter name "c_%s" goto refused' % name in r for r in work), name
    assert work[-1] == 'counter name "c_other" goto refused'
    assert sum(1 for r in work if r.endswith("accept")) == 4
    for family in ("ip daddr", "ip6 daddr"):
        assert any(r.startswith(family + " @tcp_api") for r in work)
    assert "add element inet awb_egress allow4 { " in text and "add element inet awb_egress allow6 { " in text
    assert "add element inet awb_egress tcp_api4 { " in text
    _nft_check(_rendered(mode, ME), tmp_path)


def test_only_the_work_users_uid_enters_the_chain_so_the_key_service_passes_untouched():
    """The key service runs as the owner (its unit), the Ask page as awb-ask: the base chain sends the work user's
    uid alone into the chain work, no other chain hooks into the output, and nothing in the table names another
    uid. A render for the owner or root is refused."""
    text = _rendered("enforce")
    assert _chain(text, "output") == ["type filter hook output priority filter; policy accept;",
                                      'meta skuid "awb" jump work']
    assert text.count("hook ") == 1 and text.count("jump work") == 1
    assert len(re.findall(r"skuid", text)) == 1 and "skgid" not in text
    keyd = (SEAL / "awb-keyd.service").read_text(encoding="utf-8")
    assert re.search(r"(?m)^User=@OWNER@$", keyd)
    assert re.search(r"(?m)^User=awb-ask$", (SEAL / "awb-ask.service").read_text(encoding="utf-8"))
    for conf, message in (("owner = awb\nwork_user = awb\n", "neither the owner nor root"),
                          ("owner = x\nwork_user = root\n", "neither the owner nor root"),
                          ("owner = x\n", "names no work user"),
                          ("owner = x\nwork_user = no-such-user-awbtest\n", "does not exist")):
        with pytest.raises(egress.EgressError, match=message):
            egress._work_user(conf)


def test_the_template_keeps_no_placeholder_and_names_no_address_of_a_host():
    template = (EGRESS / "work-egress.nft").read_text(encoding="utf-8")
    assert set(re.findall(r"@[A-Z_]+@", template)) == {"@WORK_USER@", "@VERDICT@"}
    assert "160.79" not in template and "otc" not in template
    with pytest.raises(egress.EgressError, match="placeholder"):
        egress.render_rules(template + "\n# @OTHER@\n", "awb", "count")
    with pytest.raises(egress.EgressError, match="no user name"):
        egress.render_rules(template, "awb; flush ruleset", "count")


# --------------------------------------------------------------------------- the mode


@pytest.mark.parametrize("text, want", [
    (None, ("count", "no host file")),
    ("owner = a\nwork_user = awb\n", ("count", "no egress line, the default")),
    ("egress = enforce\n", ("enforce", "")),
    ("  egress=count  \n", ("count", "")),
    ("egress = drop\n", (None, "an egress line that is neither count nor enforce")),
    ("egress =\n", (None, "an egress line that is neither count nor enforce")),
    ("egress = count\negress = enforce\n", (None, "two egress lines")),
    ("# egress = enforce\n", ("count", "no egress line, the default")),
])
def test_the_mode_parses_from_the_host_file(text, want):
    assert egress.parse_mode(text) == want


def test_the_mode_comes_from_the_root_host_file_alone(tmp_path, monkeypatch):
    root = tmp_path / "root.conf"
    root.write_text("work_user = awb\negress = count\n", encoding="utf-8")
    other = tmp_path / "other.conf"
    other.write_text("work_user = awb\negress = enforce\n", encoding="utf-8")
    monkeypatch.setattr(config, "HOST_CONF", str(root))
    monkeypatch.setenv("AWB_CONF", str(other))
    assert egress.mode() == "count" and egress.has_line()
    root.write_text("work_user = awb\n", encoding="utf-8")
    assert egress.mode() == "count" and not egress.has_line()


def test_writing_the_mode_keeps_every_other_line(world):
    host, _, _, conf = world
    conf.write_text(conf.read_text() + "egress = count\nfront_socket = /run/awb-web/front.sock\n")
    egress.write_mode(host, "enforce")
    lines = conf.read_text().splitlines()
    assert lines.count("egress = enforce") == 1 and "egress = count" not in lines
    assert "front_socket = /run/awb-web/front.sock" in lines and "work_user = %s" % ME in lines
    assert oct(conf.stat().st_mode & 0o777) == "0o644"


# --------------------------------------------------------------------------- load, refresh and the counters


def test_a_load_then_refreshes_the_sets_in_place_and_records_what_each_class_counted(world):
    host, nft, clock, _ = world
    assert egress.load(host)["action"] == "loaded"
    assert nft.loaded and 'meta skuid "%s" jump work' % ME in nft.loaded and "add element" in nft.loaded
    st = json.loads((host.state_dir / "status.json").read_text())
    assert st["mode"] == "count" and st["count_since"] == st["loaded_at"]
    nft.bump(web=3, allowed=40, github=1)
    clock.t += 300
    assert egress.load(host)["action"] == "refreshed"
    assert len([c for c in nft.calls if c[:1] == ["-f"] and c[1] != "-"]) == 1
    assert "flush set inet awb_egress allow4" in nft.refreshes[-1]
    rows = egress.read_history(host)
    assert rows[-1][1:] == ("count", {"web": 3, "allowed": 40, "github": 1})
    nft.bump(web=2)
    clock.t += 300
    egress.load(host)
    assert egress.read_history(host)[-1][2] == {"web": 2}
    hist = (host.state_dir / "history.tsv").read_text()
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", hist)


def test_a_change_of_the_rules_reloads_the_table_and_loses_no_count(world, tmp_path):
    host, nft, clock, _ = world
    egress.load(host)
    nft.bump(ssh=2)
    clock.t += 300
    source = tmp_path / "source"
    source.mkdir()
    (source / "ALLOW.md").write_text((EGRESS / "ALLOW.md").read_text())
    (source / "work-egress.nft").write_text((EGRESS / "work-egress.nft").read_text() + "# changed\n")
    host.source = source
    assert egress.load(host)["action"] == "loaded"
    assert sum(r[2].get("ssh", 0) for r in egress.read_history(host)) == 2
    nft.bump(ssh=1)
    clock.t += 300
    egress.load(host)
    assert sum(r[2].get("ssh", 0) for r in egress.read_history(host)) == 3


def test_a_counter_below_its_last_value_counts_from_zero(world):
    host, _, clock, _ = world
    host.state_dir.mkdir()
    st: dict = {"last": {"web": 10}}
    assert egress.record(host, st, {"web": 4}, "count", clock.t) == {"web": 4}
    assert egress.record(host, st, {"web": 6}, "count", clock.t + 60) == {"web": 2}


def test_a_host_file_without_a_mode_leaves_the_table_alone(world):
    host, nft, _, conf = world
    egress.load(host)
    before = list(nft.calls)
    for extra in ("egress = drop\n", "egress = count\negress = enforce\n"):
        _conf(conf, extra)
        with pytest.raises(egress.EgressError, match="stays as it is"):
            egress.load(host)
    assert nft.calls == before


def test_load_and_the_switches_need_root_and_the_switches_the_owner(world):
    host, nft, _, _ = world
    host.geteuid = lambda: 1000
    for call in (lambda: egress.load(host), lambda: egress.enforce(host, set()), lambda: egress.count(host)):
        with pytest.raises(egress.EgressError, match="sudo awb egress"):
            call()
    host.geteuid = lambda: 0
    for env in ({}, {"SUDO_USER": ME}):
        host.environ = env
        for call in (lambda: egress.enforce(host, set()), lambda: egress.count(host)):
            with pytest.raises(egress.EgressError, match="as the owner"):
                call()
    assert nft.calls == []


# --------------------------------------------------------------------------- enforce


def test_enforce_is_refused_with_an_unexpected_class_in_the_last_24_hours(world):
    host, nft, clock, conf = world
    egress.load(host)
    _run_for(host, nft, clock, 2)
    _run_for(host, nft, clock, 23, web=3, probe=2, allowed=100)
    with pytest.raises(egress.Refused, match="web 3"):
        egress.enforce(host, set())
    assert egress.mode(host) == "count" and "reject" not in nft.loaded
    assert "probe" not in egress.unexpected({"probe": 2, "web": 3}, set())
    with pytest.raises(egress.EgressError, match="--accept takes a class"):
        egress.enforce(host, {"everything"})
    out = egress.enforce(host, {"web"})
    assert "accepted: web 3" in out
    assert egress.mode(host) == "enforce" and "egress = enforce" in conf.read_text()
    assert "reject with tcp reset" in nft.loaded
    st = json.loads((host.state_dir / "status.json").read_text())
    assert st["mode"] == "enforce" and st["count_since"] is None


def test_enforce_passes_after_a_quiet_day_and_count_goes_back(world):
    host, nft, clock, _ = world
    egress.load(host)
    _run_for(host, nft, clock, 1)
    _run_for(host, nft, clock, 23.5, allowed=500, probe=1)
    assert "enforce" in egress.enforce(host, set())
    assert egress.mode(host) == "enforce"
    assert "count" in egress.count(host)
    assert egress.mode(host) == "count" and "reject" not in nft.loaded
    with pytest.raises(egress.Refused, match="less than 24 hours"):
        egress.enforce(host, set())


def test_old_counts_outside_the_window_do_not_stop_enforce(world):
    host, nft, clock, _ = world
    egress.load(host)
    _run_for(host, nft, clock, 2, mail=5)
    _run_for(host, nft, clock, 24.5)
    assert "enforce" in egress.enforce(host, set())


def test_enforce_is_refused_before_24_hours_and_after_a_gap(world):
    host, nft, clock, _ = world
    egress.load(host)
    _run_for(host, nft, clock, 23)
    with pytest.raises(egress.Refused, match="less than 24 hours"):
        egress.enforce(host, set())
    clock.t += 45 * 60
    _run_for(host, nft, clock, 2)
    with pytest.raises(egress.Refused, match=r"a gap of \d+ minutes"):
        egress.enforce(host, set())
    assert egress.mode(host) == "count"


def test_a_failed_enforce_load_goes_back_to_count(world):
    host, nft, clock, _ = world
    egress.load(host)
    _run_for(host, nft, clock, 24.5)
    nft.fail_on = "reject with tcp reset"
    with pytest.raises(egress.EgressError, match="nft refused the rules"):
        egress.enforce(host, set())
    assert egress.mode(host) == "count" and "reject" not in nft.loaded


def test_the_command_exits_one_on_a_refusal_and_two_on_an_error(world, capsys):
    host, nft, clock, _ = world
    assert egress.main(["load"], host) == 0
    _run_for(host, nft, clock, 2)
    _run_for(host, nft, clock, 23, dns=1)
    assert egress.main(["enforce"], host) == 1
    err = capsys.readouterr().err
    assert "dns 1" in err and "the mode stays count" in err
    assert egress.main(["enforce", "--accept", "nothing"], host) == 2
    assert egress.main(["status"], host) == 0
    out = capsys.readouterr().out
    assert "egress: mode count" in out and "counted outside the list: dns 1" in out


# --------------------------------------------------------------------------- what the owner and the work user see


def test_the_status_lines_and_the_seal_check_show_the_mode_and_the_counts(world):
    host, nft, clock, conf = world
    assert egress.check(host) is None
    assert egress.status_lines(host) == ["egress: no filter loaded on this host (mode count in the host file; "
                                         "seal/setup.sh loads it)"]
    _conf(conf, "egress = count\n")
    assert egress.check(host) == (False, "the egress filter is loaded and refreshed in the mode of the host file "
                                         "(count)")
    egress.load(host)
    nft.bump(web=7)
    clock.t += 300
    egress.load(host)
    ok, what = egress.check(host)
    assert ok and "(count)" in what
    lines = egress.status_lines(host)
    assert lines[0].startswith("egress: mode count, loaded ")
    assert lines[-1] == ("egress: last 24 hours (counting since %s, less than 24 hours): allowed 0; counted outside "
                         "the list: web 7" % egress._now_text(T0))
    clock.t += 20 * 60
    assert egress.check(host)[0] is False
    assert "older than 15 minutes" in egress.status_lines(host)[0]
    egress.load(host)
    _conf(conf, "egress = enforce\n")
    assert egress.check(host)[0] is False
    assert "the host file says enforce, the table runs count" in "\n".join(egress.status_lines(host))


def test_awb_seal_check_carries_the_egress_line_only_where_the_seal_installed_the_filter(world, monkeypatch, tmp_path):
    host, nft, clock, conf = world
    monkeypatch.setattr(config, "HOST_CONF", str(conf))
    monkeypatch.setattr(egress, "STATE_DIR", host.state_dir)
    real = egress.Host
    monkeypatch.setattr(egress, "Host", lambda: real(conf=conf, state=host.state_dir, now=clock))
    p = config.Paths(shared=tmp_path / "s", vault=tmp_path / "v", projects_root=tmp_path)
    monkeypatch.setattr(hooks, "selftest", lambda: [])
    monkeypatch.setattr(seal, "_sudo_allowed", lambda: False)
    monkeypatch.setattr(seal, "_ping", lambda sock: False)
    whats = [r.what for r in seal.run_checks(p, tmp_path / "home")]
    assert not [w for w in whats if "egress" in w]
    _conf(conf, "egress = count\n")
    got = [r for r in seal.run_checks(p, tmp_path / "home") if "egress" in r.what]
    assert len(got) == 1 and not got[0].ok
    egress.load(host)
    got = [r for r in seal.run_checks(p, tmp_path / "home") if "egress" in r.what]
    assert got[0].ok


def test_awb_deploy_status_prints_the_egress_lines(world, monkeypatch, capsys):
    host, nft, clock, conf = world
    _conf(conf, "egress = count\n")
    egress.load(host)
    monkeypatch.setattr(config, "HOST_CONF", str(conf))
    monkeypatch.setattr(egress, "STATE_DIR", host.state_dir)
    real = egress.Host
    monkeypatch.setattr(egress, "Host", lambda: real(conf=conf, state=host.state_dir, now=clock))
    monkeypatch.setattr(deploy, "owner_daemon_states", lambda: {})
    h = deploy.Host(root=host.state_dir / "opt", runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert deploy.owner_status(h) == 0
    out = capsys.readouterr().out
    assert "egress: mode count, loaded " in out and "counted outside the list: none" in out


# --------------------------------------------------------------------------- the seal scripts, the units, the deploy


def test_setup_installs_the_units_and_loads_the_filter_in_count_mode(fakebin):
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    joined = "\n".join(commands(res.stdout))
    assert "    | egress = count" in res.stdout.splitlines()
    for line in (r"^\+ write /etc/systemd/system/awb-egress\.service ", r"^\+ write /etc/systemd/system/awb-egress\.timer ",
                 r"^\+ systemctl enable --now awb-egress\.timer$", r"^\+ /usr/local/bin/awb egress load$"):
        assert re.search(line, joined, re.M), line
    assert joined.index("awb-egress.timer") < joined.index("/usr/local/bin/awb egress load")


def test_the_update_of_a_deploy_loads_the_filter_too(fakebin):
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    joined = "\n".join(commands(res.stdout))
    assert re.search(r"^\+ /usr/local/bin/awb egress load$", joined, re.M)
    assert re.search(r"^\+ write /etc/systemd/system/awb-egress\.timer ", joined, re.M)
    units_only = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert units_only.returncode == 0, units_only.stderr
    assert not re.search(r"^\+ /usr/local/bin/awb egress load$", "\n".join(commands(units_only.stdout)), re.M)


def test_setup_keeps_the_mode_of_the_host_file():
    text = SETUP.read_text(encoding="utf-8")
    assert "egress = $(kept_conf egress count)" in text


def test_the_units_load_at_boot_before_the_logins_and_every_five_minutes():
    unit = (SEAL / "awb-egress.service").read_text(encoding="utf-8")
    for line in ("Type=oneshot", "ExecStart=/usr/local/bin/awb egress load", "WantedBy=multi-user.target"):
        assert line in unit.splitlines(), line
    before = re.search(r"(?m)^Before=(.*)$", unit).group(1).split()
    assert {"systemd-user-sessions.service", "cron.service", "awb-portal.service"} <= set(before)
    assert not re.search(r"(?m)^User=", unit)
    timer = (SEAL / "awb-egress.timer").read_text(encoding="utf-8").splitlines()
    for line in ("OnUnitActiveSec=5min", "Unit=awb-egress.service", "WantedBy=timers.target"):
        assert line in timer, line


def test_verify_lists_the_egress_checks(fakebin):
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for must in ("the egress table of the work user is loaded",
                 "the egress filter runs in the mode of the host file",
                 "only the work user's uid enters the egress chain",
                 "the key service runs under a uid other than the work user's",
                 "a planted connection of the work user outside the list is counted",
                 "a planted connection of the owner's uid passes the filter uncounted",
                 "the timer of the egress filter is active"):
        assert any(must in c for c in checks), must
    assert "nft" in VERIFY.read_text(encoding="utf-8") and "192.0.2.1" in VERIFY.read_text(encoding="utf-8")


def test_a_deploy_maps_the_egress_files_to_no_restart():
    tree = deploy._SealTree(REPO)
    units = deploy.templates(tree)
    assert "awb-egress.service" in units and "awb-egress.service" in deploy.oneshot_units(tree, units)
    eff = deploy.map_changes(["seal/egress/ALLOW.md", "seal/egress/work-egress.nft", "seal/awb-egress.timer"],
                             units, {u: set() for u in units}, {u: set() for u in units})
    assert eff.units == {} and eff.unmapped == [] and eff.every == []
    from awb import cli
    assert cli.DELEGATED["egress"][0] == "egress"
