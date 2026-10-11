"""The test timeout of tests/timeouts.py: a test that blocks again after its limit, or that lost its alarm, fails
within the limit instead of holding the run (a full run hung nine hours on 2026-10-10 in spite of the 120 s limit).

Each case runs a small suite of its own in a child pytest with a 2 s limit and a 4 s grace."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import hooks

ROOT = Path(__file__).resolve().parent.parent
LIMIT, GRACE = 2, 4


def _inner(tmp_path: Path, body: str) -> tuple[int, str, float]:
    (tmp_path / "pytest.ini").write_text("[pytest]\ntimeout = %d\ntimeout_grace = %d\n" % (LIMIT, GRACE),
                                         encoding="utf-8")
    (tmp_path / "conftest.py").write_text("from tests.timeouts import *  # noqa: F401,F403\n", encoding="utf-8")
    (tmp_path / "test_inner.py").write_text(body, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_inner.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start


def test_a_teardown_that_blocks_twice_fails_at_the_limit_and_the_run_goes_on(tmp_path):
    """Two stand-in servers whose shutdown blocks: the first wait is cut at the limit, the second one a second
    later, and the next test of the run still runs."""
    code, out, took = _inner(tmp_path, """
import threading
import pytest

@pytest.fixture
def two_servers():
    yield
    try:
        threading.Event().wait()
    finally:
        threading.Event().wait()

def test_blocks(two_servers):
    pass

def test_after():
    pass
""")
    assert code == 1, out
    assert "2 passed, 1 error" in out and "still blocked" in out, out
    assert took < LIMIT + GRACE, (took, out)


def test_a_test_whose_alarm_was_switched_off_ends_the_run_after_the_grace(tmp_path):
    """No alarm can reach a test that switched it off; the last-resort thread ends the run with the stacks."""
    code, out, took = _inner(tmp_path, """
import signal
import threading

def test_blocks_without_an_alarm():
    signal.setitimer(signal.ITIMER_REAL, 0)
    threading.Event().wait()
""")
    assert code == 1, out
    assert "Timeout" in out and "test_blocks_without_an_alarm" in out, out
    assert took < LIMIT + GRACE + 10, (took, out)


def test_an_in_process_hook_gives_the_alarm_back(monkeypatch, capsys):
    """hooks.main sets its own budget alarm; afterwards the caller's handler and the time it had left are back."""
    seen = []
    old = signal.signal(signal.SIGALRM, lambda s, f: seen.append(s))
    try:
        signal.setitimer(signal.ITIMER_REAL, 30.0)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Read", "tool_input": {}})))
        monkeypatch.setattr(hooks, "for_this_user", lambda: True)
        hooks.main(["pre-tool"])
        capsys.readouterr()
        left, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 20.0 < left <= 30.0
        assert signal.getsignal(signal.SIGALRM) is not signal.SIG_DFL
        signal.setitimer(signal.ITIMER_REAL, 0.01)
        time.sleep(0.2)
        assert seen == [signal.SIGALRM]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.timeout(1, method="signal")
def test_the_suite_runs_under_the_repeating_alarm():
    """The suite's own timer is this one: the alarm of the run repeats after its limit."""
    _, interval = signal.getitimer(signal.ITIMER_REAL)
    assert interval == pytest.approx(1.0)
