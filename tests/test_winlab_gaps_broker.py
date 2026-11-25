"""tcp-bytg ask 2026-11-21-tcp-bytg-14: after the end of a window the lab broker still serves the status, the stop
and the log of a job that started inside that window, and nothing else. The fake ssh and the clock are those of
tests/test_wl_broker.py."""
from __future__ import annotations

import datetime

from awb import winlabsession
from tests.test_wl_broker import MACHINE, OTHER, PROJECT, T0, call, lab, open_window, refused  # noqa: F401

JOB = "wl-20261105-100500-a1b2"
LOG = "C:\\ProgramData\\awb-winlab\\jobs\\%s\\log.txt" % JOB
STARTED = "2026-11-05T10:05:00Z"


def _ended(lab):
    open_window(lab, minutes=30)
    lab.clock[0] = T0 + datetime.timedelta(minutes=45)
    lab.calls.clear()


def test_gap_broker_serves_the_state_the_stop_and_the_log_of_a_job_after_the_window(lab):
    _ended(lab)
    assert "ended at" in refused(call(lab, "stat", path=LOG))
    assert call(lab, "check", job_started=STARTED)[0]["ok"] is True
    for script in (winlabsession.job_status_script(JOB), winlabsession.job_stop_script(JOB)):
        assert call(lab, "run", text=script, job_started=STARTED)[0]["ok"] is True
    lab.files[LOG.lower()] = bytearray(b"row one\n")
    assert call(lab, "stat", path=LOG, job_started=STARTED)[0]["stat"]["size"] == 8
    assert call(lab, "get_chunk", path=LOG, offset=0, size=8, job_started=STARTED)[1] == b"row one\n"


def test_gap_broker_refuses_everything_else_after_the_window(lab):
    _ended(lab)
    for fields in ({"op": "run", "text": "Get-VM"},
                   {"op": "run", "text": winlabsession.job_status_script(JOB) + "Get-VM\n"},
                   {"op": "stat", "path": "C:\\lab\\a.txt"},
                   {"op": "stat", "path": LOG, "digest": True},
                   {"op": "put_chunk", "path": LOG, "offset": 0, "length": 1}):
        assert "the window has ended" in refused(call(lab, job_started=STARTED, **fields)), fields


def test_gap_broker_refuses_a_job_that_started_outside_the_window(lab):
    _ended(lab)
    assert "ended at" in refused(call(lab, "check", job_started="2026-11-05T09:00:00Z"))
    assert "ended at" in refused(call(lab, "check", job_started="2026-11-05T10:31:00Z"))
