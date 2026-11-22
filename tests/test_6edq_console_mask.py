"""A password an image prints on its serial console never reaches a run log (tcp-6edq ask 1, 2026-11-22): the
cloud-init line "Set the following 'random' passwords" and the `ubuntu:password` line after it went unmasked into
the full log of a lab run. The answer of os-getConsoleOutput passes the key service (labadmin.shorten_answer) and
the lab client (labclient.log), both through awb/labsecrets.py. The password here is invented and built at run
time."""
from __future__ import annotations

import json

from awb import labclient, labsecrets
from awb.tcp import labadmin
from tests.test_lr_lab_runs import _HARD_RUN, _hard_project

PASSWORD = "".join(("Qz7k", "Wm3p", "Tx9v", "Rb2n"))
CONSOLE = ("[  OK  ] Started cloud-init.service.\r\n"
           "Set the following 'random' passwords\r\n"
           "ubuntu:%s\r\n\r\n"
           "Ubuntu 24.04.1 LTS lab-host ttyS0\r\n\r\nlab-host login: " % PASSWORD)


def test_6edq_the_random_password_line_of_cloud_init_is_masked_raw_and_as_json():
    for text in (CONSOLE, CONSOLE.replace("\r\n", "\n"), json.dumps({"output": CONSOLE}, indent=1),
                 "[   41.2] cloud-init[812]: Set the following 'random' passwords\n"
                 "[   41.2] cloud-init[812]: ubuntu:%s\nlab-host login: " % PASSWORD):
        out, counts = labsecrets.shorten(text)
        assert PASSWORD not in out and counts["secret"] == 1, out
        assert "ubuntu:" + labsecrets.SECRET in out and "lab-host login:" in out


def test_6edq_chpasswd_and_password_lines_of_a_console_are_masked_and_prose_stays():
    texts = ["echo 'ubuntu:%s' | chpasswd" % PASSWORD,
             "chpasswd:\n  list: |\n    root:%s\n    ubuntu:%s\n  expire: false\n" % (PASSWORD, PASSWORD),
             json.dumps({"output": "boot\nPassword: %s\nlogin" % PASSWORD}),
             "New password for ubuntu: %s" % PASSWORD]
    for text in texts:
        out, counts = labsecrets.shorten(text)
        assert PASSWORD not in out and counts["secret"] >= 1, out
    for text in ("Set the password for root: change it", "pull ubuntu:24.04 and ubuntu:noble-20241011",
                 "root:x:0:0:root:/root:/bin/bash"):
        assert labsecrets.shorten(text) == (text, labsecrets.Counter())


def test_6edq_the_key_service_masks_the_console_in_its_answer():
    answer = {"ok": True, "status": 200, "headers": {}, "data": {"output": CONSOLE}, "text": ""}
    shown = labadmin.shorten_answer(answer, {}, [])
    assert PASSWORD not in json.dumps(shown)
    assert "ubuntu:" + labsecrets.SECRET in shown["data"]["output"]
    assert shown["data"]["output"].endswith("lab-host login: ")


def test_6edq_the_lab_client_writes_no_console_password_into_the_run_log(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "status": 200, "headers": {},
                                                           "data": {"output": CONSOLE}})
    status, _, out = labclient.call("POST", "ecs", "/v2/{project_id}/servers/srv-1/action",
                                    body={"os-getConsoleOutput": {}})
    assert status == 200 and out["output"] == CONSOLE        # the client hands on what the service answered
    log = (logs / (_HARD_RUN + ".log")).read_text()
    assert PASSWORD not in log and "ubuntu:" + labsecrets.SECRET in log
