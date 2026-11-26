"""B-094, F7 of the attacker review of 2026-11-26: `awb close` refuses while the project has a bucket that
`awb lab run end --keep` left on a test tenant (the kept list of the vault), with the line `awb lab kept drop NAME`
for each, unless --force. The vault is a temporary one with invented codes."""
from __future__ import annotations

from awb import cli, labruns, projects
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


def kept_row(name: str) -> dict:
    return {"tenant": "test-10491", "region": "eu-de", "kind": "obs bucket", "name": name,
            "run": "lab-20261126-100000-abcd", "date": "2026-11-26"}


def spawn(home, register_path):
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def test_a_project_with_a_kept_bucket_does_not_close_and_names_the_drop(home, register_path, capsys):
    pr = spawn(home, register_path)
    labruns.save_kept(home, pr.code, [kept_row("tcp-q7m4-lab-1")])
    assert cli.main(["close", pr.code]) == 2
    err = capsys.readouterr().err
    assert "awb lab kept drop tcp-q7m4-lab-1" in err.splitlines()
    assert {r.code: r.state for r in projects.load(home)}[pr.code] != "closed"


def test_every_kept_bucket_gets_its_line_and_another_project_s_does_not(home, register_path, capsys):
    pr = spawn(home, register_path)
    other = spawn(home, register_path)
    labruns.save_kept(home, pr.code, [kept_row("tcp-q7m4-lab-2"), kept_row("tcp-q7m4-lab-1")])
    labruns.save_kept(home, other.code, [kept_row("tcp-zz99-lab-1")])
    assert cli.main(["close", pr.code]) == 2
    lines = [x for x in capsys.readouterr().err.splitlines() if x.startswith("awb lab kept drop ")]
    assert lines == ["awb lab kept drop tcp-q7m4-lab-1", "awb lab kept drop tcp-q7m4-lab-2"]
    labruns.save_kept(home, pr.code, [])
    assert cli.main(["close", pr.code]) == 0


def test_the_work_user_reads_the_kept_list_from_the_key_service(home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path)
    monkeypatch.setattr(cli.config, "is_work_user", lambda: True)
    asked = []

    def delegate(name, argv):
        asked.append((name, argv))
        print("kept  test-10491  eu-de  obs bucket  tcp-q7m4-lab-1  lab-20261126-100000-abcd  2026-11-26")
        print("kept  test-10491  eu-de  obs bucket  Not A Bucket  lab-20261126-100000-abcd  2026-11-26")
        return 0

    monkeypatch.setattr(cli, "_delegate", delegate)
    assert cli._kept_buckets(pr.code, home) == ["tcp-q7m4-lab-1"]
    assert asked == [("lab", ["run", "status", "--project", pr.code])]
    assert capsys.readouterr().out == ""   # the answer of the key service is read, never shown


def test_force_closes_a_project_with_a_kept_bucket(home, register_path, capsys):
    pr = spawn(home, register_path)
    labruns.save_kept(home, pr.code, [kept_row("tcp-q7m4-lab-1")])
    assert cli.main(["close", pr.code, "--force"]) == 0
    assert "closed %s" % pr.code in capsys.readouterr().out
    assert {r.code: r.state for r in projects.load(home)}[pr.code] == "closed"
