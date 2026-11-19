"""Three lab run fixes of 2026-11-19: a listed delete counts as removed (tcp-6edq), a boot script stays out of the
run log (tcp-s5i3) and a home path in a run log passes the commit gate (tcp-bytg, tcp-s5i3). Every id, path and
script here is invented and built from pieces at run time."""
from __future__ import annotations

import base64
import os
import time
from types import SimpleNamespace

from awb import config, gate, labclient, labruns, ownerwait
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import LAB, P_DE, PROJECT, fake, lab_call, run_op, svc  # noqa: F401

ECS = "-".join(("464462e8", "1111", "4222", "8333", "9444" + "1" * 8))
EIP = "-".join(("f96fcbeb", "2222", "4333", "8444", "9555" + "2" * 8))
IMG = "-".join(("f4831f5b", "3333", "4444", "8555", "9666" + "3" * 8))
HEAD = ("| handle | id | type | region | cost class | expiry | state | note |\n"
        "|---|---|---|---|---|---|---|---|\n")
ROWS = ("| ecs-1 | %s | ecs cloudservers | eu-de | - | - | live | kept on %s |\n"
        "| eip-1 | %s | vpc publicips | eu-de | - | - | live | kept on %s |\n"
        "| image-1 | `%s` | ims images | eu-de | - | - | live | - |\n") % (ECS, LAB, EIP, LAB, IMG)
DELETES = [("POST", "ecs", "/v1/{project_id}/cloudservers/delete", {"servers": [{"id": ECS}], "delete_publicip": False}),
           ("DELETE", "vpc", "/v1/{project_id}/publicips/" + EIP, None),
           ("DELETE", "ims", "/v2/images/" + IMG, None)]


def _folder(tmp_path):
    folder = tmp_path / "projects" / PROJECT
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SCOPE.md").write_text("# %s\n\n- code: %s\n" % (PROJECT, PROJECT), encoding="utf-8")
    (folder / "RESOURCES.md").write_text("# Resources of %s\n\nintro line\n\n%s%s" % (PROJECT, HEAD, ROWS),
                                         encoding="utf-8")
    return folder


def _plant(fake):
    fake.store["/v1/%s/cloudservers/%s" % (P_DE, ECS)] = {}
    fake.store["/v1/%s/publicips/%s" % (P_DE, EIP)] = {"id": EIP}
    fake.store["/v2/images/%s" % IMG] = {"id": IMG}


def _as_project(svc, folder):
    row = SimpleNamespace(code=PROJECT, kind="project", state="active", customer=None, path=str(folder))
    svc._project_row = lambda code: row if code == PROJECT else None
    svc._granted = lambda code: code == PROJECT
    svc._console_uid = lambda: None


def test_lab_fix_listed_deletes_of_each_kind_count_as_removed_and_set_the_rows_deleted(svc, fake, tmp_path,
                                                                                       monkeypatch, capsys):
    folder = _folder(tmp_path)
    _as_project(svc, folder)
    _plant(fake)
    monkeypatch.chdir(folder)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    for method, service, path, body in DELETES:
        status, _h, _b = labclient.call(method, service, path, body=body, delete_listed=True)
        assert 200 <= status < 300
    assert sorted(fake.deleted) == sorted(["/v1/%s/cloudservers/%s" % (P_DE, ECS), "/v1/%s/publicips/%s" % (P_DE, EIP),
                                           "/v2/images/%s" % IMG])
    text = (folder / "RESOURCES.md").read_text(encoding="utf-8")
    run = (folder / "notes" / "lab-runs" / ("%s.open" % LAB)).read_text().strip()
    for handle in ("ecs-1", "eip-1", "image-1"):
        line = next(x for x in text.splitlines() if x.startswith("| %s |" % handle))
        assert "| deleted |" in line and ("deleted in lab run %s" % run) in line, line
    assert text.startswith("# Resources of %s\n\nintro line\n\n%s" % (PROJECT, HEAD))
    from awb import projects

    assert projects.live_resources(folder) == 0
    capsys.readouterr()
    assert labclient.run_main(["end"]) == 0
    out = capsys.readouterr().out
    assert "created 0, removed 3, failed 0, kept 0" in out, out
    for handle in ("ecs-1", "eip-1", "image-1"):
        assert "  removed  " in out and handle in out
    log = (folder / "notes" / "lab-runs" / ("%s.log" % run)).read_text(encoding="utf-8")
    assert "removed listed: ecs-1, row ecs-1 in RESOURCES.md set to deleted" in log
    assert "created 0, removed 3, failed 0, kept 0" in log
    record = next(r for r in labruns.load_runs(svc.paths_fn()) if r["id"] == run)
    assert sorted(g["handle"] for g in record["listed_removed"]) == ["ecs-1", "eip-1", "image-1"]
    assert all(g["state"] == "removed" for g in record["listed_removed"])


def test_lab_fix_a_listed_delete_ends_a_kept_create_and_a_refused_one_is_not_counted(svc, fake, tmp_path):
    folder = _folder(tmp_path)
    _as_project(svc, folder)
    _plant(fake)
    uid = os.getuid() + 1
    p = svc.paths_fn()
    old = {"id": labruns.new_id(), "project": PROJECT, "tenant": LAB, "note": "", "state": "ended",
           "started": "x", "started_ts": time.time(), "calls": 1,
           "created": [{"n": 1, "label": "vpc publicips", "state": "kept", "reason": "stand", "kept_ts": time.time(),
                        "delete": {"method": "DELETE", "service": "vpc", "path": "/v1/%s/publicips/%s" % (P_DE, EIP)}}]}
    labruns.save(p, old)
    assert [i.text for i in ownerwait.lab_run_items(p, time.time()) if "kept" in i.text]
    run = run_op(svc, uid, "start")["run"]
    fake.fail.add("/v2/images/%s" % IMG)
    answers = [lab_call(svc, uid, m, s, path, run, body=b, delete_listed=True) for m, s, path, b in DELETES]
    assert [a["status"] for a in answers] == [200, 204, 409]
    assert [g["handle"] for g in answers[0]["listed_removed"]] == ["ecs-1"]
    assert answers[1]["listed_removed"] == [{"handle": "eip-1", "id": EIP, "label": "vpc publicips"}]
    assert "listed_removed" not in answers[2]
    again = lab_call(svc, uid, "DELETE", "vpc", "/v1/{project_id}/publicips/" + EIP, run, delete_listed=True)
    assert again["status"] == 404 and again["listed_removed"][0]["handle"] == "eip-1"
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and ended["created"] == 0 and ended["failed"] == []
    assert sorted(r["name"] for r in ended["removed"]) == ["ecs-1", "eip-1"]
    saved = next(r for r in labruns.load_runs(p) if r["id"] == old["id"])
    assert saved["created"][0]["state"] == "removed" and saved["created"][0]["removed_in"] == run
    assert not [i for i in ownerwait.lab_run_items(p, time.time()) if "kept" in i.text]


def test_lab_fix_user_data_of_every_server_stays_out_of_the_run_log(svc, fake, tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    _as_project(svc, folder)
    monkeypatch.chdir(folder)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    home = "/ho" + "me/" + "nodeuser/.kube/config"
    script = "#!/bin/bash\nmkdir -p %s\necho cluster-node-bootstrap\n" % home
    coded = base64.b64encode(script.encode()).decode()
    fake.store["/v1/%s/cloudservers/detail" % P_DE] = {"servers": [
        {"id": ECS, "name": "other-node-1", "OS-EXT-SRV-ATTR:user_data": coded},
        {"id": EIP, "name": "other-node-2", "metadata": {"user_data": script},
         "extendParam": {"alpha.cce/preInstall": coded}}]}
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    status, _h, body = labclient.call("GET", "ecs", "/v1/{project_id}/cloudservers/detail")
    assert status == 200 and body["servers"][0]["name"] == "other-node-1"
    labclient.call("POST", "ecs", "/v1/{project_id}/cloudservers",
                   body={"server": {"name": "lab-fix", "user_data": coded}})
    run = (folder / "notes" / "lab-runs" / ("%s.open" % LAB)).read_text().strip()
    log = (folder / "notes" / "lab-runs" / ("%s.log" % run)).read_text(encoding="utf-8")
    for value in (coded, script, home, "cluster-node-bootstrap", coded[:40]):
        assert value not in log
    assert log.count(labclient.BOOT_SCRIPT_NOTE % len(coded)) == 3, log
    assert (labclient.BOOT_SCRIPT_NOTE % len(script)) in log
    assert "other-node-2" in log and ECS in log
    assert labclient._withhold({"a": [{"user_data": ""}], "userData": None}) == {"a": [{"user_data": ""}],
                                                                                 "userData": None}
    assert labclient.run_main(["end"]) == 0


def _lab_log(tmp_path, text: str):
    f = tmp_path / "notes" / "lab-runs" / "x.log"
    f.parent.mkdir(parents=True)
    f.write_text(text, encoding="utf-8")
    return f


def _secret() -> str:
    return "db_pass" + "word = \"" + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc" + "\""


def test_lab_fix_a_home_path_in_a_lab_log_passes_the_gate_and_a_secret_does_not(tmp_path, register_path):
    home = "/ho" + "me/" + "builder/.kube/config"
    f = _lab_log(tmp_path, "run start\nmkdir -p %s\n" % home)
    assert gate.scan_files([f], register_path) == []
    assert "homepath" not in gate.OWN_OUTPUT_CLASSES
    assert {"secret", "private-key", "token"} <= gate.OWN_OUTPUT_CLASSES
    f.write_text("run start\nmkdir -p %s\n%s\n" % (home, _secret()), encoding="utf-8")
    assert [(x.line, x.cls) for x in gate.scan_files([f], register_path)] == [(3, "secret")]
    other = tmp_path / "notes" / "other.log"
    other.write_text("mkdir -p %s\n" % home, encoding="utf-8")
    assert [(x.line, x.cls) for x in gate.scan_files([other], register_path)] == [(1, "homepath")]
