"""B-100: `awb dev ask TEXT --run RUN [--call N]` attaches the failing lab call of the project's run log. Every run,
project, id and value here is invented."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import cli, config, lablog, ownerwait, projects
from tests import fixtures as fx

RUN = "run-20261125-101500-q7m4"
OTHER_RUN = "run-20261125-111500-z9x2"
PROJECT_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
BODY = {"loadbalancer": {"name": "lb-secret-value-one", "vip_subnet_cidr_id": "subnet-value-two",
                         "tags": [{"key": "k", "value": "tag-value-three"}]}}
ERROR = {"error_code": "ELB.8950", "error_msg": "The load balancer quota is exceeded."}


@pytest.fixture
def world(home, monkeypatch, tmp_path):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.chdir(pr.path)
    monkeypatch.setattr(ownerwait, "export_items", lambda *a: [])
    monkeypatch.setattr(ownerwait, "winlab_items", lambda: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda: [])
    monkeypatch.setattr(ownerwait, "queue_file", lambda: tmp_path / "none.tsv")
    return home, pr


def entry(n: int, method: str, service: str, path: str, status: int | None, body=None, answer=None) -> str:
    lines = ["## 2026-11-25T10:%02d:00Z %s %s %s" % (n, method, service, path)]
    if body is not None:
        lines += ["request body:", json.dumps(body, indent=1, sort_keys=True)]
    if status is None:
        lines += ["refused: the lab key is not allowed here"]
    else:
        lines += ["answer: %d" % status, "answer headers: {}"]
        if answer is not None:
            lines += ["answer body:", json.dumps(answer, indent=1, sort_keys=True)]
    return "\n".join(lines) + "\n\n"


def write_log(pr, run: str = RUN, project: str | None = None, extra: str = "", not_found: bool = True) -> Path:
    folder = Path(pr.path) / "notes" / "lab-runs"
    folder.mkdir(parents=True, exist_ok=True)
    text = "# lab run %s of %s on test-00001, opened 2026-11-25T10:00:00Z\nnote: -\n\n" % (run, project or pr.code)
    text += entry(1, "GET", "ecs", "/v1/%s/cloudservers" % PROJECT_ID, 200, answer={"servers": []})
    text += entry(2, "POST", "elb", "/v3/%s/elb/loadbalancers" % PROJECT_ID, 400, BODY, ERROR)
    if not_found:
        text += entry(3, "GET", "ecs", "/v1/%s/cloudservers/%s" % (PROJECT_ID, "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"),
                      404, answer={"error": {"code": "Ecs.0614", "message": "server not found"}})
    text += entry(4, "GET", "vpc", "/v1/%s/vpcs" % PROJECT_ID, 200, answer={"vpcs": []})
    text += extra
    path = folder / (run + ".log")
    path.write_text(text, encoding="utf-8")
    return path


def ask(world, *options, code=0):
    home, _ = world
    assert cli.main(["dev", "ask", "the load balancer create fails", *options]) == code
    found = sorted((home.shared / "notices/dev").glob("*.md"))
    return found[-1].read_text(encoding="utf-8") if found else ""


def test_the_failing_call_of_the_run_is_attached(world):
    _, pr = world
    write_log(pr, not_found=False)
    text = ask(world, "--run", RUN)
    assert "## Call" in text
    assert "POST elb /v3/{id}/elb/loadbalancers" in text
    assert "400 ELB.8950" in text
    assert "error_msg: The load balancer quota is exceeded." in text
    assert "loadbalancer.name" in text and "loadbalancer.vip_subnet_cidr_id" in text
    assert PROJECT_ID not in text


def test_the_last_failed_call_is_the_default_and_call_picks_one(world):
    _, pr = world
    write_log(pr)
    assert "GET ecs /v1/{id}/cloudservers/{id}" in ask(world, "--run", RUN)
    first = ask(world, "--run", RUN, "--call", "1")
    assert "GET ecs /v1/{id}/cloudservers" in first and "Answer: 200" in first
    assert "call 1 of 4" in first
    second = ask(world, "--run", RUN, "--call", "2")
    assert "400 ELB.8950" in second


def test_a_nested_error_is_read(world):
    _, pr = world
    write_log(pr)
    text = ask(world, "--run", RUN)
    assert "404 Ecs.0614" in text and "message: server not found" in text


def test_a_run_of_another_project_is_refused(world, capsys):
    home, pr = world
    write_log(pr, project="tcp-zzzz")
    capsys.readouterr()
    assert cli.main(["dev", "ask", "x", "--run", RUN]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "not a lab run of this project" in err
    assert cli.main(["dev", "ask", "x", "--run", OTHER_RUN]) == 2
    assert "not a lab run of this project" in capsys.readouterr().err
    assert not list((home.shared / "notices/dev").glob("*.md"))


def test_a_missing_call_is_refused_in_one_line(world, capsys):
    _, pr = world
    write_log(pr)
    capsys.readouterr()
    assert cli.main(["dev", "ask", "x", "--run", RUN, "--call", "9"]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "no call 9" in err
    assert cli.main(["dev", "ask", "x", "--run", RUN, "--call", "0"]) == 2
    assert cli.main(["dev", "ask", "x", "--call", "1"]) == 2


def test_a_run_without_a_failed_call_is_refused(world, capsys):
    _, pr = world
    folder = Path(pr.path) / "notes" / "lab-runs"
    folder.mkdir(parents=True)
    (folder / (RUN + ".log")).write_text(entry(1, "GET", "ecs", "/v1/x/cloudservers", 200), encoding="utf-8")
    assert cli.main(["dev", "ask", "x", "--run", RUN]) == 2
    assert "no call with a status of 400 or more" in capsys.readouterr().err


def test_a_body_value_is_never_in_the_ask(world):
    _, pr = world
    write_log(pr)
    for options in (["--run", RUN], ["--run", RUN, "--call", "2"]):
        text = ask(world, *options)
        for value in ("lb-secret-value-one", "subnet-value-two", "tag-value-three"):
            assert value not in text


def test_a_value_that_fails_the_screen_is_dropped_with_its_key_named(world):
    _, pr = world
    bad = dict(ERROR, error_msg="quota\x07 exceeded")
    write_log(pr, extra=entry(5, "POST", "elb", "/v3/%s/elb/pools" % PROJECT_ID, 400, {"pool": {}}, bad))
    text = ask(world, "--run", RUN)
    assert "\x07" not in text
    assert "Dropped, it failed the screen: error_msg" in text
    assert "400 ELB.8950" in text


def test_a_log_that_left_git_is_read_from_its_full_folder(world):
    _, pr = world
    path = write_log(pr)
    full = path.parent / lablog.FULL
    full.mkdir()
    path.rename(full / path.name)
    assert "POST elb /v3/{id}/elb/loadbalancers" in ask(world, "--run", RUN, "--call", "2")


def test_a_refused_call_is_shown_when_it_is_picked(world):
    _, pr = world
    write_log(pr, extra=entry(5, "DELETE", "ecs", "/v1/%s/cloudservers/x" % PROJECT_ID, None))
    assert "refused by the key service: the lab key is not allowed here" in ask(world, "--run", RUN, "--call", "5")


def test_ids_in_a_path_become_a_placeholder():
    assert lablog.mask_ids("/v3/{project_id}/elb/loadbalancers/%s?limit=5" % PROJECT_ID.upper()) == \
        "/v3/{id}/elb/loadbalancers/{id}"
    assert lablog.mask_ids("/v1.1/%s/cloudservers/12345" % PROJECT_ID) == "/v1.1/{id}/cloudservers/{id}"
