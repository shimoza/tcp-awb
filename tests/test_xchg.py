"""awb/tcp/xchg.py through the key service: the two inboxes, the take, the put and the close check (T-102).

Two stand-in buckets (tests/obs_fake.py) play the lab bucket and the owner bucket; the key service runs on short
socket paths; every name is an invented fixture name.
"""
from __future__ import annotations

import base64
import contextlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from awb import cli, config, jobs, projects
from awb.tcp import keys, xchg
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

AK, SK = "AKLABFAKE", "sk-lab-fake-secret"
GOAL = "move two app clusters to managed k8s"


@contextlib.contextmanager
def short_dir():
    d = tempfile.mkdtemp(prefix="awbx", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    from awb.tcp import throttle
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)


@pytest.fixture
def buckets():
    with FakeOBS("awb-lab-eu-de", AK, SK) as lab, FakeOBS("awb", AK, SK) as own:
        yield lab, own


@pytest.fixture
def svc(home, buckets, tmp_path, monkeypatch):
    lab, own = buckets
    notes: list = []
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={lab.bucket: lab.endpoint, own.bucket: own.endpoint})
        s._notify = lambda subject, message: notes.append((subject, message))
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, ["test-1/lab/ak", "test-1/lab/sk"], reader={"test-1/lab/ak": AK,
                    "test-1/lab/sk": SK}.get, settings={"bucket_tenant": "test-1", "lab_bucket": lab.bucket,
                                                         "owner_bucket": own.bucket, "max_mb": "1"})
        s.notes = notes
        try:
            yield s
        finally:
            s.stop()


@pytest.fixture
def lab_project(home, register_path, monkeypatch):
    pr = projects.spawn(home, "lab", GOAL, None, register_path)
    monkeypatch.chdir(pr.path)
    return pr


@pytest.fixture
def cust_project(home, register_path, monkeypatch):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    monkeypatch.chdir(pr.path)
    return pr


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- the lab inbox


def test_a_take_of_a_file_in_neither_inbox_is_refused(svc, lab_project, capsys):
    code, _, err = run(["inbox", "take", "missing.md"], capsys)
    assert code == 1 and "neither inbox" in err


def test_a_clean_file_moves_from_the_lab_inbox_into_the_project(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    lab.objects["inbox/vendor-notes.md"] = b"The appliance needs two NICs and UEFI.\n"
    code, out, err = run(["inbox", "take", "vendor-notes.md"], capsys)
    assert code == 0, err
    assert (Path(lab_project.path) / "input" / "vendor-notes.md").read_bytes() == b"The appliance needs two NICs and UEFI.\n"
    assert "inbox/vendor-notes.md" not in lab.objects
    assert lab.objects["%s/in/vendor-notes.md" % lab_project.code] == b"The appliance needs two NICs and UEFI.\n"


def test_a_file_with_a_registered_name_stays_in_the_lab_inbox(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    lab.objects["inbox/notes.md"] = ("Meeting with %s about the firewall.\n" % fx.CUSTOMER_FORMS[0]).encode()
    code, out, err = run(["inbox", "take", "notes.md"], capsys)
    assert code == 0 and "held in the lab inbox" in out and "name check" in out
    assert "inbox/notes.md" in lab.objects
    assert not (Path(lab_project.path) / "input" / "notes.md").exists()
    fx.assert_no_fixture_name(out + err, "the answer of a held take")


def test_the_listing_of_the_lab_inbox_withholds_names_with_a_hit(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    lab.objects["inbox/clean.md"] = b"x"
    lab.objects["inbox/offer %s.md" % fx.CUSTOMER_FORMS[1]] = b"y"
    code, out, _ = run(["inbox", "list"], capsys)
    assert code == 0 and "clean.md" in out and "1 file(s) withheld" in out
    fx.assert_no_fixture_name(out, "the inbox listing")


def test_a_file_in_both_inboxes_is_refused(svc, buckets, lab_project, capsys):
    lab, own = buckets
    lab.objects["inbox/a.md"] = b"x"
    own.objects["inbox/a.md"] = b"x"
    code, _, err = run(["inbox", "take", "a.md"], capsys)
    assert code == 1 and "both inboxes" in err


# --------------------------------------------------------------------------- the owner inbox


def test_a_take_from_the_owner_inbox_hands_the_session_the_sanitised_copy_only(svc, buckets, cust_project, home,
                                                                             capsys):
    _, own = buckets
    original = ("Kick-off with %s: migrate the database cluster.\n" % fx.CUSTOMER_FORMS[0]).encode()
    own.objects["inbox/kickoff.txt"] = original
    code, out, err = run(["inbox", "take", "kickoff.txt"], capsys)
    assert code == 0, err
    assert "through the intake" in out
    inputs = [f for f in (Path(cust_project.path) / "input").iterdir() if f.name != ".gitkeep"]
    assert len(inputs) == 1 and inputs[0].suffix == ".md"
    text = inputs[0].read_text(encoding="utf-8")
    assert fx.CUSTOMER_CODE in text
    fx.assert_no_fixture_name(text + out + err, "what the session got from the owner inbox")
    assert "inbox/kickoff.txt" not in own.objects
    kept = [k for k in own.objects if k.endswith("/%s/in/kickoff.txt" % cust_project.code)]
    assert len(kept) == 1 and own.objects[kept[0]] == original
    assert any(home.originals.rglob("*")), "the vault keeps the original"


def test_unknown_name_candidates_hold_the_file_and_tell_the_owner_only(svc, buckets, cust_project, capsys):
    _, own = buckets
    own.objects["inbox/memo.txt"] = ("Call %s about the migration of the database.\n"
                                     % fx.PLANTED_CANDIDATE).encode()
    code, out, err = run(["inbox", "take", "memo.txt"], capsys)
    assert code == 0 and "held on the owner's side" in out
    assert fx.PLANTED_CANDIDATE not in out + err
    assert svc.notes and "name candidate" in svc.notes[-1][1]
    assert fx.PLANTED_CANDIDATE not in json.dumps(svc.notes)
    assert not [f for f in (Path(cust_project.path) / "input").iterdir() if f.name != ".gitkeep"]


def test_a_project_without_a_customer_takes_nothing_from_the_owner_inbox_without_a_code(svc, buckets, lab_project,
                                                                                    capsys):
    _, own = buckets
    own.objects["inbox/task.txt"] = b"a task"
    code, _, err = run(["inbox", "take", "task.txt"], capsys)
    assert code == 1 and "no customer" in err
    assert "inbox/task.txt" in own.objects


# --------------------------------------------------------------------------- put


def test_a_put_lands_in_from_session_and_tells_the_owner(svc, buckets, lab_project, capsys, tmp_path):
    lab, _ = buckets
    f = Path(lab_project.path) / "evidence" / "result.md"
    f.write_text("The image imported in four minutes.\n", encoding="utf-8")
    code, out, err = run(["xchg", "put", str(f)], capsys)
    assert code == 0, err
    keys_ = [k for k in lab.objects if k.startswith("%s/from-session/" % lab_project.code)]
    assert len(keys_) == 1 and keys_[0].endswith("/result.md")
    assert svc.notes and lab_project.code in svc.notes[-1][0]
    code, out, _ = run(["xchg", "list"], capsys)
    assert "result.md" in out


def test_a_put_with_a_registered_name_or_an_unchecked_picture_is_refused(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    root = Path(lab_project.path)
    (root / "evidence" / "named.md").write_text("Notes for %s.\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    code, _, err = run(["xchg", "put", str(root / "evidence" / "named.md")], capsys)
    assert code == 1 and "name check" in err
    (root / "evidence" / "shot.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="))
    code, _, err = run(["xchg", "put", str(root / "evidence" / "shot.png")], capsys)
    assert code == 1 and "--image" in err
    code, _, err = run(["xchg", "put", str(root / "evidence" / "shot.png"), "--image"], capsys)
    assert code == 1 and "reason" in err
    code, _, err = run(["xchg", "put", str(root / "evidence" / "shot.png"), "--image", "--reason",
                        "console screenshot of the image list, checked: no names, no ids"], capsys)
    assert code == 0, err
    assert not [k for k in lab.objects if k.endswith("named.md")]


def test_a_deliverable_of_a_customer_project_needs_its_review(svc, cust_project, capsys):
    d = Path(cust_project.path) / "deliverables" / "plan.md"
    d.write_text("The plan for %s.\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    code, _, err = run(["xchg", "put", str(d)], capsys)
    assert code == 1 and "send gate" in err


@pytest.mark.parametrize("req", [
    {"method": "PUT", "key": "inbox/x.md"},
    {"method": "PUT", "key": "tcp-zzzz/from-session/x.md"},
    {"method": "GET", "key": "2026-10/tcp-zzzz/in/x.md"},
    {"method": "DELETE", "key": "images/meraki-vmx/26.1.4/x.qcow2"},
    {"method": "LIST", "prefix": ""},
    {"method": "COPY", "source": "images/a", "key": "%(code)s/in/a"},
    {"method": "PUT", "key": "%(code)s/../policy"},
])
def test_object_calls_outside_the_allowed_folders_are_refused_before_signing(svc, buckets, lab_project, req, tmp_path):
    lab, _ = buckets
    req = {k: (v % {"code": lab_project.code} if isinstance(v, str) else v) for k, v in req.items()}
    before = len(lab.calls)
    upload = None
    if req["method"] == "PUT":
        upload = tmp_path / "x"
        upload.write_bytes(b"x")
    a = keys.request(svc.call_path, dict(req, op="obs", project=lab_project.code), upload=upload)
    assert not a["ok"]
    assert len(lab.calls) == before


def test_a_put_above_the_limit_is_refused(svc, lab_project, capsys):
    f = Path(lab_project.path) / "evidence" / "big.log"
    f.write_text("line of a log\n" * 100_000, encoding="utf-8")       # 1.4 MB, the limit is 1 MB
    code, _, err = run(["xchg", "put", str(f)], capsys)
    assert code in (1, 2) and "max_mb" in err


def test_no_answer_or_log_line_carries_a_key_or_an_owner_file_name(svc, buckets, cust_project, capsys, tmp_path):
    _, own = buckets
    own.objects["inbox/budget-%s.txt" % fx.CUSTOMER_FORMS[1]] = b"numbers"
    run(["inbox", "take", "budget-%s.txt" % fx.CUSTOMER_FORMS[1]], capsys)
    log = "\n".join(p.read_text() for p in (tmp_path / "log").glob("*.tsv"))
    assert AK not in log and SK not in log and fx.CUSTOMER_FORMS[1] not in log and "budget" not in log


# --------------------------------------------------------------------------- close


def test_close_waits_for_files_in_the_exchange_folders(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    lab.objects["%s/from-session/2026-10-02/result.md" % lab_project.code] = b"x"
    code, _, err = run(["close", lab_project.code], capsys)
    assert code == 2 and "from-session" in err
    with open(Path(lab_project.path) / "RESOURCES.md", "a", encoding="utf-8") as f:
        f.write("| awb-lab-eu-de/%s/ | obs folder | eu-de | small | none | kept | results for the owner |\n"
                % lab_project.code)
    code, _, err = run(["close", lab_project.code], capsys)
    assert code == 0, err


def test_the_settings_file_is_read_and_refuses_unknown_keys(tmp_path):
    f = tmp_path / "keys.conf"
    f.write_text("# exchange\nbucket_tenant = test-1\nmax_mb = 200\n", encoding="utf-8")
    s = xchg.read_settings(f)
    assert s["bucket_tenant"] == "test-1" and s["max_mb"] == "200" and s["lab_bucket"] == "awb-lab-eu-de"
    f.write_text("secret = x\n", encoding="utf-8")
    with pytest.raises(xchg.XchgError, match="line 1"):
        xchg.read_settings(f)
