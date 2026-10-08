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


def test_a_take_from_the_owner_inbox_wipes_unknown_names_and_holds_nothing(svc, buckets, cust_project, capsys):
    """Replaces test_unknown_name_candidates_hold_the_file_and_tell_the_owner_only (wipe mode, T4): the owner take
    runs wipe mode, the name becomes a token and nobody gets a hold mail."""
    _, own = buckets
    own.objects["inbox/memo.txt"] = ("Call %s about the migration of the database.\n"
                                     % fx.PLANTED_CANDIDATE).encode()
    code, out, err = run(["inbox", "take", "memo.txt"], capsys)
    assert code == 0 and "through the intake" in out and "held" not in out
    assert fx.PLANTED_CANDIDATE not in out + err and not svc.notes
    (copy,) = [f for f in (Path(cust_project.path) / "input").iterdir() if f.name != ".gitkeep"]
    text = copy.read_text(encoding="utf-8")
    assert fx.PLANTED_CANDIDATE not in text and "[company 1]" in text


def test_a_file_that_cannot_be_read_is_held_with_the_one_line_of_the_owner(svc, buckets, cust_project, capsys):
    _, own = buckets
    own.objects["inbox/scan.png"] = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 17
    code, out, err = run(["inbox", "take", "scan.png"], capsys)
    line = "held: run awb import %s as the owner in your own terminal" % cust_project.code
    assert code == 0 and "held on the owner's side" in out and line in out
    assert svc.notes and line in svc.notes[-1][1] and fx.CUSTOMER_CODE in svc.notes[-1][1]
    assert "/" not in svc.notes[-1][1].replace("in/ folder", ""), "the mail names no path"
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



def test_a_terraform_file_without_addresses_or_names_is_put(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    d = Path(lab_project.path) / "terraform" / "https-vm"
    d.mkdir(parents=True)
    f = d / "main.tf"
    f.write_text('resource "opentelekomcloud_vpc_subnet_v1" "subnet" {\n'
                 '  vpc_id = opentelekomcloud_vpc_v1.vpc.id\n'
                 '  cidr   = opentelekomcloud_vpc_v1.vpc.cidr\n'
                 '  port   = module.loadbalancer.listener_port\n'
                 '  az     = data.opentelekomcloud_compute_availability_zone_v2.az.name\n}\n', encoding="utf-8")
    code, _, err = run(["xchg", "put", str(f)], capsys)
    assert code == 0, err
    assert [k for k in lab.objects if k.endswith("/main.tf")]
    f.write_text(f.read_text() + '# owner tobias.beispielmann@%s\n' % fx.CUSTOMER_DOMAIN, encoding="utf-8")
    code, _, err = run(["xchg", "put", str(f), "--as", "second.tf"], capsys)
    assert code == 1 and "mail" in err


# --------------------------------------------------------------------------- clean


def _put_three(lab, code):
    for key in ("2026-10-01/a.md", "2026-10-03/b.tf", "2026-10-08/c.md"):
        lab.objects["%s/from-session/%s" % (code, key)] = b"x"
    lab.objects["%s/in/keep.md" % code] = b"x"
    lab.objects["inbox/keep.md"] = b"x"


def test_clean_dry_run_deletes_nothing(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    _put_three(lab, lab_project.code)
    before = dict(lab.objects)
    res = Path(lab_project.path) / "RESOURCES.md"
    rows = res.read_text()
    code, out, err = run(["xchg", "clean", lab_project.code, "--dry-run"], capsys)
    assert code == 0, err
    assert out.count("would delete") == 3 and "--go" in out
    assert lab.objects == before and res.read_text() == rows


def test_clean_go_deletes_exactly_the_listed_keys_and_writes_the_rows(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    c = lab_project.code
    _put_three(lab, c)
    code, out, err = run(["xchg", "clean", c, "--older-than", "2026-10-08", "--go"], capsys)
    assert code == 0, err
    gone = {"%s/from-session/2026-10-01/a.md" % c, "%s/from-session/2026-10-03/b.tf" % c}
    assert not gone & set(lab.objects)
    assert {"%s/from-session/2026-10-08/c.md" % c, "%s/in/keep.md" % c, "inbox/keep.md"} <= set(lab.objects)
    rows = [l for l in (Path(lab_project.path) / "RESOURCES.md").read_text().splitlines() if "| from-session |" in l]
    assert len(rows) == 2 and all("| deleted |" in r for r in rows)
    assert {k for k in gone if any(k in r for r in rows)} == gone
    assert projects.live_resources(Path(lab_project.path)) == 0
    code, out, _ = run(["xchg", "clean", c, "--go"], capsys)
    assert code == 0 and "1 object(s) deleted" in out
    code, out, _ = run(["xchg", "clean", c, "--dry-run"], capsys)
    assert "nothing to delete" in out


def test_clean_refuses_another_code_and_needs_a_mode(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    _put_three(lab, lab_project.code)
    before = dict(lab.objects)
    code, _, err = run(["xchg", "clean", "tcp-zzzz", "--go"], capsys)
    assert code == 1 and "this project" in err
    code, _, _ = run(["xchg", "clean", lab_project.code], capsys)
    assert code == 2
    code, _, err = run(["xchg", "clean", lab_project.code, "--older-than", "last week", "--go"], capsys)
    assert code == 1 and "YYYY-MM-DD" in err
    assert lab.objects == before


@pytest.mark.parametrize("key", ["tcp-zzzz/from-session/2026-10-01/a.md", "%(code)s/from-session/",
                                 "%(code)s/from-session/../in/keep.md", "%(code)s/other/a.md", "2026-10/%(code)s/in/a"])
def test_a_delete_outside_the_projects_own_prefix_is_refused_by_the_service(svc, buckets, lab_project, key):
    lab, _ = buckets
    _put_three(lab, lab_project.code)
    lab.objects["tcp-zzzz/from-session/2026-10-01/a.md"] = b"x"
    before = (dict(lab.objects), len(lab.calls))
    a = keys.request(svc.call_path, {"op": "obs", "method": "DELETE", "key": key % {"code": lab_project.code},
                                     "project": lab_project.code})
    assert not a["ok"]
    assert (dict(lab.objects), len(lab.calls)) == before

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


# --- the web mode: the owner's materials service reads both buckets, never writes ---------------------------

def _web(svc, **obj):
    return keys.request(svc.call_path, {"op": "web_read", **obj})


def test_web_read_lists_names_months_and_reads_a_version_without_writing(svc, buckets, cust_project, tmp_path):
    lab, own = buckets
    month = "%s/%s/in/" % (cust_project.created[:7], cust_project.code)
    lab.client().put_bytes("inbox/brief.md", b"a task without customer content")
    own.client().put_bytes("inbox/drop.pdf", b"unassigned")
    own.client().put_bytes(month + "scope.docx", b"customer scope")
    own.client().put_bytes("notes/readme.txt", b"not a month folder")
    writes = lambda: [m for m, _ in lab.calls + own.calls if m in ("PUT", "DELETE")]
    before = writes()
    assert [r["key"] for r in _web(svc, bucket="lab", what="list", prefix="inbox/")["objects"]] == ["inbox/brief.md"]
    assert [r["key"] for r in _web(svc, bucket="owner", what="list", prefix="inbox/")["objects"]] == ["inbox/drop.pdf"]
    rows = _web(svc, bucket="owner", what="list", prefix=month)["objects"]
    assert [r["key"] for r in rows] == [month + "scope.docx"] and rows[0]["modified"] and rows[0]["etag"]
    assert _web(svc, bucket="owner", what="months")["months"] == [cust_project.created[:7] + "/"]
    out = tmp_path / "copy"
    answer = keys.request(svc.call_path, {"op": "web_read", "bucket": "owner", "what": "get", "key": month + "scope.docx",
                                          "etag": rows[0]["etag"]}, download=out)
    assert answer["ok"] and answer["etag"] == rows[0]["etag"] and out.read_bytes() == b"customer scope"
    assert own.client().head(month + "scope.docx") is not None
    assert writes() == before


def test_web_read_refuses_other_folders_versions_and_sizes(svc, buckets, cust_project, tmp_path):
    lab, own = buckets
    month = "%s/%s/in/" % (cust_project.created[:7], cust_project.code)
    own.client().put_bytes(month + "big.bin", b"x" * 2048)
    etag = _web(svc, bucket="owner", what="list", prefix=month)["objects"][0]["etag"]
    for obj in ({"bucket": "lab", "what": "list", "prefix": month}, {"bucket": "owner", "what": "list", "prefix": ""},
                {"bucket": "owner", "what": "list", "prefix": "2026-10/tcp-zzzz/in/"},
                {"bucket": "owner", "what": "list", "prefix": "inbox/../"},
                {"bucket": "owner", "what": "list", "prefix": month + "sub/"},
                {"bucket": "lab", "what": "months"}, {"bucket": "other", "what": "list", "prefix": "inbox/"},
                {"bucket": "owner", "what": "get", "key": month, "etag": etag},
                {"bucket": "owner", "what": "get", "key": month + "big.bin"},
                {"bucket": "owner", "what": "put", "key": month + "big.bin"},
                {"bucket": "owner", "what": "delete", "key": month + "big.bin"}):
        answer = _web(svc, **obj)
        assert answer["ok"] is False and answer["kind"] == "refused", obj
    stale = _web(svc, bucket="owner", what="get", key=month + "big.bin", etag="0" * 32)
    assert (stale["ok"], stale["kind"]) == (False, "changed")
    small = _web(svc, bucket="owner", what="get", key=month + "big.bin", etag=etag, limit=1024)
    assert (small["ok"], small["kind"]) == (False, "too_large")
    assert own.client().head(month + "big.bin") is not None


def test_web_read_is_for_the_owner_only_and_waits_for_the_unlock(svc, buckets, monkeypatch, tmp_path):
    lab, _ = buckets
    lab.client().put_bytes("inbox/brief.md", b"text")
    real = keys.os.getuid()
    monkeypatch.setattr(keys.os, "getuid", lambda: real + 1)
    assert _web(svc, bucket="lab", what="list", prefix="inbox/") == {"ok": False, "kind": "refused", "error": "refused"}
    monkeypatch.setattr(keys.os, "getuid", lambda: real)
    keys.request(svc.admin_path, {"op": "lock"})
    answer = _web(svc, bucket="lab", what="list", prefix="inbox/")
    assert (answer["ok"], answer["kind"]) == (False, "locked")
    log = "\n".join(p.read_text() for p in (tmp_path / "log").glob("*.tsv"))
    assert "inbox/brief.md" not in log and AK not in log and SK not in log


# --------------------------------------------------------------------------- which file he means


def _ids(text):
    import re
    return re.findall(r"(?:lab|own)-[0-9a-f]{12}", text)


def test_the_words_he_uses_for_a_file():
    rels = ["Angebot.pdf", "Sizing.xlsx", "deck/Overview.pptx", "notes.md", "Angebot alt.pdf"]
    modified = ["2026-10-01T08:00:00.000Z", "2026-10-02T08:00:00.000Z", "2026-10-03T08:00:00.000Z",
                "2026-10-04T08:00:00.000Z", "2026-09-01T08:00:00.000Z"]

    def m(words):
        return xchg.match(words, rels, modified)
    assert m("ANGEBOT.PDF") == [0]                                  # the same letters and digits
    assert m("эксель") == m("the excel file") == m("Tabelle") == [1]
    assert m("презентацию") == m("Präsentation") == m("overview") == [2]
    assert m("pdf") == [0, 4] and m("newest pdf") == m("последний пдф") == [0]
    assert m("последний") == [3]
    assert m("sizing|angebot alt") == [1, 4]
    assert m("contract") == [] and m("файл") == []


def test_his_words_find_the_file_without_its_exact_name(svc, buckets, cust_project, capsys):
    _, own = buckets
    own.objects["inbox/All_blocks.TXT"] = b"The migration plan of the database cluster.\n"
    code, out, err = run(["inbox", "take", "all-blocks.txt"], capsys)
    assert code == 0, err
    assert "taken from the owner inbox through the intake" in out
    assert not [k for k in own.objects if k.startswith("inbox/")]


def test_a_description_by_kind_takes_the_one_file_of_that_kind(svc, buckets, cust_project, capsys):
    _, own = buckets
    own.objects["inbox/kickoff notes.md"] = b"Notes of the kick-off: two clusters move.\n"
    own.objects["inbox/sizing.csv"] = b"flavor,count\ns3.large.2,4\n"
    code, out, err = run(["inbox", "take", "эксель"], capsys)
    assert code == 0, err
    assert out.startswith("CSV of") and "inbox/sizing.csv" not in own.objects
    assert "inbox/kickoff notes.md" in own.objects


def test_none_or_several_list_both_inboxes_and_never_an_owner_name(svc, buckets, cust_project, capsys):
    lab, own = buckets
    lab.objects["inbox/vendor-guide.png"] = b"x" * 10
    own.objects["inbox/offer %s.txt" % fx.CUSTOMER_FORMS[1]] = b"y" * 3000
    own.objects["inbox/notes.txt"] = b"The notes of the workshop about the network.\n"
    code, _, err = run(["inbox", "take", "презентация"], capsys)
    assert code == 1 and "neither inbox" in err and len(_ids(err)) == 3
    assert "vendor-guide.png" in err and err.count("name not shown") == 2
    code, listed, _ = run(["inbox", "list"], capsys)
    assert code == 0 and len(_ids(listed)) == 3
    code, _, several = run(["inbox", "take", "txt"], capsys)
    assert code == 1 and "2 files match" in several and len(_ids(several)) == 2
    raw = json.dumps([keys.request(svc.call_path, dict(q, op="inbox_find", project=cust_project.code))
                      for q in ({"words": "txt"}, {"all": True}, {})])
    for text in (err, listed, several, raw):
        fx.assert_no_fixture_name(text, "the list of the inboxes")
        assert "offer" not in text and "notes.txt" not in text
    line = next(row for row in several.splitlines() if "owner inbox" in row and " bytes " in row)
    code, out, err = run(["inbox", "take", "--id", _ids(line)[0]], capsys)
    assert code == 0, err
    assert "inbox/notes.txt" not in own.objects and "inbox/offer %s.txt" % fx.CUSTOMER_FORMS[1] in own.objects


def test_all_takes_every_file_of_both_inboxes(svc, buckets, cust_project, capsys):
    lab, own = buckets
    lab.objects["inbox/vendor.md"] = b"The appliance needs two NICs.\n"
    own.objects["inbox/kickoff.txt"] = ("Kick-off with %s: two clusters move.\n" % fx.CUSTOMER_FORMS[0]).encode()
    code, out, err = run(["inbox", "take", "--all"], capsys)
    assert code == 0, err
    assert "taken from the lab inbox: input/vendor.md" in out and "through the intake" in out
    assert not [k for k in list(lab.objects) + list(own.objects) if k.startswith("inbox/")]
    fx.assert_no_fixture_name(out, "the answer of a take of all files")


def test_a_file_in_a_subfolder_of_the_lab_inbox_is_taken(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    lab.objects["inbox/meraki/readme.md"] = b"Boot the image with UEFI.\n"
    code, out, err = run(["inbox", "take", "readme"], capsys)
    assert code == 0, err
    assert (Path(lab_project.path) / "input" / "readme.md").exists()
    assert lab.objects["%s/in/readme.md" % lab_project.code] == b"Boot the image with UEFI.\n"
    assert "inbox/meraki/readme.md" not in lab.objects


def test_a_find_leaves_no_owner_name_in_the_log(svc, buckets, cust_project, capsys, tmp_path):
    _, own = buckets
    own.objects["inbox/budget-%s.txt" % fx.CUSTOMER_FORMS[1]] = b"numbers"
    run(["inbox", "list"], capsys)
    run(["inbox", "take", "budget"], capsys)
    log = "\n".join(p.read_text() for p in (tmp_path / "log").glob("*.tsv"))
    assert fx.CUSTOMER_FORMS[1] not in log and "budget" not in log
