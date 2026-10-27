"""Task GT (his decision of 2026-10-27): customer material is checked once, at the entry. After it nothing stops his
work: a put never refuses a file for its review state, a name or structured data; a secret is replaced with
[secret] in the uploaded copy; the stop hook asks nothing about a review; the work rules say that a review runs
only on his word.

Every text is invented; the only names are those of tests/fixtures.py.
"""
from __future__ import annotations

import datetime
import secrets
from pathlib import Path

import pytest

from awb import bucket, hooks, projects, putcopy, rulesync, writing
from awb.tcp import keys
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_hooks import project, run_hook  # noqa: F401 (fixture)
from tests.test_review_board import PLATFORM_LENS, reviewed, spawn
from tests.test_xchg import buckets, cust_project, lab_project, quiet, run, svc  # noqa: F401 (fixtures)

TEXT = "The cluster runs 3 nodes of s3.large.2 in eu-de.\n"
TODAY = datetime.date.today().isoformat()


def secret_value() -> str:
    """A value the gate takes for a secret, made at run time so that no test file holds one."""
    return "Qx" + secrets.token_hex(10) + "Zr7"


def with_secret(value: str) -> str:
    key = "_".join(("db", "pass" + "word"))
    return "The plan.\n%s = %s\nOne more line.\n" % (key, value)


@pytest.fixture
def fake():
    with FakeOBS() as f:
        yield f


def put(home, fake, code: str, f: Path) -> tuple[str, list[str]]:
    said: list[str] = []
    key, _ = bucket.put(home, fake.client(), code, f, replace=True, report=said)
    return key, said


# --------------------------------------------------------------------------- awb bucket put


def test_gt_a_put_of_an_unreviewed_or_stale_deliverable_succeeds_with_review_none(home, register_path, tmp_path,
                                                                                    fake):
    folder = spawn(home, register_path)
    draft = folder / "deliverables" / "draft.md"
    draft.write_text(TEXT, encoding="utf-8")
    key, said = put(home, fake, folder.name, draft)
    assert key.endswith("/out/draft.md") and fake.objects[key] == draft.read_bytes()
    assert said == ["review: none"]
    done = reviewed(folder, tmp_path, 1, name="done.md")
    assert put(home, fake, folder.name, done)[1] == ["review: passed %s" % TODAY]
    copy = tmp_path / "copy.md"
    copy.write_bytes(done.read_bytes())
    assert put(home, fake, folder.name, copy)[1] == ["review: passed %s" % TODAY]
    done.write_text(TEXT + "One more line.\n", encoding="utf-8")
    key, said = put(home, fake, folder.name, done)
    assert key and said == ["review: none"]


def test_gt_a_tier_3_deliverable_without_a_request_check_and_a_named_file_are_put(home, register_path, tmp_path,
                                                                                  fake):
    folder = spawn(home, register_path)
    full = reviewed(folder, tmp_path, 3, name="full.md", lenses=[("platform-tcp", PLATFORM_LENS)])
    key, said = put(home, fake, folder.name, full)
    assert key and said == ["review: passed %s" % TODAY]
    named = tmp_path / "notes.md"
    named.write_text("Notes for %s, mail ops@%s.\n" % (fx.CUSTOMER_FORMS[0], fx.CUSTOMER_DOMAIN), encoding="utf-8")
    key, said = put(home, fake, folder.name, named)
    assert key and said == ["review: none"] and fake.objects[key] == named.read_bytes()


def test_gt_a_secret_is_replaced_in_the_uploaded_copy_and_counted(home, register_path, fake):
    folder = spawn(home, register_path)
    value = secret_value()
    f = folder / "deliverables" / "plan.md"
    f.write_text(with_secret(value), encoding="utf-8")
    key, said = put(home, fake, folder.name, f)
    sent = fake.objects[key].decode("utf-8")
    assert value not in sent and "[secret]" in sent and "One more line." in sent
    assert value in f.read_text(encoding="utf-8")                   # the file itself stays as it was
    assert said == ["review: none; 1 secret replaced"]


def test_gt_scrub_handles_text_office_and_binary_files(tmp_path):
    import zipfile

    value = secret_value()
    office = tmp_path / "a.docx"
    with zipfile.ZipFile(office, "w") as z:
        z.writestr("word/document.xml", "<w:t>api_" + "key=" + value + "</w:t>")
    out = tmp_path / "out.docx"
    assert putcopy.scrub(office, out) == 1
    with zipfile.ZipFile(out) as z:
        assert value not in z.read("word/document.xml").decode("utf-8")
    raw = tmp_path / "a.bin"
    raw.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    assert putcopy.scrub(raw, tmp_path / "b.bin") == 0
    assert (tmp_path / "b.bin").read_bytes() == raw.read_bytes()
    assert putcopy.scrub_text("no secret here\n") == ("no secret here\n", 0)


# --------------------------------------------------------------------------- awb xchg put and the key service


def test_gt_a_session_put_of_a_customer_deliverable_succeeds_and_replaces_the_secret(svc, buckets, cust_project,
                                                                                     capsys):
    lab, _ = buckets
    value = secret_value()
    d = Path(cust_project.path) / "deliverables" / ("plan %s.md" % fx.CUSTOMER_FORMS[0])
    d.write_text(with_secret(value) + "For %s.\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    code, out, err = run(["xchg", "put", str(d)], capsys)
    assert code == 0, err
    assert "review: none; 1 secret replaced" in err.splitlines()
    sent = [v for k, v in lab.objects.items() if "/from-session/" in k]
    assert len(sent) == 2 and all(value.encode() not in v and b"[secret]" in v for v in sent)   # md and its HTML
    subject, message = svc.notes[-1]
    fx.assert_no_fixture_name(subject + message, "the put mail")


def test_gt_the_key_service_replaces_a_secret_of_a_raw_put_and_takes_a_name(svc, buckets, lab_project, tmp_path):
    lab, _ = buckets
    up = tmp_path / "x.md"
    value = secret_value()
    up.write_text(with_secret(value) + "Notes for %s.\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    key = "%s/from-session/2026-10-27/put-0123456789ab.md" % lab_project.code
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": lab_project.code, "key": key,
                                     "name": "notes.md"}, upload=up)
    assert a["ok"] and a["secrets"] == 1, a
    assert value.encode() not in lab.objects[key] and fx.CUSTOMER_FORMS[0].encode() in lab.objects[key]


# --------------------------------------------------------------------------- hooks


def test_gt_the_stop_hook_asks_nothing_about_a_review(project, monkeypatch, capsys):  # noqa: F811
    (project / "deliverables" / "offer.md").write_text("The landing zone is ready.\n", encoding="utf-8")
    (project / "deliverables" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 0 and "review" not in err and "tier" not in err


def test_gt_the_name_of_a_deliverable_blocks_nothing(project, monkeypatch, capsys):  # noqa: F811
    name = "lever" + "age-plan.md"
    assert writing.check_name(name), "control: the writing check reads the name as a tell"
    f = project / "deliverables" / name
    f.write_text("The landing zone is ready.\n", encoding="utf-8")
    assert hooks._writing_tells(f, "tcp") == []


# --------------------------------------------------------------------------- the work rules


OLD_PUT = ("- Files for him leave with `awb xchg put FILE` (the lab bucket, from-session/<date>/), after the name check "
           "and, for a customer project, the review. Name every deliverable `tcp-q7m4-<kind word>-<language>.<ext>` "
           "from the start (`tcp-q7m4-mapping-de.md`), never with words of the material.")
OLD_ADD = ("- Files for him leave with `awb xchg put FILE` (the lab bucket, from-session/<date>/), after the name check "
           "and, for a customer project, the review.")


def test_gt_the_template_carries_the_review_line_and_not_the_old_put_condition():
    text = projects._claude("tcp-q7m4", "engagement", fx.CUSTOMER_CODE)
    assert text.count(projects.REVIEW_ON_WORD) == 1
    assert "for a customer project, the review" not in text
    assert projects.REVIEW_ON_WORD in projects.FINAL_CHECK_SECTION


@pytest.mark.parametrize("put_line", [OLD_PUT, OLD_ADD])
def test_gt_the_sync_takes_the_old_put_condition_out_and_adds_the_review_line_once(tmp_path, put_line):
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    lines = projects._claude("tcp-q7m4", "lab", projects.NO_CUSTOMER).split("\n")
    lines = [line for line in lines if line != projects.REVIEW_ON_WORD]
    at = next(i for i, line in enumerate(lines) if line.startswith("- Files for him leave with"))
    lines[at] = put_line
    (root / "CLAUDE.md").write_text("\n".join(lines), encoding="utf-8")
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "for a customer project, the review" not in text and "tiers" in text
    assert text.count(projects.REVIEW_ON_WORD) == 1
    assert rulesync.sync_project(root) == []
    assert rulesync.current(root)


# --------------------------------------------------------------------------- the review levels


def test_gt_the_template_carries_both_review_levels_with_their_workflows():
    text = projects._claude("tcp-q7m4", "lab", projects.NO_CUSTOMER)
    lines = text.split("\n")
    one = next(line for line in lines if "Level 1," in line)
    two = next(line for line in lines if line.startswith("- Level 2"))
    assert '"ревью"' in one and '"review"' in one and "workflows/review.js" in one
    assert "workflows/request-check.js" in one and "never blocks a put" in one
    assert '"глубокое ревью"' in two and '"deep review"' in two and "workflows/final-check.js" in two
    assert "Fable 5.1 at effort max" in two and '"финальная вычитка"' in two
    assert any("200-300k tokens" in line and "wait for one word" in line for line in lines)
    assert "Neither level runs on its own" in text and "## Final check (FX)" not in text
    assert not [line for line in lines if line in (projects.FX_PHRASE_OLD, projects.FX_SCRIPT_LINE,
                                                    projects.FX_AFTER_OLD)]


@pytest.mark.parametrize("old_path, with_rv2", [(False, True), (True, False)])
def test_gt_the_sync_turns_the_old_fx_section_into_the_review_levels_once(tmp_path, old_path, with_rv2):
    now = projects._claude("tcp-q7m4", "lab", projects.NO_CUSTOMER)
    head = now[:now.index("## Review levels")]
    head = head.replace(projects.LAB_LOGS + "\n", "")
    script = projects.FX_SCRIPT_LINE
    if old_path:      # the FX line before T6 named /opt/tcp-awb/workflows
        script = script.replace("/opt/tcp-awb/src/workflows/", "/opt/tcp-awb/workflows/")
    section = (projects.FX_HEADING_OLD, "", projects.FX_PHRASE_OLD, script, projects.FX_AFTER_OLD)
    old = head + "\n".join(section + ((projects.FX_REVIEW_LINE,) if with_rv2 else ())) + "\n"
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    (root / "CLAUDE.md").write_text(old, encoding="utf-8")
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == now
    assert rulesync.sync_project(root) == [] and rulesync.current(root)


# --------------------------------------------------------------------------- logs of our own lab runs


def lab_log() -> tuple[str, dict]:
    """A log of an invented lab run: an auth call, a token, a temporary credential and ids, all made at run time."""
    import base64
    import json

    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    v = {
        "domain": secrets.token_hex(16), "project": secrets.token_hex(16), "request": secrets.token_hex(16),
        "token": "MII" + secrets.token_urlsafe(300), "pw": "Zq" + secrets.token_hex(6) + "!x",
        "ak": "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(20)),
        "sk": secrets.token_urlsafe(30)[:40], "st": secrets.token_urlsafe(200),
        "jwt": "%s.%s.%s" % (b64({"alg": "RS256", "typ": "JWT"}), b64({"sub": "lab", "exp": 1}),
                             secrets.token_urlsafe(40)),
    }
    pw_key = "pass" + "word"
    auth = {"auth": {"identity": {"methods": [pw_key], pw_key: {"user": {
        "name": "lab-user", pw_key: v["pw"], "domain": {"id": v["domain"]}}}}}}
    cred = {"credential": {"access": v["ak"], "secret": v["sk"], "securitytoken": v["st"]}}
    text = ("POST /v3/auth/tokens\nX-Request-Id: %s\n%s\nX-Subject-Token: %s\nproject_id: %s\n%s\n"
            "jwt claims {\"sub\": \"lab\"}\nid_token: %s\nGET https://iam.eu-de.otc.t-systems.com/v3/projects ip "
            "192.0.2.10 mail ops@example.org\n"
            % (v["request"], json.dumps(auth), v["token"], v["project"], json.dumps(cred), v["jwt"]))
    return text, v


def test_gt_a_lab_log_keeps_its_ids_and_shortens_its_temporary_credentials():
    from awb import gate, labsecrets

    text, v = lab_log()
    out, counts = labsecrets.shorten(text)
    for kept in ("domain", "project", "request"):
        assert v[kept] in out
    for gone in ("token", "pw", "ak", "sk", "st", "jwt"):
        assert v[gone] not in out
    assert labsecrets.short(v["token"]) in out and v["token"][:8] + "...(" in out
    assert labsecrets.short(v["ak"]) in out and '"name": "[login]"' in out and '"[secret]"' in out
    assert "192.0.2.10" in out and "ops@example.org" in out and "iam.eu-de.otc.t-systems.com" in out
    assert not [f for f in gate._scan_text("x", out, None) if f.cls in labsecrets.GATE_CLASSES]
    perm = "ak = %s\nsk = %s\n" % (v["ak"], v["sk"])
    out, _ = labsecrets.shorten(perm)
    assert v["ak"] not in out and v["ak"][:8] not in out and out.count("[secret]") == 2


def post_write(path: Path) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": str(path)}}


def test_gt_the_write_hook_keeps_ids_and_shortens_secrets_in_a_project_file(project, monkeypatch,  # noqa: F811
                                                                             capsys):
    text, v = lab_log()
    f = project / "evidence" / "auth-call.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    code, out, err = run_hook("post-write", post_write(f), monkeypatch, capsys)
    assert code == 0, err
    now = f.read_text(encoding="utf-8")
    assert v["domain"] in now and v["request"] in now and v["token"] not in now and v["sk"] not in now
    assert "shortened" in out
    f.write_text("The run for %s.\n" % fx.CUSTOMER_FORMS[1], encoding="utf-8")
    code, _, err = run_hook("post-write", post_write(f), monkeypatch, capsys)
    assert code == 2 and "name check" in err, "a registered name still blocks"


def test_gt_the_commit_gate_of_a_project_passes_ids_and_still_refuses_a_secret(project, monkeypatch,  # noqa: F811
                                                                               capsys):
    from awb import gate

    f = project / "evidence" / "ids.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("domain_id %s\nrequest %s\n" % (secrets.token_hex(16), secrets.token_hex(16)), encoding="utf-8")
    outside = project.parent.parent / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    g = outside / "ids.log"
    g.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    assert gate.in_project(project / "evidence") and not gate.in_project(outside)
    monkeypatch.chdir(outside)
    assert gate.main(["--no-register", str(g)]) == 1, "control: the gate reads the ids as identifiers"
    capsys.readouterr()
    monkeypatch.chdir(project)
    assert gate.main(["--no-register", str(f)]) == 0
    f.write_text(with_secret(secret_value()), encoding="utf-8")
    assert gate.main(["--no-register", str(f)]) == 1


def test_gt_paste_own_keeps_ids_shortens_secrets_and_refuses_a_name(project):  # noqa: F811
    from awb import paste

    text, v = lab_log()
    path, n = paste.own(text, project)
    now = path.read_text(encoding="utf-8")
    assert v["project"] in now and "192.0.2.10" in now and v["token"] not in now and n >= 5
    assert v["token"][:8] + "...(" in now and v["ak"] not in now and v["sk"] not in now
    with pytest.raises(paste.PasteRefused, match="registered name"):
        paste.own("The run for %s.\n" % fx.CUSTOMER_FORMS[1], project)


def test_gt_the_template_carries_the_lab_log_line_and_the_sync_adds_it_once(tmp_path):
    text = projects._claude("tcp-q7m4", "lab", projects.NO_CUSTOMER)
    assert text.count(projects.LAB_LOGS) == 1
    assert projects.LAB_LOGS in [new for _, new, _ in rulesync.CLAUDE_ADD]
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    (root / "CLAUDE.md").write_text(text.replace(projects.LAB_LOGS + "\n", ""), encoding="utf-8")
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == text


def test_gt_a_refused_paste_without_a_name_goes_in_with_own_retry_and_ids_stay(project, monkeypatch,  # noqa: F811
                                                                                capsys):
    import io
    import sys

    from awb import paste

    text, v = lab_log()
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert paste.main([]) == 2, "control: the entry check refuses a mail and a secret"
    err = capsys.readouterr().err
    stamp = err.split("awb paste --retry ")[1].split("`")[0]
    assert paste.main(["--own", "--retry", stamp]) == 0
    out = capsys.readouterr().out
    assert "own output:" in out and paste.kept(stamp) is None
    written = next((project / "notes" / "pastes").iterdir()).read_text(encoding="utf-8")
    assert "ops@example.org" in written and v["domain"] in written and v["token"] not in written
