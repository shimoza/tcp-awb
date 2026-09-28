"""awb/jobs.py: three states for a list, safe reads, a time budget, resume and bounded retries (T-60)."""
from __future__ import annotations

import io

import pytest

from awb import jobs


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FlushCounter(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


def test_a_listing_has_three_states_and_no_truth_value():
    assert jobs.Listing.of([]).state == jobs.EMPTY
    assert jobs.Listing.of(iter([1, 2])).items == (1, 2)
    u = jobs.Listing.unknown("HTTP 503")
    assert u.state == jobs.UNKNOWN and not u.known and u.describe() == "unknown: HTTP 503"
    with pytest.raises(TypeError, match="three states"):
        bool(u)
    with pytest.raises(TypeError):
        if jobs.Listing.of([]):                     # the classic slip: unknown or empty read as "nothing there"
            pass
    with pytest.raises(ValueError):
        jobs.Listing(jobs.LIST, ())
    with pytest.raises(ValueError):
        jobs.Listing("maybe")


def test_dig_reads_a_nested_answer_without_raising():
    data = {"servers": [{"id": "a", "addresses": {"net": [{"addr": "x"}]}}]}
    assert jobs.dig(data, "servers", 0, "addresses", "net", -1, "addr") == "x"
    assert jobs.dig(data, "servers", 3, "id") is jobs.MISSING
    assert jobs.dig(data, "servers", "0") is jobs.MISSING
    assert jobs.dig(None, "servers") is jobs.MISSING
    assert jobs.dig({"a": [1]}, "a", True) is jobs.MISSING      # a bool is no index
    assert jobs.dig(data, "nothing", default=[]) == []
    assert not jobs.MISSING and repr(jobs.MISSING) == "MISSING"


def test_say_flushes_every_line():
    out = FlushCounter()
    job = jobs.Job("say", None, out=out)
    job.say("deleted 1 of 3")
    job.say("deleted 2 of 3")
    assert out.getvalue() == "deleted 1 of 3\ndeleted 2 of 3\n" and out.flushes >= 2


def test_a_killed_job_resumes_where_it_stopped(tmp_path):
    ran: list[str] = []
    first = jobs.Job("teardown", tmp_path)
    for key in ("a", "b"):
        first.step(key, lambda key=key: ran.append(key) or {"deleted": key})
    # the process dies here: no finish()
    second = jobs.Job("teardown", tmp_path)
    got = [second.step(k, lambda k=k: ran.append(k) or {"deleted": k}) for k in ("a", "b", "c")]
    assert ran == ["a", "b", "c"]
    assert got == [{"deleted": "a"}, {"deleted": "b"}, {"deleted": "c"}]
    assert (second.skipped, second.ran) == (2, 1)
    second.finish()
    assert not second.state_path.exists()
    assert jobs.Job("teardown", tmp_path).done_count == 0


def test_a_line_torn_by_a_kill_runs_that_step_again(tmp_path):
    job = jobs.Job("torn", tmp_path)
    job.step("a", lambda: 1)
    with open(job.state_path, "a", encoding="utf-8") as f:
        f.write('{"key": "b", "val')
    again = jobs.Job("torn", tmp_path)
    assert again.is_done("a") and not again.is_done("b")


def test_the_budget_stops_a_job_before_the_next_step():
    clock = Clock()
    job = jobs.Job("budget", None, budget=10, clock=clock)
    job.step("one", lambda: 1)
    clock.now = 7.0
    with pytest.raises(jobs.JobStopped, match="run the same command again") as exc:
        job.step("two", lambda: 2, cost=5)
    assert exc.value.done == 1
    clock.now = 11.0
    with pytest.raises(jobs.JobStopped):
        job.step("three", lambda: 3)


def test_a_bad_job_name_is_refused(tmp_path):
    with pytest.raises(ValueError):
        jobs.Job("../escape", tmp_path)


def test_retry_waits_longer_each_time_and_never_past_the_budget():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("slow")
        return "ok"

    assert jobs.retry(flaky, attempts=3, delay=1.0, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]

    clock = Clock()
    job = jobs.Job("r", None, budget=3, clock=clock)
    calls.clear()
    sleeps.clear()

    def always():
        calls.append(1)
        clock.now += 0.5
        raise TimeoutError("slow")

    with pytest.raises(TimeoutError):
        jobs.retry(always, attempts=5, delay=1.0, job=job, sleep=lambda s: (sleeps.append(s), setattr(clock, "now", clock.now + s)))
    assert sum(sleeps) + 0.5 * len(calls) <= 3.0      # the budget was never passed


def test_retry_lets_other_errors_through_at_once():
    calls = []

    def broken():
        calls.append(1)
        raise KeyError("bug")

    with pytest.raises(KeyError):
        jobs.retry(broken, attempts=3, retry_on=(TimeoutError,), sleep=lambda s: None)
    assert len(calls) == 1


def test_wait_gone_asks_about_one_resource_until_it_is_gone():
    clock = Clock()

    def run(answers, timeout):
        it = iter(answers)
        return jobs.wait_gone(lambda: next(it), timeout=timeout, interval=5, clock=clock,
                              sleep=lambda s: setattr(clock, "now", clock.now + s))

    assert run(["present", "present", "gone"], 60) == "gone"
    clock.now = 0
    assert run(["present"] * 20, 12) == "present"
    clock.now = 0
    assert run(["present", "unknown", "unknown"], 12) == "unknown"
    with pytest.raises(ValueError):
        run(["deleted"], 12)
