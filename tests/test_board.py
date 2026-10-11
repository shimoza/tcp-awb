"""The board (awb/tcp/board.py, GET /api/board): the status of every active project for management, codes only.

2026-10-06: the owner asked for a run on demand that walks every project and gives management one status update,
in the portal and as a file; a lagging or missing status is marked, never written by the board.
"""
from __future__ import annotations

import copy
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from awb import cli
from awb.tcp import board
from awb.tcp.web import contract
from awb.tcp.web import projects_api as api

STATE = ("# State of tcp-q7m4\n\nStatus: The appliance image is imported.\nNext: The boot test.\n\n## Now\n\n- work\n\n"
         "## Waiting on\n\n- The ticket to the operator.\n- The trial licence.\n\n## Decisions\n\n- none\n")


def git(root: Path, *args: str, when: int) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.org", GIT_AUTHOR_DATE="@%d +0000" % when,
               GIT_COMMITTER_DATE="@%d +0000" % when)
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def fixture():
    from tests.test_web_projects_api import Tests

    t = Tests()
    t.setUp()
    (t.folder / "STATE.md").write_text(STATE, encoding="utf-8")
    os.utime(t.folder / "STATE.md", (1_790_000_000, 1_790_000_000))
    git(t.folder, "init", "-q", when=1_790_000_000)
    git(t.folder, "add", "-A", when=1_790_000_000)
    git(t.folder, "commit", "-q", "-m", "start", when=1_790_000_000)
    (t.folder / "notes.md").write_text("a finding\n", encoding="utf-8")
    git(t.folder, "add", "-A", when=1_790_000_100)
    git(t.folder, "commit", "-q", "-m", "a finding", when=1_790_000_100)
    return t


def test_the_board_carries_status_waiting_on_open_items_and_lag():
    t = fixture()
    try:
        data = api.board(t.paths)
        p = data["projects"][0]
        assert p["status"]["summary"] == "The appliance image is imported." and p["status"]["behind"] == 1
        assert p["waiting_on"] == {"items": ["The ticket to the operator.", "The trial licence."], "total": 2}
        assert p["open"] == {"items": ["One question", "Another question"], "total": 2}
        assert p["deliverables"] == {"status": "available", "counts": {}}
        assert data["summary"] == {"projects": 1, "current": 0, "lagging": 1, "without_status": 0}
        assert contract.validate(contract.build(), data, contract.ref("Board")) == []
    finally:
        t.tearDown()


def test_a_closed_project_is_not_on_the_board_and_a_refused_check_withholds_every_item():
    t = fixture()
    try:
        with patch.object(api, "checked", side_effect=lambda text, paths: (api.WITHHELD, True)):
            p = api.board(t.paths)["projects"][0]
        assert p["waiting_on"]["items"] == [api.WITHHELD] * 2 and p["open"]["items"] == [api.WITHHELD] * 2
        assert p["board_redacted"] is True
        t.row.state = "closed"
        assert api.board(t.paths)["projects"] == []
    finally:
        t.tearDown()


def test_the_file_marks_a_lagging_status_and_escapes_every_text():
    data = copy.deepcopy(contract.P_BOARD)
    data["projects"][1]["open"]["items"][0] = "<b>bold</b>"
    data["projects"][1]["status"]["summary"] = "image in | boot open"
    md, page = board.markdown(data), board.page(data)
    assert "# Project status, 06.10.2026 14:00 CEST" in md
    assert "3 change(s) since: may be out of date" in md and "image in \\| boot open" in md
    assert "&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page
    assert '<span class="late">' in page and "@page{size:A4" in page
    assert "tcp-m3fx: Test a virtual firewall appliance on a test tenant" in md


def test_the_command_writes_the_markdown_and_the_html(tmp_path, capsys):
    with patch.object(api, "board", return_value=copy.deepcopy(contract.P_BOARD)):
        assert cli.main(["board", "write", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "2 active project(s), 1 current, 1 lagging, 0 without a status line" in out
    assert sorted(f.name for f in tmp_path.iterdir()) == ["board-2026-10-06-1200.html", "board-2026-10-06-1200.md"]
    with patch.object(api, "board", return_value=copy.deepcopy(contract.P_BOARD)):
        assert cli.main(["board", "show"]) == 0
    assert capsys.readouterr().out.startswith("# Project status, 06.10.2026 14:00 CEST")
    assert cli.main(["board"]) == 2


def test_the_gateway_sends_the_board_to_the_projects_service():
    from tests.test_web_gateway import DOMAIN, Backend, LoginTests
    from awb.tcp.web import gateway

    class Marked(Backend):
        def answer(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.path.encode())

    LoginTests.setUpClass()
    t = LoginTests()
    backend = ThreadingHTTPServer(("127.0.0.1", 0), Marked)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    s = gateway.make_server(0, t.auth, t.root / "index.html", 1, 1, backend.server_port, 1, domain=DOMAIN)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        assert t.request("/api/board", headers=t.session_headers(), server=s)[2] == b"/api/board"
        assert t.request("/api/board")[0] in (302, 303, 401)          # no session, no board
    finally:
        for x in (s, backend):
            x.shutdown()
            x.server_close()
        LoginTests.tearDownClass()


def test_board_and_health_open_with_locked_since(home, monkeypatch, capsys):
    """T3. Planted failure: a board, a health answer or a board API answer without the line while the stand-in
    daemon answers locked; a line on them while it answers unlocked."""
    import http.client
    import json

    from awb import config
    from awb.tcp import portal
    from tests.test_hooks import SINCE, locked_answers, stand_in

    def health() -> str:
        server = portal.make_server(0, config.paths())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection(*server.server_address, timeout=20)
            c.request("GET", "/health")
            return c.getresponse().read().decode()
        finally:
            server.shutdown()
            server.server_close()

    def board_api() -> tuple[int, dict]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection(*server.server_address, timeout=20)
            c.request("GET", "/api/board")
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            server.shutdown()
            server.server_close()

    with stand_in(locked_answers(SINCE)) as (sock, _):
        monkeypatch.setenv("AWB_CHECK_SOCKET", str(sock))
        for command in ("show", "write"):
            cli.main(["board", command, "--out", str(home.shared / "board")] if command == "write" else
                     ["board", command])
            assert capsys.readouterr().out.splitlines()[0] == "locked since %s" % SINCE
        assert health() == "locked since %s\nok\n" % SINCE
        status, answer = board_api()
        assert status == 503 and answer["error"].startswith("locked since %s. The data check is locked." % SINCE)
        capsys.readouterr()          # the log line of the board service
    with stand_in({"ping": {"ok": True, "state": "unlocked"}, "check": {"ok": True, "hits": []}}) as (sock, _):
        monkeypatch.setenv("AWB_CHECK_SOCKET", str(sock))
        with patch.object(api, "board", return_value=copy.deepcopy(contract.P_BOARD)):
            assert cli.main(["board", "show"]) == 0
        assert capsys.readouterr().out.startswith("# Project status")
        assert health() == "ok\n"


# --------------------------------------------------------------------------- the build line (self-build SB4)

TSV = ("# written by awb build serve: ids, states, hashes and times; the board and the portal read it\n"
       "queue\theld\tvault locked\n"
       "task\tB-001\t1\tdone\t2026-10-11T11:40:00\t1a2b3c4\tsent\n"
       "task\tB-002\t1\tasked\t2026-10-11T12:05:00\t-\tsent\n"
       "task\tB-003\t2\trunning\t2026-10-11T14:10:00\t-\t-\n"
       "task\tB-004\t1\tred: suite\t2026-10-11T13:00:00\t-\tnot_sent\n"
       "live\teb928d2\n"
       "main\t1a2b3c4\n"
       "not_deployed\t1\n"
       "daemons\tvault\n")


def shared(tmp_path, text: str | None):
    import types

    if text is not None:
        (tmp_path / "build").mkdir(exist_ok=True)
        (tmp_path / "build" / "state.tsv").write_text(text, encoding="utf-8")
    return types.SimpleNamespace(shared=tmp_path)


def test_the_build_line_from_a_planted_state_tsv(tmp_path):
    """SB4: the board reads <shared>/build/state.tsv and prints one build line. Planted: a task, the held queue, the
    hashes, the count, the daemon behind or the unsent mail missing from the line; the order not by time."""
    assert board.build_line(shared(tmp_path, TSV)) == (
        "build: queue held: vault locked, B-001 done 11:40, B-002 asked, B-004 red: suite, B-003 running since 14:10; "
        "live eb928d2, main 1a2b3c4, 1 not deployed; daemon code behind: vault; mail not sent: B-004")
    many = "".join("task\tB-%03d\t1\tdone\t2026-10-11T0%d:00:00\tabcdef%d\tsent\n" % (i, i, i) for i in range(1, 6))
    assert board.build_line(shared(tmp_path, many + "not_deployed\t0\n")) == (
        "build: B-003 done 03:00, B-004 done 04:00, B-005 done 05:00, 2 more done; live unknown, main unknown, "
        "0 not deployed")


def test_the_build_line_without_the_file(tmp_path):
    """SB4: no state.tsv (the runner never ran), a folder at its place, a link at its place: one fixed line."""
    assert board.build_line(shared(tmp_path, None)) == "build: no runner state yet"
    (tmp_path / "build" / "state.tsv").mkdir(parents=True)
    assert board.build_line(shared(tmp_path, None)) == "build: no runner state yet"
    (tmp_path / "build" / "state.tsv").rmdir()
    (tmp_path / "elsewhere.tsv").write_text(TSV)
    (tmp_path / "build" / "state.tsv").symlink_to(tmp_path / "elsewhere.tsv")
    assert board.build_line(shared(tmp_path, None)) == "build: no runner state yet"


def test_a_planted_free_text_does_not_reach_the_build_line(tmp_path):
    """SB4: every cell is matched before use. Planted: a free reason after a known word, an unknown word, a name in
    the id, the hash, the time and the queue reason, an extra column, a control character."""
    text = ("queue\theld\tcall the zyxwo team now\n"
            "task\tB-001\t1\tred: ask zyxwo about it\t2026-10-11T11:40:00\t1a2b3c4\tsent\n"
            "task\tB-002\t1\tstopped: zyxwo\t2026-10-11T11:41:00\t-\tsent\n"
            "task\tB-003\t1\tdeployed by zyxwo\t2026-10-11T11:42:00\t-\tsent\n"
            "task\tZyxwo-1\t1\tdone\t2026-10-11T11:43:00\t-\tsent\n"
            "task\tB-004\t1\tdone\tzyxwo\tzyxwo12\tsent\n"
            "task\tB-005\t1\tdone\t2026-10-11T11:44:00\t-\tsent\textra zyxwo\n"
            "task\tB-006\t1\tasked\x1b[2J\t2026-10-11T11:45:00\t-\tsent\n"
            "live\tzyxwo\nmain\t1a2b3c4 zyxwo\nnot_deployed\tzyxwo\ndaemons\tzyxwo,vault\n")
    line = board.build_line(shared(tmp_path, text))
    assert "zyxwo" not in line.lower() and "\x1b" not in line
    assert line == ("build: queue held, B-004 done unknown, B-001 red, B-002 stopped; live unknown, main unknown, "
                    "unknown not deployed; daemon code behind: vault")


def test_the_vocabulary_of_the_line_covers_every_state_the_runner_writes():
    """SB4: the board cuts an unknown reason to its word, so each fixed phrase of the runner must be known here.
    Planted: a new state literal in awb/buildrun.py or a gate class the board does not know."""
    import re as _re
    from pathlib import Path

    from awb import build, gate

    source = (Path(board.__file__).parents[1] / "buildrun.py").read_text(encoding="utf-8")
    literals = set(_re.findall(r'"((?:red|held|stopped|hung): [a-z_. -]+)"', source))
    literals |= {"red: gate %s" % c for c in gate.CLASSES}
    assert literals and all(board.build_state_word(s) == s for s in literals if not s.endswith(" "))
    assert set(board.BUILD_WORDS) == set(build.STATES)
    assert board._ID.pattern == build.ID_RE.pattern
    held = set(_re.findall(r'return "([a-z][a-z -]+)"\n', source.split("def preconditions", 1)[1].split(
        "\n    def ", 1)[0]))
    assert held and held <= set(board.QUEUE_HELD)


def test_awb_board_and_the_api_carry_the_build_line(home, monkeypatch, capsys):
    """SB4: awb board show and GET /api/board print the line. Planted: the line on one of them only."""
    (home.shared / "build").mkdir(parents=True, exist_ok=True)
    (home.shared / "build" / "state.tsv").write_text(TSV, encoding="utf-8")
    want = board.build_line(home)
    assert want.startswith("build: queue held: vault locked")
    with patch.object(api, "board", return_value=copy.deepcopy(contract.P_BOARD)):
        assert cli.main(["board", "show"]) == 0
    assert want in capsys.readouterr().out
    assert api.board(home)["build"] == want
    assert want in board.page(dict(copy.deepcopy(contract.P_BOARD), build=want))
