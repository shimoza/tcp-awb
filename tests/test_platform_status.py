"""The platform status of the board (B-042, awb/tcp/platformstatus.py and awb/tcp/board.py).

2026-11-07: the owner asked the board to show what is wrong on the platform now and what is planned. The public status
page answers without a key (KB-GI4M); every answer here is a stand-in, no test reaches the real host.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import socket
import types
from unittest.mock import patch

import pytest

from awb import cli
from awb.tcp import board, platformstatus as ps
from awb.tcp.web import contract
from awb.tcp.web import projects_api as api

NOW = dt.datetime(2026, 11, 7, 10, 0, 0, tzinfo=dt.timezone.utc)
COMPONENTS = [
    {"id": 1, "name": "a service", "attributes": [{"name": "type", "value": "ecs"}, {"name": "category", "value": "Compute"},
                                                   {"name": "region", "value": "EU-DE"}]},
    {"id": 2, "name": "another service", "attributes": [{"name": "region", "value": "Global"}]},
    {"id": 3, "name": "a third service", "attributes": [{"name": "region", "value": "EU-NL"}]},
    {"id": 4, "name": "a fourth service", "attributes": [{"name": "region", "value": "Mars"}]},
]


def event(i, kind, status, start, end=None, impact=1, title="a title", comps=(1,)):
    return {"id": i, "title": title, "description": "text", "type": kind, "status": status, "impact": impact,
            "start_date": start, "end_date": end, "components": list(comps), "updates": []}


def answers(events, components=COMPONENTS):
    """A stand-in for the status page: one answer by path."""
    calls = []

    def getter(host, path, timeout, cap):
        calls.append((host, path, timeout, cap))
        data = {ps.EVENTS_PATH: {"data": events}, ps.COMPONENTS_PATH: components}[path]
        return 200, json.dumps(data).encode()
    getter.calls = calls
    return getter


def failing(word):
    def getter(host, path, timeout, cap):
        if word == "timeout":
            raise ps.PullError("timeout")
        return (500, b"") if word == "http" else (200, b"<html>not json")
    return getter


def shared(tmp_path):
    return types.SimpleNamespace(shared=tmp_path)


def state(tmp_path):
    return (tmp_path / "platform-status" / "state.tsv").read_text(encoding="utf-8")


def put_state(tmp_path, text):
    (tmp_path / "platform-status").mkdir(parents=True, exist_ok=True)
    (tmp_path / "platform-status" / "state.tsv").write_text(text, encoding="utf-8")
    return shared(tmp_path)


GOOD = [
    event(1, "incident", "analysing", "2026-11-07T08:00:00", impact=2, title="object store slow", comps=(1, 2)),
    event(2, "maintenance", "planned", "2026-11-10T22:00:00Z", "2026-11-11T02:00:00Z", title="network work", comps=(3,)),
    event(3, "maintenance", "planned", "2026-11-27T22:00:00Z", "2026-11-28T02:00:00Z", title="too far away"),
    event(4, "incident", "resolved", "2026-11-05T08:00:00Z", "2026-11-05T09:00:00Z", title="old incident"),
]
HEADER = "pull\t2026-11-07T10:00:00Z\tok\t2026-11-07T10:00:00Z\n"


def test_ps_the_pull_keeps_an_open_incident_and_a_maintenance_in_three_days_and_leaves_the_others(tmp_path):
    getter = answers(GOOD)
    assert ps.pull(shared(tmp_path), getter=getter, now=NOW) == ("ok", 2, 0)
    assert state(tmp_path) == (
        HEADER
        + "event\tincident\tanalysing\t2\t2026-11-07T08:00:00Z\t\tEU-DE,Global\tobject store slow\t1\n"
        + "event\tmaintenance\tplanned\t1\t2026-11-10T22:00:00Z\t2026-11-11T02:00:00Z\tEU-NL\tnetwork work\t2\n")
    assert [p.name for p in (tmp_path / "platform-status").iterdir()] == ["state.tsv"]


def test_ps_an_incident_is_open_by_its_end_and_its_status_alone(tmp_path):
    events = [
        event(5, "incident", "resolved", "2026-11-07T07:00:00Z"),                          # resolved, no end
        event(6, "incident", "completed", "2026-11-07T07:00:00Z"),                         # completed, no end
        event(7, "incident", "fixing", "2026-11-07T07:00:00Z", "2026-11-07T09:00:00Z"),    # not resolved, ended
        event(8, "incident", "fixing", "2026-11-07T07:00:00Z", "2026-11-07T12:00:00Z"),    # end in the future
        event(9, "incident", "observing", "2026-11-07T07:00:00Z"),                         # no end
        event(10, "maintenance", "planned", "2026-11-21T10:00:00Z"),                       # exactly 14 days
        event(11, "maintenance", "planned", "2026-11-21T10:00:01Z"),                       # a second later
        event(12, "maintenance", "planned", "2026-11-07T09:59:59Z"),                       # started already
        event(13, "maintenance", "cancelled", "2026-11-09T09:00:00Z"),                     # called off
    ]
    ps.pull(shared(tmp_path), getter=answers(events), now=NOW)
    assert [r["id"] for r in board.platform_state(shared(tmp_path))["rows"]] == [8, 9, 10]


def test_ps_the_pull_asks_the_fixed_host_for_the_two_paths_within_its_limits(tmp_path):
    getter = answers(GOOD)
    ps.pull(shared(tmp_path), getter=getter, now=NOW)
    assert getter.calls == [("status.otc-service.com", "/v2/events", 20.0, 2 << 20),
                            ("status.otc-service.com", "/v2/components", 20.0, 2 << 20)]


class FakeConnection:
    made = []
    body = b"{}"

    def __init__(self, host, port=None, timeout=None, context=None):
        self.args, self.sock, self.raises = (host, port, timeout), None, None
        FakeConnection.made.append(self)

    def request(self, method, path, headers=None):
        if self.raises:
            raise self.raises
        self.sent = (method, path, dict(headers or {}))

    def getresponse(self):
        return types.SimpleNamespace(status=200, read=io.BytesIO(self.body).read)

    def close(self):
        pass


def test_ps_the_real_getter_is_https_on_443_with_a_timeout_a_cap_and_no_key():
    FakeConnection.made.clear()
    with patch("http.client.HTTPSConnection", FakeConnection):
        assert ps.https_get(ps.HOST, "/v2/events") == (200, b"{}")
    conn = FakeConnection.made[0]
    assert conn.args == ("status.otc-service.com", 443, 20.0)
    method, path, headers = conn.sent
    assert (method, path) == ("GET", "/v2/events")
    assert not {k.lower() for k in headers} & {"authorization", "cookie", "x-auth-token"}


def test_ps_the_real_getter_refuses_more_than_2_mb_and_names_a_slow_answer_a_timeout():
    class Big(FakeConnection):
        body = b"x" * ((2 << 20) + 1)

    class Slow(FakeConnection):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.raises = socket.timeout()
    with patch("http.client.HTTPSConnection", Big), pytest.raises(ps.PullError) as big:
        ps.https_get(ps.HOST, "/v2/events")
    assert big.value.word == "http"
    exact = b"x" * (2 << 20)
    with patch("http.client.HTTPSConnection", type("Exact", (FakeConnection,), {"body": exact})):
        assert len(ps.https_get(ps.HOST, "/v2/events")[1]) == 2 << 20
    with patch("http.client.HTTPSConnection", Slow), pytest.raises(ps.PullError) as slow:
        ps.https_get(ps.HOST, "/v2/events")
    assert slow.value.word == "timeout"


def test_ps_a_cell_outside_its_form_is_dropped_on_write(tmp_path):
    events = [
        event(21, "incident", "fixing", "2026-11-07T08:00:00Z", title="a|b"),                    # the title cell goes
        event(22, "incident", "burning", "2026-11-07T08:00:00Z"),                                # unknown status
        event(23, "incident", "fixing", "2026-11-07T08:00:00Z", comps=(1, 4)),                   # Mars leaves the list
        event(24, "outage", "fixing", "2026-11-07T08:00:00Z"),                                   # unknown type
        event(25, "incident", "fixing", "2026-11-07T08:00:00Z", impact=7),                       # impact above 3
        event(26, "incident", "fixing", "yesterday"),                                            # no date
        event(27, "incident", "fixing", "2026-11-07T08:00:00Z", "someday"),                      # no end date
        event(28, "incident", "fixing", "2026-11-07T08:00:00Z", title="a\u202eb"),            # not printable
        event(29, "incident", "fixing", "2026-11-07T08:00:00Z", title="x" * 150),                # cut, not dropped
    ]
    result, written, dropped = ps.pull(shared(tmp_path), getter=answers(events), now=NOW)
    assert (result, written, dropped) == ("ok", 4, 5)
    rows = {r["id"]: r for r in board.platform_state(shared(tmp_path))["rows"]}
    assert sorted(rows) == [21, 23, 28, 29]
    assert rows[21]["title"] == "" and rows[28]["title"] == "" and rows[29]["title"] == "x" * 100
    assert rows[23]["regions"] == ["EU-DE"]
    assert "|" not in state(tmp_path) and "\u202e" not in state(tmp_path)
    assert all(line.count("\t") == 8 for line in state(tmp_path).splitlines()[1:])


def test_ps_a_cell_outside_its_form_is_dropped_on_read(tmp_path):
    rows = ("event\tincident\tfixing\t1\t2026-11-07T08:00:00Z\t\tEU-DE\tgood title\t31\n"
            "event\tincident\tfixing\t1\t2026-11-07T08:00:00Z\t\tEU-DE\ta|b\t32\n"             # the title cell goes
            "event\tincident\tburning\t1\t2026-11-07T08:00:00Z\t\tEU-DE\ta title\t33\n"        # unknown status
            "event\tincident\tfixing\t1\t2026-11-07T08:00:00Z\t\tMars,EU-NL,EU-CH2\ta title\t34\n"
            "event\tincident\tfixing\t4\t2026-11-07T08:00:00Z\t\tEU-DE\ta title\t35\n"          # impact 4
            "event\tincident\tfixing\t1\t2026-11-07 08:00\t\tEU-DE\ta title\t36\n"              # not ISO
            "event\tincident\tfixing\t1\t2026-11-07T08:00:00Z\tsoon\tEU-DE\ta title\t37\n"      # end not ISO
            "event\tincident\tfixing\t1\t2026-11-07T08:00:00Z\t\tEU-DE\ta\ttitle\t38\n"         # a tab in the title
            "event\tincident\tfixing\t1\t2026-11-07T08:00:00Z\t\tEU-DE\ta title\t3x\n"          # no id
            "event\tincident\tfixing\t1\t2026-13-45T08:00:00Z\t\tEU-DE\ta title\t39\n")         # no such day
    data = board.platform_state(put_state(tmp_path, HEADER + rows))
    got = {r["id"]: r for r in data["rows"]}
    assert sorted(got) == [31, 32, 34]
    assert got[32]["title"] == "" and got[31]["title"] == "good title"
    assert got[34]["regions"] == ["EU-NL", "EU-CH2"]


@pytest.mark.parametrize("word", ["timeout", "http", "json"])
def test_ps_a_failed_pull_keeps_the_last_rows_and_the_header_says_the_reason(tmp_path, word):
    p = shared(tmp_path)
    ps.pull(p, getter=answers(GOOD), now=NOW)
    first = state(tmp_path).splitlines()
    later = NOW + dt.timedelta(minutes=15)
    result, written, _ = ps.pull(p, getter=failing(word), now=later)
    assert (result, written) == (word, 2)
    after = state(tmp_path).splitlines()
    assert after[0] == "pull\t2026-11-07T10:15:00Z\t%s\t2026-11-07T10:00:00Z" % word
    assert after[1:] == first[1:]
    data = board.platform_data(p, later)
    assert data["result"] == word and data["pulled"] == "2026-11-07T10:00:00Z" and len(data["events"]) == 2
    assert "failed: %s" % word in board.platform_pull_line(data)
    # the next good pull takes the header back to ok
    ps.pull(p, getter=answers(GOOD), now=later + dt.timedelta(minutes=15))
    assert state(tmp_path).splitlines()[0].split("\t")[2] == "ok"


def test_ps_a_failed_first_pull_writes_the_header_alone(tmp_path):
    assert ps.pull(shared(tmp_path), getter=failing("timeout"), now=NOW) == ("timeout", 0, 0)
    assert state(tmp_path) == "pull\t2026-11-07T10:00:00Z\ttimeout\t\n"
    data = board.platform_data(shared(tmp_path), NOW)
    assert data["events"] == [] and data["pulled"] is None
    assert board.platform_pull_line(data).startswith("status not pulled yet")


def test_ps_a_pull_older_than_two_hours_shows_not_pulled_since(tmp_path):
    p = put_state(tmp_path, "pull\t2026-11-07T08:00:00Z\tok\t2026-11-07T08:00:00Z\n")
    at = board.when("2026-11-07T08:00:00+00:00")
    fresh = board.platform_data(p, NOW - dt.timedelta(seconds=1))          # 1:59:59 old
    assert fresh["stale"] is False and board.platform_pull_line(fresh) == "status of %s" % at
    old = board.platform_data(p, NOW + dt.timedelta(seconds=1))            # 2:00:01 old
    assert old["stale"] is True and board.platform_pull_line(old) == "status not pulled since %s" % at
    assert board.platform_data(shared(tmp_path / "none"), NOW)["result"] == "none"


FIXED = (HEADER
         + "event\tmaintenance\tplanned\t1\t2026-11-12T22:00:00Z\t2026-11-13T02:00:00Z\tEU-NL\tlater work\t52\n"
         + "event\tincident\tfixing\t1\t2026-11-07T09:00:00Z\t\tEU-DE\tminor trouble\t50\n"
         + "event\tmaintenance\tplanned\t2\t2026-11-09T22:00:00Z\t2026-11-10T02:00:00Z\tEU-DE,Global\tearlier work <b>\t51\n"
         + "event\tincident\tobserving\t3\t2026-11-07T07:00:00Z\t\tEU-DE,EU-NL\tmajor trouble\t49\n"
         + "event\tincident\tresolved\t1\t2026-11-06T07:00:00Z\t2026-11-06T08:00:00Z\tEU-DE\tresolved one\t48\n")


def board_dict(platform):
    return {"generated_at": "2026-11-07T10:00:00+00:00", "projects": [], "build": "build: no runner state yet",
            "recheck": "recheck: no run yet", "platform": platform,
            "summary": {"projects": 0, "current": 0, "lagging": 0, "without_status": 0}}


def test_ps_the_board_section_lists_incidents_by_impact_then_maintenances_by_start_then_the_pull(tmp_path):
    data = board.platform_data(put_state(tmp_path, FIXED), NOW)
    assert [r["id"] for r in data["events"]] == [49, 50, 51, 52]
    md = board.markdown(board_dict(data))
    at = board.when("2026-11-07T10:00:00+00:00")
    section = md.split("## TCP platform\n", 1)[1].split("| Project |", 1)[0]
    order = [section.index(t) for t in ("major trouble", "minor trouble", "earlier work", "later work", "status of %s" % at)]
    assert order == sorted(order)
    assert "resolved one" not in md
    assert "- incident, impact 3, EU-DE, EU-NL, since %s: major trouble" % board.when("2026-11-07T07:00:00+00:00") in md
    assert "- maintenance planned, impact 2, EU-DE, Global, starts %s: earlier work <b>" % board.when(
        "2026-11-09T22:00:00+00:00") in md
    # after the header lines, before the table of the projects
    assert md.index("recheck: no run yet") < md.index("## TCP platform") < md.index("| Project |")
    page = board.page(board_dict(data))
    assert "<h2>TCP platform</h2>" in page and "earlier work &lt;b&gt;" in page and "earlier work <b>" not in page
    assert page.index("recheck: no run yet") < page.index("TCP platform") < page.index("<table>")
    assert "status of %s" % at in page


def test_ps_no_open_incident_is_one_line_and_a_stale_pull_says_so(tmp_path):
    p = put_state(tmp_path, "pull\t2026-11-07T05:00:00Z\tok\t2026-11-07T05:00:00Z\n"
                            "event\tincident\tresolved\t1\t2026-11-06T07:00:00Z\t2026-11-06T08:00:00Z\tEU-DE\told\t48\n")
    md = board.markdown(board_dict(board.platform_data(p, NOW)))
    section = md.split("## TCP platform\n", 1)[1].split("| Project |", 1)[0]
    assert section.count("no open incident") == 1
    assert "status not pulled since %s" % board.when("2026-11-07T05:00:00+00:00") in section
    assert "planned" not in section


def test_ps_a_board_without_the_platform_field_has_no_section():
    data = board_dict(None)
    del data["platform"]
    assert "TCP platform" not in board.markdown(data) and "TCP platform" not in board.page(data)


def api_fixture(tmp_path_text):
    from tests.test_web_projects_api import Tests

    t = Tests()
    t.setUp()
    folder = t.paths.shared / "platform-status"
    folder.mkdir(parents=True)
    (folder / "state.tsv").write_text(tmp_path_text, encoding="utf-8")
    return t


def test_ps_api_board_carries_the_platform_field_with_checked_titles():
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    soon = (now + dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = ("pull\t%s\tok\t%s\n"
            "event\tincident\tfixing\t2\t%s\t\tEU-DE\tmajor trouble\t49\n"
            "event\tmaintenance\tplanned\t1\t%s\t\tGlobal\tnetwork work\t51\n" % (stamp, stamp, stamp, soon))
    t = api_fixture(text)
    try:
        data = api.board(t.paths)
        assert data["platform"]["pulled"] == stamp and data["platform"]["result"] == "ok"
        assert data["platform"]["stale"] is False
        assert [(e["type"], e["impact"], e["regions"], e["title"], e["id"]) for e in data["platform"]["events"]] == [
            ("incident", 2, ["EU-DE"], "major trouble", 49), ("maintenance", 1, ["Global"], "network work", 51)]
        assert contract.validate(contract.build(), data, contract.ref("Board")) == []
        seen = []

        def check(text, paths):
            seen.append(text)
            return text.replace("trouble", "[masked]"), True
        with patch.object(api, "checked", check):
            masked = api.board(t.paths)["platform"]["events"]
        assert seen[-1] == "major trouble\nnetwork work"
        assert [e["title"] for e in masked] == ["major [masked]", "network work"]
    finally:
        t.tearDown()


def test_ps_api_board_without_a_state_file_has_an_empty_stale_platform():
    from tests.test_web_projects_api import Tests

    t = Tests()
    t.setUp()
    try:
        data = api.board(t.paths)
        assert data["platform"] == {"events": [], "pulled": None, "attempted": None, "result": "none", "stale": True}
        assert contract.validate(contract.build(), data, contract.ref("Board")) == []
    finally:
        t.tearDown()


def test_ps_show_prints_the_section_alone_and_the_command_is_registered(tmp_path, capsys):
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    t = api_fixture("pull\t%s\tok\t%s\nevent\tincident\tfixing\t2\t%s\t\tEU-DE\tmajor trouble\t49\n" % (stamp, stamp, stamp))
    try:
        from awb import config

        with patch.object(config, "paths", return_value=t.paths), patch.object(board, "locked", return_value=None):
            assert cli.main(["platform-status", "show"]) == 0
    finally:
        t.tearDown()
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("incident, impact 2, EU-DE, since ") and out[0].endswith(": major trouble")
    assert out[-1] == "status of %s" % board.when(now.replace(microsecond=0).isoformat())
    assert "platform-status" in cli.DELEGATED


def test_ps_pull_command_writes_the_file_and_reports_a_failure_with_exit_1(tmp_path, capsys):
    from awb import config

    p = types.SimpleNamespace(shared=tmp_path)
    with patch.object(config, "paths", return_value=p), patch.object(ps, "https_get", answers(GOOD)):
        assert cli.main(["platform-status", "pull"]) == 0
    assert capsys.readouterr().out.startswith("awb platform-status:") and "ok" in state(tmp_path).splitlines()[0]
    with patch.object(config, "paths", return_value=p), patch.object(ps, "https_get", failing("http")):
        assert cli.main(["platform-status", "pull"]) == 1
    assert "pull failed (http)" in capsys.readouterr().err
    assert state(tmp_path).splitlines()[0].split("\t")[2] == "http"
