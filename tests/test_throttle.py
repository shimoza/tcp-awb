"""Staying below the request limits of the TCP API gateway (awb/tcp/throttle.py): calls spaced per host across
processes, the wait a 429 names, and a call log without paths or ids."""
from __future__ import annotations

import datetime
import json

import pytest

from awb import obs
from awb.tcp import cloud, throttle
from tests.tcp_fake import FakeGateway

AK, SK = "AKFAKE", "sk-fake-secret"


@pytest.fixture(autouse=True)
def own_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AWB_THROTTLE_DIR", str(tmp_path / "throttle"))


class Clock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.now += s


def test_calls_to_one_host_are_spaced_and_other_hosts_are_not():
    clk = Clock()
    assert throttle.wait_turn("ecs.eu-de.example", clock=clk.time, sleep=clk.sleep) == 0
    clk.now += 0.1
    assert throttle.wait_turn("ecs.eu-de.example", clock=clk.time, sleep=clk.sleep) == pytest.approx(0.15)
    assert throttle.wait_turn("evs.eu-de.example", clock=clk.time, sleep=clk.sleep) == 0
    clk.now += 5
    assert throttle.wait_turn("ecs.eu-de.example", clock=clk.time, sleep=clk.sleep) == 0


def test_the_spacing_is_shared_through_the_lock_file(tmp_path):
    clk = Clock()
    throttle.wait_turn("iam.example", clock=clk.time, sleep=clk.sleep)
    stamp = float((tmp_path / "throttle" / "iam.example").read_text())
    assert stamp == clk.now, "a second process reads the same stamp and waits for it"


def test_retry_after_reads_seconds_and_dates_and_is_capped():
    now = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.timezone.utc)
    assert throttle.retry_after("3") == 3.0
    assert throttle.retry_after("600") == throttle.MAX_RETRY_AFTER
    assert throttle.retry_after("Tue, 29 Sep 2026 12:00:05 GMT", now=now) == 5.0
    assert throttle.retry_after("soon") is None and throttle.retry_after(None) is None


def test_a_429_waits_what_it_names_and_every_call_is_logged(home, monkeypatch):
    slept = []
    monkeypatch.setattr(cloud.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr("awb.jobs.time.sleep", lambda s: slept.append(s))
    with FakeGateway(AK, SK) as g:
        g.throttled, g.retry_after = 1, "4"
        c = cloud.Client(obs.Keys(AK, SK), "eu-de", endpoint=g.base, label="test-1")
        r = c.get("ecs", "/v1/{project_id}/throttled")
        assert r.status == 200 and 4.0 in slept
    log = (home.shared / "tenants" / ("calls-%s.jsonl" % datetime.date.today().strftime("%Y-%m")))
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [x["status"] for x in lines] == [200, 429, 200] and {x["tenant"] for x in lines} == {"test-1"}
    text = log.read_text(encoding="utf-8")
    assert FakeGateway.PROJECT not in text and "throttled" not in text, "no path and no id in the log"
    usage = "\n".join(throttle.usage_lines(home.shared, datetime.date.today().strftime("%Y-%m")))
    assert "throttled (429) this month: 1" in usage and "ecs 2" in usage


def test_usage_without_calls_and_the_command_line(home, capsys):
    assert throttle.usage_lines(home.shared, "2026-01") == ["2026-01: no call to the TCP API recorded"]
    assert cloud.main(["usage", "--month", "2026-01"]) == 0
    assert "no call to the TCP API" in capsys.readouterr().out
    assert cloud.main(["usage", "--month", "january"]) == 2
