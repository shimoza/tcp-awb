"""Q-0026 and Q-0025 (section 5 of the UX review of 2026-11-19, W1 to W4): the health strip and the Now block of both
console pages (GET /api/health of the console data service) and the dev asks and the queue of the owner page (the
status file of the owner's timer, served in the answer of GET /api/owner/state). Temporary folders, a fake /proc,
invented codes and the fixture register only; no live service is reached."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import config, deploy, ownerwait, status_shape
from awb.status_shape import StatusError
from awb.tcp import owner
from awb.tcp.web import contract, gateway, nowhealth, owner_actions, projects_api, users
from tests import fixtures as fx
from tests.test_web_owner_api import OWNER, Site, owner_headers, status_doc

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "awb" / "tcp" / "web" / "portal.html"
OWNER_PAGE = REPO / "awb" / "tcp" / "web" / "owner.html"
LIVE = "1" * 40
MAIN = "2" * 7
NOW = dt.datetime(2026, 11, 20, 9, 30, 0, tzinfo=dt.timezone.utc)
CODE, OTHER = "tcp-q7m4", "tcp-k2wd"


def local(t: dt.datetime) -> str:
    """A stamp of state.tsv: host local time without a zone, as the build runner writes it."""
    return t.astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def write_state(paths, rows: list[str]) -> Path:
    tsv = paths.shared / "build" / "state.tsv"
    tsv.parent.mkdir(parents=True, exist_ok=True)
    tsv.write_text("# written by awb build serve\n" + "\n".join(rows) + "\n")
    return tsv


def opt_with(tmp_path: Path, journal: dict | None) -> Path:
    opt = tmp_path / "opt"
    (opt / "releases" / LIVE).mkdir(parents=True)
    (opt / "src").symlink_to("releases/" + LIVE)
    if journal is not None:
        (opt / deploy.JOURNAL_NAME).write_text(json.dumps(journal))
    return opt


def fake_proc(tmp_path: Path, boot: str, starts_us: list[int]) -> Path:
    """A /proc with the boot id, one gateway process per start and one other process."""
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random" / "boot_id").write_text(boot + "\n")
    tick = os.sysconf("SC_CLK_TCK")
    for n, start in enumerate(starts_us + [1], start=100):
        d = proc / str(n)
        d.mkdir()
        cmd = (b"/usr/bin/python3\0-I\0/opt/tcp-awb/src/awb/tcp/web/gateway.py\0--domain\0x\0"
               if n - 100 < len(starts_us) else b"/usr/bin/sleep\0100\0")
        (d / "cmdline").write_bytes(cmd)
        fields = ["S"] + ["0"] * 18 + [str(start * tick // 1000000)] + ["0"] * 10
        (d / "stat").write_text("%d (python3 x) %s\n" % (n, " ".join(fields)))
    return proc


class Statvfs(SimpleNamespace):
    pass


# --------------------------------------------------------------------------- W1: the tiles


def test_vault_and_key_tiles_say_the_state_and_no_answer_is_bad():
    assert nowhealth.vault_tile(lambda: {"state": "unlocked", "since": None})["state"] == "ok"
    locked = nowhealth.vault_tile(lambda: {"state": "locked", "since": "2026-11-14T06:02:11Z"})
    assert (locked["state"], locked["value"], locked["detail"]) == ("bad", "locked", "since 2026-11-14T06:02:11Z")

    def down():
        raise OSError("no socket")

    assert nowhealth.vault_tile(down)["value"] == "no answer" and nowhealth.vault_tile(down)["state"] == "bad"
    assert nowhealth.keys_tile(lambda: "unlocked")["state"] == "ok"
    assert nowhealth.keys_tile(lambda: "locked")["state"] == "bad"
    assert nowhealth.keys_tile(down)["value"] == "no answer"
    assert nowhealth.keys_tile(lambda: "odd")["state"] == "unknown"


def test_runner_tile_reads_held_hung_running_and_idle_from_state_tsv(home):
    from awb.tcp import board

    since = local(NOW - dt.timedelta(minutes=20))
    write_state(home, ["queue\theld\tvault locked", "task\tB-077\t1\trunning\t%s\t-\t-" % since])
    assert nowhealth.runner_tile(board.build_state(home), "x")["value"] == "held: vault locked"
    write_state(home, ["queue\tok\t-", "check\tC-301\thung: suite\t%s\t-" % since])
    assert nowhealth.runner_tile(board.build_state(home), None)["state"] == "bad"
    write_state(home, ["queue\tok\t-", "task\tB-077\t1\trunning\t%s\t-\t-" % since,
                       "check\tC-301\tpassed\t%s\t-" % since])
    tile = nowhealth.runner_tile(board.build_state(home), "2026-11-20T09:29:00Z")
    assert (tile["state"], tile["value"], tile["detail"]) == ("ok", "running B-077", "state of 2026-11-20T09:29:00Z")
    write_state(home, ["queue\tok\t-", "check\tC-302\tqueued\t%s\t-" % since])
    assert nowhealth.runner_tile(board.build_state(home), None)["value"] == "1 queued"
    assert nowhealth.runner_tile(None, None)["state"] == "unknown"


def test_release_and_disk_tiles():
    build = {"main": MAIN, "not_deployed": 2}
    tile = nowhealth.release_tile(LIVE, {"state": "done"}, build)
    assert (tile["state"], tile["value"], tile["detail"]) == ("warn", "1111111", "main 2222222, 2 not deployed")
    assert nowhealth.release_tile(LIVE, {"state": "running"}, build)["detail"] == "deploy running"
    assert nowhealth.release_tile(LIVE, {"state": "done"}, {"main": MAIN, "not_deployed": 0})["state"] == "ok"
    assert nowhealth.release_tile(None, None, None)["state"] == "unknown"

    def vfs(free):
        return lambda path: Statvfs(f_blocks=1000, f_frsize=1000000, f_bavail=free)

    assert nowhealth.disk_tile(statvfs=vfs(500))["state"] == "ok"
    assert nowhealth.disk_tile(statvfs=vfs(150))["state"] == "warn"
    low = nowhealth.disk_tile(statvfs=vfs(50))
    assert (low["state"], low["value"], low["detail"]) == ("bad", "5%", "0.1 GB of 1 GB")


def test_web_tile_compares_the_gateway_start_with_the_switch_of_the_last_deploy(tmp_path):
    flip = 5_000_000_000
    journal = {"state": "done", "boot": "boot-a", "flip": flip, "target": LIVE}
    after = fake_proc(tmp_path / "a", "boot-a", [flip + 30_000_000])
    assert nowhealth.web_tile(journal, LIVE, after)["value"] == "live release"
    before = fake_proc(tmp_path / "b", "boot-a", [flip - 60_000_000])
    tile = nowhealth.web_tile(journal, LIVE, before)
    assert (tile["state"], tile["value"]) == ("warn", "older release") and "1111111" in tile["detail"]
    # a deploy of an earlier boot: every process of this boot started after it
    assert nowhealth.web_tile(journal, LIVE, fake_proc(tmp_path / "c", "boot-b", [10]))["state"] == "ok"
    assert nowhealth.web_tile(journal, LIVE, fake_proc(tmp_path / "d", "boot-a", []))["value"] == "not running"
    assert nowhealth.web_tile(dict(journal, state="running", flip=None), LIVE, after)["value"] == "deploy running"
    assert nowhealth.web_tile(None, LIVE, after)["state"] == "unknown"


# --------------------------------------------------------------------------- W2: Now


def test_now_lists_open_build_work_lab_windows_by_end_and_a_pending_deploy(home):
    from awb.tcp import board

    started = NOW - dt.timedelta(minutes=12)
    write_state(home, ["queue\tok\t-", "task\tB-070\t1\tdone\t%s\t1234567\tsent" % local(started),
                       "task\tB-077\t1\tqueued\t%s\t-\t-" % local(started + dt.timedelta(minutes=5)),
                       "check\tC-300\tred: suite\t%s\t-" % local(started),
                       "check\tC-301\trunning\t%s\t-" % local(started), "live\t1111111", "main\t%s" % MAIN,
                       "not_deployed\t2"])
    notices = home.shared / "notices"
    notices.mkdir()
    (notices / ("winlab-lab-one.%s.json" % CODE)).write_text(json.dumps(
        {"machine": "lab-one", "project": CODE, "end": "2026-11-20T10:10:00Z"}))
    (notices / ("winlab-lab-two.%s.json" % OTHER)).write_text(json.dumps(
        {"machine": "lab-two", "project": OTHER, "end": "2026-11-20T15:00:00Z"}))
    (notices / ("winlab-lab-old.%s.json" % OTHER)).write_text(json.dumps(
        {"machine": "lab-old", "project": OTHER, "end": "2026-11-20T08:00:00Z"}))                # ended
    (notices / ("winlab-lab-bad.%s.json" % CODE)).write_text(json.dumps(
        {"machine": "lab-bad", "project": OTHER, "end": "2026-11-20T15:00:00Z"}))                # another code
    (notices / "input-20261120T090000Z-0000aaaa.json").write_text("{}")
    items = nowhealth.now_items(nowhealth.build_data(home), {"state": "done"}, LIVE, notices, NOW)
    # the board keeps its own data: its checks carry the clock time only, never the full start (test_parked)
    assert all("since" not in c for c in board.build_state(home)["checks"])
    assert [(i["kind"], i["id"], i["state"]) for i in items] == [
        ("check", "C-301", "running"), ("task", "B-077", "queued"), ("lab", CODE, "ends soon"),
        ("lab", OTHER, "open"), ("deploy", MAIN, "pending")]
    assert items[0]["start"] == started.strftime("%Y-%m-%dT%H:%M:%SZ") and items[0]["end"] is None
    assert items[2]["end"] == "2026-11-20T10:10:00Z" and items[2]["note"] == "window as opened"
    assert items[4]["note"] == "2 commits on main, live 1111111"
    text = json.dumps(items)
    assert "lab-one" not in text and "lab-two" not in text                   # codes only, no machine name
    running = nowhealth.deploy_items({"state": "running", "target": LIVE, "started": "2026-11-20T09:29:00Z"},
                                     None, LIVE)
    assert running == [{"kind": "deploy", "id": "1111111", "state": "running", "start": "2026-11-20T09:29:00Z",
                        "end": None, "note": None}]


def test_health_reads_files_and_pings_only_and_is_kept_fifteen_seconds(home, tmp_path):
    write_state(home, ["queue\tok\t-", "live\t1111111", "main\t1111111", "not_deployed\t0"])
    opt = opt_with(tmp_path, {"state": "done", "boot": "boot-a", "flip": 5_000_000_000})
    proc = fake_proc(tmp_path, "boot-a", [6_000_000_000])
    pings = []

    def vault_ping():
        pings.append("vault")
        return {"state": "locked", "since": "2026-11-20T06:00:00Z"}

    kw = dict(opt=opt, proc=proc, disk=tmp_path, now=NOW, vault_ping=vault_ping, keys_ping=lambda: "unlocked")
    answer = nowhealth.health(home, **kw)
    assert [t["id"] for t in answer["health"]] == ["vault", "keys", "runner", "release", "disk", "web"]
    assert [t["state"] for t in answer["health"]][:2] == ["bad", "ok"]
    assert answer["health"][3]["value"] == "1111111" and answer["health"][5]["value"] == "live release"
    assert answer["now"] == [] and answer["generated_at"] == "2026-11-20T09:30:00Z"
    clock = iter([100.0, 110.0, 116.0])
    nowhealth._CACHE.clear()
    first = nowhealth.cached_health(home, clock=lambda: next(clock), **kw)
    assert nowhealth.cached_health(home, clock=lambda: next(clock), **kw) is first and len(pings) == 2
    nowhealth.cached_health(home, clock=lambda: next(clock), **kw)
    assert len(pings) == 3
    nowhealth._CACHE.clear()
    fx.assert_no_fixture_name(json.dumps(answer))


def test_the_data_service_answers_health_while_the_vault_is_locked(home, monkeypatch):
    seen = []
    monkeypatch.setattr(nowhealth, "cached_health", lambda paths: seen.append(paths) or {"generated_at": "x",
                                                                                          "health": [], "now": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), projects_api.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % server.server_port, timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["health"] == []
        monkeypatch.setattr(nowhealth, "cached_health", lambda paths: 1 / 0)
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % server.server_port, timeout=5)
        assert err.value.code == 503
        assert "could not be read" in json.loads(err.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
    assert len(seen) == 1                       # no ping of the vault before: a locked vault is a tile, not a 503


# --------------------------------------------------------------------------- the gateway


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def test_health_goes_to_the_data_service_on_both_hosts_and_never_to_a_limited_reader(site):
    status, _, body = site.request("/api/health", headers={"Cookie": "__Host-awb-session=" + site.reader_session()})
    assert status == 200 and json.loads(body) == {"backend": "tcp", "path": "/api/health"}
    token = site.owner_session()
    status, _, body = site.request("/api/health", host=OWNER, headers=owner_headers(site, token))
    assert status == 200 and json.loads(body)["backend"] == "tcp"
    users.change(site.users, "projects", "reader1", False, projects=[CODE])
    before = len(site.tcp.calls)
    status, _, body = site.request("/api/health", headers={"Cookie": "__Host-awb-session=" + site.reader_session()})
    assert (status, body) == (404, b"Not found.\n") and len(site.tcp.calls) == before


def test_the_lists_come_with_the_owner_state_on_the_owner_host_only(site):
    assert owner_actions.ROUTES == gateway.OWNER_ACTIONS                       # no route of their own
    assert not any("dev-asks" in p or p.endswith("/queue") for p in owner_actions.ROUTES)
    site.status.write_text(json.dumps(status_doc(dev_asks=[], queue={"readable": False, "rows": []},
                                                 lists_built=1)))
    status, _, _ = site.request("/api/owner/state",
                                headers={"Cookie": "__Host-awb-session=" + site.reader_session()})
    assert status == 404                                       # /api/owner/ is not on the main host
    status, _, body = site.request("/api/owner/state", host=OWNER, headers=owner_headers(site, site.owner_session()))
    data = json.loads(body)
    assert status == 200 and data["dev_asks"] == [] and data["queue"] == {"readable": False, "rows": []}


# --------------------------------------------------------------------------- W3 and W4: the owner side


def ask_file(home, name: str, title: str, created: str, blocks: bool, state: str = "open") -> None:
    folder = home.shared / "notices" / "dev"
    folder.mkdir(parents=True, exist_ok=True)
    code = name[11:19]
    data = {"project": code, "title": title, "created": created, "blocks": blocks, "state": state}
    (folder / name).write_text("<!-- %s -->\n# %s\n\nProject: %s\n\n## Text\n\nsome text\n" % (
        json.dumps(data), title, code))


def test_dev_asks_carry_codes_and_screened_titles_oldest_first(home):
    ask_file(home, "2026-11-18-%s-1.md" % OTHER, "the import stops at 30 MB", "2026-11-18T08:00:00+00:00", True)
    ask_file(home, "2026-11-16-%s-1.md" % CODE, "contact %s about it" % fx.CUSTOMER_FORMS[0],
             "2026-11-16T08:00:00+00:00", False)
    ask_file(home, "2026-11-15-%s-2.md" % CODE, "answered already", "2026-11-15T08:00:00+00:00", False, "closed")
    (home.shared / "notices" / "dev" / "notes.md").write_text("not an ask")
    rows = nowhealth.ask_rows(home, owner.summary_screen())
    assert [(r["code"], r["blocks"]) for r in rows] == [(CODE, False), (OTHER, True)]
    assert rows[0]["title"] is None and rows[0]["withheld"] == "name"
    assert rows[1] == {"code": OTHER, "file": "2026-11-18-%s-1.md" % OTHER, "title": "the import stops at 30 MB",
                       "withheld": None, "created": "2026-11-18T08:00:00Z", "blocks": True}
    status_shape.validate_lists({"dev_asks": rows})
    fx.assert_no_fixture_name(json.dumps(rows))


def write_queue(path: Path, rows: list[dict]) -> None:
    from awb import workqueue

    lines = ["\t".join(workqueue.FIELDS)] + ["\t".join(r[k] for k in workqueue.FIELDS) for r in rows]
    path.write_text("\n".join(lines) + "\n")


def queue_row(ident, what, nxt, who="owner", project="", changed="2026-11-20T08:00:00+00:00"):
    return {"id": ident, "created": "2026-11-18T08:00:00+00:00", "updated": changed, "next_changed": changed,
            "what": what, "next": nxt, "who": who, "project": project, "done": ""}


def test_queue_rows_carry_the_stale_mark_and_withhold_a_text_with_a_finding(home, tmp_path, monkeypatch):
    queue = tmp_path / "work-profile" / "queue.tsv"
    queue.parent.mkdir()
    monkeypatch.setenv("AWB_WORK_QUEUE", str(queue))
    write_queue(queue, [queue_row("Q-0002", "second", "do it", who="session:%s" % CODE, project=CODE),
                        queue_row("Q-0001", "first " + "x" * 400, "review", who="development",
                                  changed="2026-11-18T08:00:00+00:00")])

    def screen(text):
        return (None, "secret") if "do it" in text else (text, None)

    got = nowhealth.queue_rows(screen, now=NOW)
    assert got["readable"] is True
    first, second = got["rows"]
    assert first["id"] == "Q-0001" and first["stale"] is True and len(first["what"]) == nowhealth.TEXT_CUT
    assert first["what"].endswith("...") and first["project"] is None
    assert (second["what"], second["next"], second["withheld"], second["stale"]) == (None, None, "secret", False)
    assert second["who"] == "session:%s" % CODE and second["project"] == CODE
    status_shape.validate_lists({"queue": got})
    # a row with a registered name never leaves the queue: awb queue refuses it, the page says it cannot be read
    write_queue(queue, [queue_row("Q-0003", "call %s" % fx.CUSTOMER_FORMS[0], "x")])
    assert nowhealth.queue_rows(screen, now=NOW) == {"readable": False, "rows": []}


def test_one_screen_call_for_all_texts_and_one_each_only_after_a_finding():
    calls = []

    def screen(text):
        calls.append(text)
        return (None, "name") if "bad" in text else (text, None)

    assert nowhealth.screen_all(["a", "b"], screen) == [("a", None), ("b", None)] and len(calls) == 1
    calls.clear()
    assert nowhealth.screen_all(["a", "bad"], screen) == [("a", None), (None, "name")] and len(calls) == 3
    assert nowhealth.screen_all(["a"], lambda t: (None, "unavailable")) == [(None, "unavailable")]
    assert nowhealth.screen_all([], screen) == []


def test_the_status_carries_the_lists_through_the_shape_and_owner_actions_serves_them(home, tmp_path, monkeypatch):
    ask_file(home, "2026-11-18-%s-1.md" % OTHER, "the import stops", "2026-11-18T08:00:00+00:00", True)
    queue = tmp_path / "q" / "queue.tsv"
    queue.parent.mkdir()
    monkeypatch.setenv("AWB_WORK_QUEUE", str(queue))
    write_queue(queue, [queue_row("Q-0001", "first", "review")])
    for name, value in (("daemons", {"vault": status_doc()["vault"], "keys": status_doc()["keys"]}),
                        ("deployed", status_doc()["deployed"]), ("page", status_doc()["page"]), ("ui_runs", [])):
        monkeypatch.setattr(owner, name, lambda *a, _value=value, **k: _value)
    monkeypatch.setattr(ownerwait, "page_items", lambda p: [])
    data = owner.collect(home)
    assert [a["code"] for a in data["dev_asks"]] == [OTHER] and data["queue"]["rows"][0]["id"] == "Q-0001"
    path = tmp_path / "status.json"
    owner.write(data, path, checker=lambda text: "passed")
    assert owner_actions.read_status(path) == data
    code, state = owner_actions.answer("/api/owner/state", data, time.time())
    assert code == 200 and state["dev_asks"] == data["dev_asks"] and state["stale"] is False
    assert state["queue"] == data["queue"] and state["queue"]["readable"] is True
    # an old status file without the lists: empty lists
    old = owner_actions.answer("/api/owner/state", status_doc(), time.time())[1]
    assert old["dev_asks"] == [] and old["queue"] == {"readable": True, "rows": []}
    # the name check of the whole status cannot run: every text of the lists is withheld as unavailable
    owner.write(data, path, checker=lambda text: "unavailable")
    held = owner_actions.read_status(path)
    assert held["dev_asks"][0]["title"] is None and held["dev_asks"][0]["withheld"] == "unavailable"
    assert held["queue"]["rows"][0]["what"] is None and held["lists_built"] == 0
    # within five minutes the timer reuses the lists of its own file and builds no new ones
    owner.write(data, path, checker=lambda text: "passed")
    monkeypatch.setattr(nowhealth, "ask_rows", lambda *a: pytest.fail("rebuilt"))
    asks2, queue2, built = owner._lists(home, path)
    assert asks2 == data["dev_asks"] and built == data["lists_built"]


@pytest.mark.parametrize("change", [
    lambda s: s["dev_asks"][0].update(code="TCP-1"),
    lambda s: s["dev_asks"][0].update(file="../x.md"),
    lambda s: s["dev_asks"][0].update(title="a\nb"),
    lambda s: s["dev_asks"][0].update(withheld="name"),
    lambda s: s["queue"].update(readable="yes"),
    lambda s: s["queue"]["rows"][0].update(who="someone"),
    lambda s: s["queue"]["rows"][0].update(extra=1),
    lambda s: s["queue"]["rows"].append(dict(s["queue"]["rows"][0])),
    lambda s: s.update(lists_built=-1),
])
def test_a_list_of_another_shape_is_refused_on_write_and_read(change, tmp_path):
    good = status_doc(dev_asks=[{"code": CODE, "file": "2026-11-18-%s-1.md" % CODE, "title": "t", "withheld": None,
                                 "created": "2026-11-18T08:00:00Z", "blocks": False}],
                      queue={"readable": True, "rows": [{
                          "id": "Q-0001", "who": "owner", "project": None, "what": "w", "next": "n",
                          "withheld": None, "created": "2026-11-18T08:00:00Z",
                          "next_changed": "2026-11-18T08:00:00Z", "stale": False}]}, lists_built=1)
    ownerwait.validate_status(good)
    bad = json.loads(json.dumps(good))
    change(bad)
    with pytest.raises(StatusError):
        ownerwait.validate_status(bad)
    path = tmp_path / "status.json"
    path.write_text(json.dumps(bad))
    assert owner_actions.read_status(path) is None


# --------------------------------------------------------------------------- the pages and the contract


def test_the_contract_carries_health_and_the_two_owner_lists():
    doc = contract.build()
    ops = {(m, p): op for m, p, op in contract.operations(doc)}
    assert ("get", "/api/health") in ops and ops[("get", "/api/health")].get("security") != [{"owner": []}]
    assert ("get", "/api/owner/dev-asks") not in ops and ("get", "/api/owner/queue") not in ops
    state = doc["components"]["schemas"]["OwnerState"]
    assert {"dev_asks", "queue"} <= set(state["properties"]) and "dev_asks" not in state["required"]
    assert contract.check(doc) == []
    assert (REPO / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8") == contract.to_yaml(doc)
    tiles = contract.HEALTH_ANSWER["health"]
    assert [t["id"] for t in tiles] == ["vault", "keys", "runner", "release", "disk", "web"]


def test_the_main_page_draws_the_strip_and_now_from_health():
    page = PORTAL.read_text(encoding="utf-8")
    assert "readAPI('/api/health'" in page and 'id="overview-health"' in page
    assert "function healthBlock" in page and "function nowRow" in page
    assert "Nothing runs now" in page
    # a login limited to some projects gets 404: the strip is left out, never an error box
    assert "if(e.status===404)healthLive.hidden=true" in page
    # tokens only in the new styles
    css = page[page.index("/* Health strip and Now"):page.index("</style>")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    fx.assert_no_fixture_name(page)


OWNER_HARNESS = r'''
const assert = require("assert");
const vm = require("vm");
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.disabled=false; this.value="";
                     this.textContent=""; this.className=""; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c),1); }
  get firstChild() { return this.children[0]; }
  setAttribute(k,v) { this[k]=v; }
  addEventListener(k,f) { (this.listeners[k] ||= []).push(f); }
}
const nodes={};
for (const id of INPUT.html.matchAll(/id="([^"]+)"/g)) nodes[id[1]]=new Element("div");
const document={getElementById:id=>nodes[id],createElement:tag=>new Element(tag)};
function response(data) {return {status:200,ok:true,headers:{get:()=>"application/json"},json:()=>Promise.resolve(data)};}
function fetch(path) {
  const data = INPUT.answers[path] || (path.endsWith("customers")||path.endsWith("intake") ? {customers:[]} :
               path.endsWith("ui-runs") ? {runs:[]} : path.endsWith("items") ? {items:[]} :
               path.endsWith("tenants") ? {tenants:[]} : path.endsWith("options") ? {kinds:[],tags:[]} :
               path.endsWith("projects") ? {projects:[]} : {});
  return Promise.resolve(response(data));
}
vm.runInNewContext(INPUT.script,{document,fetch,location:{},setInterval:()=>0,Date});
const settle=()=>new Promise(resolve=>setTimeout(resolve,20));
function text(node) {return node.textContent+" "+node.children.map(text).join(" ");}
function cls(node) {return node.className+" "+node.children.map(cls).join(" ");}
(async()=>{
  await settle();
  const out={health:text(nodes.health),healthCls:cls(nodes.health),now:text(nodes.now),asks:text(nodes.asks),
             asksCls:cls(nodes.asks),queue:text(nodes.queue),queueCls:cls(nodes.queue),nowRows:nodes.now.children.length,
             askRows:nodes.asks.children.length,queueRows:nodes.queue.children.length};
  console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def run_owner_page(answers: dict) -> dict:
    html = OWNER_PAGE.read_text()
    script = re.search(r"<script>(.*?)</script>", html, re.S)[1]
    result = subprocess.run(["node", "-e", "const INPUT=" + json.dumps({"html": html, "script": script,
                                                                         "answers": answers}) + ";\n" + OWNER_HARNESS],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_owner_page_shows_health_now_dev_asks_and_the_queue():
    old = (NOW - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    answers = {
        "/api/owner/state": {"csrf": "c", "vault": {"state": "unlocked"}, "keys": {"state": "unlocked"},
                             "written": old, "stale": False, "dev_asks": [
            {"code": CODE, "file": "2026-11-16-%s-1.md" % CODE, "title": "the import stops", "withheld": None,
             "created": old, "blocks": True},
            {"code": OTHER, "file": "2026-11-17-%s-1.md" % OTHER, "title": None, "withheld": "name",
             "created": old, "blocks": False}],
                             "queue": {"rows": [
            {"id": "Q-0025", "who": "development", "project": None, "what": "dev asks page", "next": "review",
             "withheld": None, "created": old, "next_changed": old, "stale": True}], "readable": True}},
        "/api/health": contract.HEALTH_ANSWER,
    }
    out = run_owner_page(answers)
    assert "Key service" in out["health"] and "locked" in out["health"] and "t-bad" in out["healthCls"]
    assert "t-warn" in out["healthCls"] and "t-ok" in out["healthCls"]
    assert out["nowRows"] == 4 and "C-301" in out["now"] and "until" in out["now"] and "since" in out["now"]
    assert out["askRows"] == 2 and "the import stops" in out["asks"] and "withheld by the name check (name)" in \
        out["asks"] and "blocks" in out["asks"] and "mark bad" in out["asksCls"]
    assert out["queueRows"] == 1 and "dev asks page · next: review" in out["queue"] and "stale" in out["queue"]
    assert "mark warn" in out["queueCls"]
    empty = run_owner_page({"/api/owner/state": dict(answers["/api/owner/state"], dev_asks=[],
                                                     queue={"rows": [], "readable": False}),
                            "/api/health": dict(contract.HEALTH_ANSWER, now=[])})
    assert "Nothing runs now" in empty["now"] and "No open ask." in empty["asks"]
    assert "Not available." in empty["queue"]


def test_the_owner_page_keeps_its_rules_and_marks_where_the_buttons_go():
    page = OWNER_PAGE.read_text()
    for word in ("awb vault", "awb keys", "sudo", "awb intake", "awb register", "passphrase", "innerHTML",
                 "http://", "https://"):
        assert word not in page, word
    assert "the close button of an ask and the archive button of a queue row" in page
    fx.assert_no_fixture_name(page)
