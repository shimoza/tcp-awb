"""The test timeout that holds when a test blocks more than once (imported by tests/conftest.py).

pytest-timeout's signal method arms one SIGALRM per test. It fails the first blocking wait after the limit and is
then spent, so a second blocking wait in the teardown waits for ever: the shutdown of the second stand-in bucket
after the first one was cut, a join after a cut join. A full run hung nine hours that way on 2026-10-10 in spite
of the 120 s limit. A test that ran `awb hook` in its own process lost its timer as well, because hooks.main set
its own alarm and switched it off at the end (it now gives the alarm back).

Here the alarm fires again every REPEAT seconds after the limit until the test ends, so every further blocking wait
fails too, and only while the test runs its setup, call or teardown (never inside pytest's own reporting). A thread
ends the whole run with the stacks of every thread when the test is still not done GRACE seconds after the limit:
the main thread never came back to Python (a wait in C that retries after a signal, an alarm switched off)."""
from __future__ import annotations

import signal
import threading

import pytest

REPEAT = 1.0
GRACE = 30.0
_PHASE = pytest.StashKey[bool]()


def pytest_addoption(parser):
    parser.addini("timeout_grace", "seconds after the timeout before the run is ended (tests/timeouts.py)",
                  default=str(GRACE))


@pytest.hookimpl(tryfirst=True)
def pytest_timeout_set_timer(item, settings):
    """The signal method with a repeating alarm and a last-resort thread; other methods stay pytest-timeout's."""
    if (settings.method != "signal" or not hasattr(signal, "SIGALRM")
            or threading.current_thread() is not threading.main_thread()):
        return None
    import pytest_timeout

    fired = []

    def handler(signum, frame):
        __tracebackhide__ = True
        if not item.stash.get(_PHASE, False):
            return
        if fired:
            pytest.fail("Timeout (>%ss) and still blocked" % settings.timeout, pytrace=False)
        fired.append(True)
        pytest_timeout.timeout_sigalrm(item, settings)

    last = threading.Timer(settings.timeout + float(item.config.getini("timeout_grace")),
                           pytest_timeout.timeout_timer, (item, settings))
    last.daemon = True

    def cancel():
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        last.cancel()

    item.cancel_timeout = cancel
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, settings.timeout, REPEAT)
    last.start()
    return True


def _phase(item):
    item.stash[_PHASE] = True
    try:
        return (yield)
    finally:
        item.stash[_PHASE] = False


pytest_runtest_setup = pytest.hookimpl(wrapper=True)(_phase)
pytest_runtest_call = pytest.hookimpl(wrapper=True)(_phase)
pytest_runtest_teardown = pytest.hookimpl(wrapper=True)(_phase)
