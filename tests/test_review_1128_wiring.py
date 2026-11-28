"""The two places the fix of G4 and G11 (attacker review of 2026-11-28) left outside its three files: `awb close`
refuses while the project still holds a bucket grant of the owner, and `awb lab obs link --owner-word TEXT` hands
the owner's words to the key service, which signs a binary or large object only with them. The vault is a temporary
one with invented codes."""
from __future__ import annotations

from awb import cli, labclient, projects
from awb.tcp import bucketgrant
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"
WORD = "the bucket shared-images belongs only to this project"


def _state(home, code):
    return {r.code: r.state for r in projects.load(home)}[code]


def _grant(home, code):
    assert bucketgrant.grant(home, "test-10503", "shared-images", code, WORD, "2026-11-28", say=lambda _l: None,
                             exchange={"awb", "awb-lab-eu-de"})


def test_close_is_refused_while_the_project_holds_a_bucket_grant(home, register_path, monkeypatch, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    _grant(home, pr.code)
    monkeypatch.setattr(cli, "_kept_buckets", lambda code, p: [])
    assert cli.main(["close", pr.code]) != 0
    err = capsys.readouterr().err
    assert "bucket grant" in err and "awb tenant bucket-revoke tcp-10503 shared-images" in err
    assert _state(home, pr.code) == "active"


def test_close_goes_on_once_the_grant_is_revoked(home, register_path, monkeypatch, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    _grant(home, pr.code)
    assert bucketgrant.revoke(home, "test-10503", "shared-images")
    monkeypatch.setattr(cli, "_kept_buckets", lambda code, p: [])
    assert cli.main(["close", pr.code]) == 0
    assert _state(home, pr.code) == "closed"


def test_force_closes_with_a_grant_left(home, register_path, monkeypatch, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    _grant(home, pr.code)
    assert cli.main(["close", pr.code, "--force"]) == 0
    assert _state(home, pr.code) == "closed"


def _fake_link(monkeypatch, tmp_path):
    sent = []

    def request(obj, root=None, fds=()):
        sent.append(obj)
        return {"ok": False, "error": "refused: test"}

    monkeypatch.setattr(labclient, "_root", lambda start=None: tmp_path)
    monkeypatch.setattr(labclient, "_pick", lambda root, tenant, run: ("test-10503", "run-1"))
    monkeypatch.setattr(labclient, "_code", lambda root: "tcp-q7m4")
    monkeypatch.setattr(labclient, "log", lambda root, run, lines: None)
    monkeypatch.setattr(labclient, "_request", request)
    return sent


def test_obs_link_hands_the_owner_word_to_the_key_service(monkeypatch, tmp_path):
    sent = _fake_link(monkeypatch, tmp_path)
    assert labclient.obs_main(["link", "awb-run-1", "setup.exe", "--owner-word", "yes, link the installer"]) != 0
    assert sent and sent[0]["owner_word"] == "yes, link the installer"


def test_obs_link_without_the_flag_sends_no_owner_word(monkeypatch, tmp_path):
    sent = _fake_link(monkeypatch, tmp_path)
    labclient.obs_main(["link", "awb-run-1", "notes.txt"])
    assert sent and "owner_word" not in sent[0]
