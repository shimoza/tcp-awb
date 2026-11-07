"""B-046: the console page (awb/tcp/web/portal.html) is held to the API contract and ships with `sudo awb deploy`.

From the repository alone: every API path the page calls is a path of docs/api/openapi.yaml, every top-level field
of the /api/board answer is read by the page or listed in BOARD_NOT_SHOWN with a reason, the page passes the checks
of `awb web publish` and the page step of the deploy publishes, leaves, refuses and skips as it should (a temporary
web folder, never the real one). Every test names the planted case that must turn it red.
"""
from __future__ import annotations

import contextlib
import io
import os
import pwd
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from awb import deploy, gate
from awb.tcp.web import publish
from tests.conftest import not_as_root

REPO = Path(__file__).resolve().parent.parent
PORTAL = REPO / deploy.PAGE
CONTRACT = REPO / "docs" / "api" / "openapi.yaml"
BIN = "/usr/local/bin/awb"
C1 = "b" * 40

# The fields of the /api/board answer the page does not read, each with the reason why not. A new field of the board
# is red here until the page reads it or this list says why not; a field the page reads must leave the list.
BOARD_NOT_SHOWN: dict[str, str] = {}
"""Board fields the page does not show, each with its reason. Empty since the pult page of 2026-11-07 shows the
platform, the parked projects and the build line."""


def page_text() -> str:
    return PORTAL.read_text(encoding="utf-8")


def contract_text() -> str:
    return CONTRACT.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the paths the page calls

_QUOTES = "'\"`"


def _literal(text: str, i: int) -> tuple[str, int]:
    """The string literal that starts at text[i] (a quote) with every ${...} as {}; the index after it."""
    q, i, out = text[i], i + 1, []
    while i < len(text) and text[i] != q:
        if text[i] == "\\":
            out.append(text[i + 1:i + 2])
            i += 2
        elif q == "`" and text.startswith("${", i):
            depth, i = 1, i + 2
            while i < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
            out.append("{}")
        else:
            out.append(text[i])
            i += 1
    return "".join(out), i + 1


def _expression(text: str, i: int) -> int:
    """The index after one operand of a `+` chain: an identifier, a call or a member, balanced brackets."""
    depth = 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (c in "+,;?:" or c.isspace()):
            break
        elif c in _QUOTES:
            _, i = _literal(text, i)
            continue
        i += 1
    return i


def api_calls(text: str) -> set[str]:
    """Every API path the page names, as a template: a `+` operand or a ${...} is {}, the query is cut off."""
    out = set()
    def skip(i: int) -> int:
        while i < len(text) and text[i].isspace():
            i += 1
        return i

    for m in re.finditer(r"['\"`](?=/api/)", text):
        parts, i = [], m.start()
        while i < len(text):
            if text[i] in _QUOTES:
                lit, i = _literal(text, i)
                parts.append(lit)
            else:
                i = _expression(text, i)
                parts.append("{}")
            j = skip(i)
            if j >= len(text) or text[j] != "+":
                break
            i = skip(j + 1)
        out.add("".join(parts).split("?", 1)[0])
    return out


def contract_paths(text: str) -> list[str]:
    """The keys of `paths:` in the contract (two spaces in, quoted or not)."""
    head = text.index("\npaths:\n")
    end = text.index("\ncomponents:\n", head)
    return [m.group(1) for m in re.finditer(r'^  "?(/[^":]*)"?:\s*$', text[head:end], re.M)]


def unknown_calls(page: str, contract: str) -> list[str]:
    """The calls of the page that match no path of the contract: a {} of the page and a {name} of the contract
    stand for one segment or part of one."""
    samples = [re.sub(r"\{[^/}]*\}", "x1", p) for p in contract_paths(contract)]
    out = []
    for call in sorted(api_calls(page)):
        pattern = "^%s$" % "[^/]*".join(re.escape(piece) for piece in call.split("{}"))
        if not any(re.match(pattern, s) for s in samples):
            out.append(call)
    return out


def test_every_api_path_the_page_calls_is_in_the_contract():
    """Planted: a call of a path the contract lacks, one with an id under /api/projects/ and one built with a
    template literal are found; the page itself has none."""
    page, contract = page_text(), contract_text()
    calls = api_calls(page)
    for call in ("/api/board", "/api/projects", "/api/projects/{}", "/api/projects/{}/materials",
                 "/api/projects/{}/chat", "/api/{}-operations/{}", "/api/kb", "/api/price"):
        assert call in calls, call
    assert "/api/projects/{code}/materials/{material_id}" in contract_paths(contract)
    assert unknown_calls(page, contract) == []
    planted = page + "<script>readAPI('/api/nothing');readAPI('/api/projects/'+code+'/nowhere');" \
                     "fetch(`/api/projects/${code}/void?x=1`)</script>"
    assert unknown_calls(planted, contract) == ["/api/nothing", "/api/projects/{}/nowhere", "/api/projects/{}/void"]
    # a call with an id matches the template of the contract and nothing longer
    assert unknown_calls("readAPI('/api/projects/'+encodeURIComponent(id))", contract) == []
    assert unknown_calls("readAPI('/api/projects/'+id+'/materials/'+m+'/more')", contract) == [
        "/api/projects/{}/materials/{}/more"]


# --------------------------------------------------------------------------- the board fields

def board_fields(text: str) -> list[str]:
    """The top-level properties of the schema the 200 answer of GET /api/board refers to."""
    m = re.search(r"^  /api/board:\n(.*?)(?=^  \S)", text, re.M | re.S)
    ok = m.group(1).split('"200":', 1)[1]
    name = re.search(r'"\$ref": "#/components/schemas/(\w+)"', ok).group(1)
    m = re.search(r"^    %s:\n(.*?)(?=^    \S)" % name, text, re.M | re.S)
    props = m.group(1).split("\n      properties:\n", 1)[1]
    props = re.split(r"^      \S", props, maxsplit=1, flags=re.M)[0]
    return re.findall(r"^        (\w+):", props, re.M)


def board_code(page: str) -> str:
    """The functions of the page whose name holds `board`: where the board answer is read."""
    chunks = re.split(r"\n(?=(?:async\s+)?function\s)", page)
    return "\n".join(c for c in chunks if re.match(r"(?:async\s+)?function\s+\w*[Bb]oard\w*\s*\(", c))


def board_problems(page: str, contract: str, not_shown: dict[str, str]) -> list[str]:
    code = board_code(page)
    fields = board_fields(contract)
    read = {f for f in fields if re.search(r"(\.\s*%s\b|\[\s*['\"]%s['\"]\s*\]|\{[^{}=]*\b%s\b[^{}=]*\}\s*=)"
                                           % (f, f, f), code)}
    out = ["not read and not listed: %s" % f for f in fields if f not in read and f not in not_shown]
    out += ["read but listed: %s" % f for f in sorted(read & set(not_shown))]
    out += ["listed but not in the contract: %s" % f for f in sorted(set(not_shown) - set(fields))]
    out += ["listed without a reason: %s" % f for f, why in sorted(not_shown.items()) if not why.strip()]
    return out


def test_every_board_field_is_read_by_the_page_or_listed_with_a_reason():
    """Planted: a new field of the Board schema, the list without platform, a listed field the page starts to read
    and a listed field the contract lost are each red."""
    page, contract = page_text(), contract_text()
    fields = board_fields(contract)
    assert {"source", "generated_at", "summary", "projects", "parked", "build", "platform"} <= set(fields)
    assert "data.projects" in board_code(page) and "function paintBoard" in board_code(page)
    assert board_problems(page, contract, BOARD_NOT_SHOWN) == []
    grown = contract.replace("        platform:\n          \"$ref\": \"#/components/schemas/BoardPlatform\"\n",
                             "        platform:\n          \"$ref\": \"#/components/schemas/BoardPlatform\"\n"
                             "        quota:\n          type: integer\n", 1)
    assert board_problems(page, grown, BOARD_NOT_SHOWN) == ["not read and not listed: quota"]
    hidden = page.replace(".platform", ".platfrom")
    assert board_problems(hidden, contract, BOARD_NOT_SHOWN) == ["not read and not listed: platform"]
    assert board_problems(hidden, contract, dict(BOARD_NOT_SHOWN, platform="a reason")) == []
    assert board_problems(page, contract, dict(BOARD_NOT_SHOWN, platform="a reason")) == ["read but listed: platform"]
    assert board_problems(page, contract, dict(BOARD_NOT_SHOWN, gone="a field that left")) == [
        "listed but not in the contract: gone"]
    assert board_problems(page, contract, dict(BOARD_NOT_SHOWN, gone=" ")) == [
        "listed but not in the contract: gone", "listed without a reason: gone"]


# --------------------------------------------------------------------------- the checks of awb web publish


def test_the_page_passes_the_checks_of_publish(home):
    """Planted: a second <title>, an external script and a secret of the gate's self-test in the page are refused
    by the same checks; the page itself passes them with the fixture register."""
    data = PORTAL.read_bytes()
    assert publish.checked_page(None, PORTAL) == data
    text = data.decode("utf-8")
    assert publish.html_problems(text) == []
    matcher = publish.name_matcher()
    assert publish.html_problems(text.replace("</head>", "<title>again</title></head>", 1)) == [
        "html: not exactly one <title>"]
    assert publish.html_problems(text.replace("</head>", '<script src="https://cdn.example/x.js"></script></head>',
                                              1)) == ["html: an external script"]
    planted = gate._selftest_cases()["secret"].decode("utf-8")
    assert publish.content_problems(text.replace("</body>", "<pre>%s</pre></body>" % planted, 1).encode("utf-8"),
                                    matcher) == ["secret: 1 line"]


# --------------------------------------------------------------------------- the page step of the deploy


class Checker:
    """The runner of the root run. `awb web publish --release-page` (the page step's own process) runs publish.main
    in this process as root with this runner; `awb web check --emit FILE` as the owner runs the real check of
    publish.py (the fixture register). Every call is recorded."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.chown = None

    def __call__(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append((cmd, kw))
        if cmd[:4] == [BIN, "web", "publish", "--release-page"]:
            out = io.StringIO()
            host = publish.Host(geteuid=lambda: 0, runner=self, environ=dict(kw["env"]), chown=self.chown, bin=BIN)
            with contextlib.redirect_stdout(out):
                rc = publish.main(cmd[2:], host=host)
            return subprocess.CompletedProcess(cmd, rc, out.getvalue().encode("utf-8"), b"")
        assert cmd[:4] == [BIN, "web", "check", "--emit"], cmd
        try:
            return subprocess.CompletedProcess(cmd, 0, publish.checked_page(None, Path(cmd[4])), b"")
        except publish.Refused as err:
            return subprocess.CompletedProcess(cmd, 1, b"", ("awb web: %s\n" % err).encode("utf-8"))

    def checks(self):
        return [(c, kw) for c, kw in self.calls if c[1:3] == ["web", "check"]]


def _step(tmp_path: Path, release_page: bytes | None, live: bytes | None, web: bool = True):
    """One page step of a root run: a release with `release_page`, a web folder with `live` (none without `web`)."""
    root = tmp_path / "opt"
    page = root / "releases" / C1 / deploy.PAGE
    page.parent.mkdir(parents=True)
    if release_page is not None:
        page.write_bytes(release_page)
    web_root = tmp_path / "web"
    if web:
        web_root.mkdir()
        if live is not None:
            (web_root / "index.html").write_bytes(live)
    runner, owned = Checker(), []
    runner.chown = lambda path, uid, gid: owned.append((Path(path).name, uid, gid))
    me = pwd.getpwuid(os.getuid())
    host = deploy.Host(root=root, runner=runner, geteuid=lambda: 0, environ={"SUDO_USER": "planted", "LANG": "C"},
                       web_root=web_root, chown=lambda path, uid, gid: owned.append((Path(path).name, uid, gid)),
                       bin=BIN)
    caller = deploy.Caller(owner=me, work=None, repo=tmp_path, conf={})
    deploy.Run(host, caller, {"target": C1}, deploy.Options()).page()
    return web_root, runner, owned, me


def _page(body: str) -> bytes:
    return ("<!doctype html><html><head><title>AWB</title></head><body><p>%s</p></body></html>\n" % body).encode()


def test_the_page_step_publishes_a_changed_page_with_a_backup(home, tmp_path, capsys):
    """Planted: a step that installs without the check as the owner, without a backup or as another user than root
    fails here."""
    web, runner, owned, me = _step(tmp_path, _page("new"), _page("old"))
    backup = "index.html.before-deploy-%s" % C1[:7]
    assert capsys.readouterr().out.splitlines() == ["# page published (backup %s)" % backup]
    assert (web / "index.html").read_bytes() == _page("new")
    assert (web / backup).read_bytes() == _page("old")
    assert (web / "index.html").stat().st_mode & 0o777 == 0o644
    assert (backup, 0, 0) in owned and len(owned) == 2
    publish_call = runner.calls[0][0]
    assert publish_call[:5] == [BIN, "web", "publish", "--release-page",
                                str(tmp_path / "opt" / "releases" / C1 / deploy.PAGE)]
    assert runner.calls[0][1]["env"]["SUDO_USER"] == me.pw_name
    [(cmd, kw)] = runner.checks()
    assert cmd == [BIN, "web", "check", "--emit", str(tmp_path / "opt" / "releases" / C1 / deploy.PAGE)]
    assert kw["user"] == me.pw_uid and kw["group"] == me.pw_gid and kw["env"]["HOME"] == me.pw_dir


def test_the_page_step_leaves_an_unchanged_page(home, tmp_path, capsys):
    """Planted: a step that publishes the same bytes again (a new backup each deploy) fails here."""
    web, runner, owned, _ = _step(tmp_path, _page("same"), _page("same"))
    assert capsys.readouterr().out.splitlines() == ["# page unchanged"]
    assert runner.calls == [] and owned == []
    assert sorted(p.name for p in web.iterdir()) == ["index.html"]


def test_the_page_step_keeps_the_live_page_on_a_refusal(home, tmp_path, capsys):
    """Planted: a release page with an external script or none at all; the live page stays and no backup is made."""
    bad = _page('</p><script src="https://cdn.example/x.js"></script><p>')
    web, runner, owned, _ = _step(tmp_path, bad, _page("old"))
    assert capsys.readouterr().out.splitlines() == [
        "# page refused (the page is refused: html: an external script, the live page stays)"]
    assert len(runner.checks()) == 1 and owned == []
    assert sorted(p.name for p in web.iterdir()) == ["index.html"]
    assert (web / "index.html").read_bytes() == _page("old")
    web, runner, owned, _ = _step(tmp_path / "none", None, _page("old"))
    assert capsys.readouterr().out.splitlines() == ["# page refused (the page file is missing, the live page stays)"]
    assert runner.calls == [] and (web / "index.html").read_bytes() == _page("old")


def test_the_page_step_skips_a_host_without_the_web_part(home, tmp_path, capsys):
    """Planted: a step that creates the web folder or calls the check on a host without it fails here."""
    web, runner, owned, _ = _step(tmp_path, _page("new"), None, web=False)
    assert capsys.readouterr().out.splitlines() == ["# page skipped (no %s on this host)" % web]
    assert not web.exists() and runner.calls == [] and owned == []
    assert deploy.page_web_root(deploy.Host()) == publish.SRV


def test_a_change_of_the_page_restarts_no_unit():
    """Planted: without its PACKAGE_DATA entry the page is an unmapped file of the package and restarts every
    unit."""
    units = {"awb-x.service": {"files": [], "dropins": []}}
    eff = deploy.map_changes([deploy.PAGE], units, {"awb-x.service": set()}, {}, reached=set())
    assert eff.units == {} and eff.unmapped == []
    saved = deploy.PACKAGE_DATA.pop(deploy.PAGE)
    try:
        eff = deploy.map_changes([deploy.PAGE], units, {"awb-x.service": set()}, {}, reached=set())
    finally:
        deploy.PACKAGE_DATA[deploy.PAGE] = saved
    assert eff.unmapped == [deploy.PAGE] and list(eff.units) == ["awb-x.service"]


# --------------------------------------------------------------------------- the step in a whole deploy


@pytest.fixture
def world(tmp_path):
    from tests.test_deploy import World

    return World(tmp_path)


def _release_with(world, page: bytes):
    """The archive of the target carries the page; `awb web publish --release-page` and `awb web check --emit` run
    the real publish and check."""
    checker = Checker()
    checker.chown = lambda *a: None

    def on(cmd, kw):
        if cmd[:3] == [BIN, "deploy", "archive"]:
            with tarfile.open(fileobj=kw["stdout"], mode="w") as tf:
                tf.add(REPO / "seal", arcname="seal")
                info = tarfile.TarInfo(deploy.PAGE)
                info.size = len(page)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(page))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] in ([BIN, "web", "check"], [BIN, "web", "publish"]):
            return checker(cmd, **kw)
        return None

    world.runner.on = on
    return checker


@not_as_root
def test_the_deploy_publishes_the_page_after_the_units_and_a_refusal_never_fails_it(home, world, tmp_path, capsys):
    """Planted: a page step before the units, one whose refusal changes the exit code or a deploy without it."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_bytes(_page("old"))
    kw = dict(web_root=web, chown=lambda *a: None)
    _release_with(world, _page("new"))
    world.runner.plan = world.plan(actions={"awb-ask.service": "restart"})
    assert world.main([], **kw) == 0
    out = capsys.readouterr().out.splitlines()
    line = "# page published (backup index.html.before-deploy-%s)" % world.c1[:7]
    assert line in out
    assert out.index(line) > max(i for i, s in enumerate(out) if s.startswith("+ ") and "install.sh" in s)
    assert (web / "index.html").read_bytes() == _page("new")
    # a later deploy with a refused page: exit 0, the live page stays
    world.c1 = "c" * 40
    _release_with(world, _page('</p><script src="//cdn.example/x.js"></script><p>'))
    world.runner.plan = world.plan(deployed="b" * 40, live="b" * 40)
    assert world.main([], **kw) == 0
    assert "# page refused (the page is refused: html: an external script, the live page stays)" in \
        capsys.readouterr().out.splitlines()
    assert (web / "index.html").read_bytes() == _page("new")


@not_as_root
def test_the_dry_run_says_what_the_page_step_would_do(world, tmp_path, capsys):
    """Planted: a dry run that publishes or says nothing of the page."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_bytes(_page("old"))
    world.runner.plan = world.plan()
    assert world.main(["--dry-run"], web_root=web) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(s.startswith("+ publish %s of the release as %s when its bytes differ from the live page"
                            % (deploy.PAGE, web / "index.html")) for s in out), out
    assert (web / "index.html").read_bytes() == _page("old") and sorted(p.name for p in web.iterdir()) == ["index.html"]
    assert world.main(["--dry-run"], web_root=tmp_path / "no-web") == 0
    assert "# page skipped (no %s on this host)" % (tmp_path / "no-web") in capsys.readouterr().out.splitlines()
