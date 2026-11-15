"""gate review of 2026-11-15: what slipped past the commit gate, what made it slow on long input, what hung it and
what a refusal printed.

Every planted value is built from pieces so that this file passes the gate itself. Names come from
tests/fixtures.py. A slow case runs in a child process with a time limit (it imports awb from this tree), so that a
pattern that went quadratic again fails the test instead of holding the suite.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from awb import gate
from tests import fixtures

ROOT = Path(__file__).resolve().parent.parent
GH_BODY = "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
TOKEN = "gh" + "p_" + GH_BODY
VALUE = "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
PW_KEY = "pass" + "word"
HOME = "ho" + "me"
USER = "builder"


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def classes(text: str) -> list[tuple[str, int]]:
    return [(f.cls, f.line) for f in gate._scan_text("x", text, None)]


def run_child(code: str, limit: float, **kwargs) -> subprocess.CompletedProcess:
    """`code` in a fresh interpreter that imports this tree's awb; a run past `limit` seconds fails the test."""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    try:
        return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=limit, check=False, **kwargs)
    except subprocess.TimeoutExpired:
        pytest.fail("the gate took longer than %d seconds" % limit)


# --------------------------------------------------------------------------- linear time on long input

LONG_RUNS = {
    "slash-run": ("homepath", "'/' * 60000"),
    "escaped-slash-run": ("homepath", "'\\\\/' * 30000"),
    "jwt-run": ("token", "'-eyJ' * 200000"),
    "discord-run": ("token", "'-M' * 100000"),
    "userinfo-run": ("secret", "'+a' * 100000"),
    "nested-assign-run": ("secret", "'a=' * 50000"),
    "nested-colon-run": ("secret", "'a:' * 50000"),
    "xml-pair-run": ("secret", "'name=\"x\" ' * 12000"),
    "keypair-line": ("secret", "'ABCDEFGHIJ1234567890 ' * 5000"),
}


@pytest.mark.parametrize("case", list(LONG_RUNS))
def test_a_long_run_is_scanned_in_linear_time(case):
    cls, expr = LONG_RUNS[case]
    r = run_child("""
        from awb import gate
        text = %s
        print(len(gate.DETECTORS[%r](text)))
    """ % (expr, cls), limit=20)
    assert r.returncode == 0, r.stderr[-400:]


# --------------------------------------------------------------------------- lookalike letters and spellings


LOOKALIKES = {
    "cyrillic-a-in-key": ("secret", "db_p\u0430ss" + "word = \"" + VALUE + "\""),
    "fullwidth-key": ("secret", "\uff50\uff41\uff53\uff53" + "word = \"" + VALUE + "\""),
    "cyrillic-home": ("homepath", "log under /h\u043em\u0435/" + USER + "/awb.log"),
    "fullwidth-slash-home": ("homepath", "log under \uff0f" + HOME + "\uff0f" + USER + "/awb.log"),
    "cyrillic-key-id": ("token", "id \u0410K" + "IA" + "Z7QX4MLP2R9TVW3B"),
    "fullwidth-tenant": ("identifier", "tenant \uff11\uff12\uff13\uff14\uff15"),
}


@pytest.mark.parametrize("case", list(LOOKALIKES))
def test_a_lookalike_letter_does_not_hide_a_finding(case):
    cls, line = LOOKALIKES[case]
    assert classes("first\n" + line + "\n") == [(cls, 2)]


WINDOWS_HOMES = {
    "lower-backslash": "c:\\users\\" + USER + "\\notes.txt",
    "lower-user-folder": "C:\\users\\" + USER + "\\notes.txt",
    "lower-forward": "c:/users/" + USER + "/notes.txt",
    "git-bash": "/c/Users/" + USER + "/notes.txt",
    "wsl-lower": "/mnt/c/users/" + USER + "/notes.txt",
}


@pytest.mark.parametrize("case", list(WINDOWS_HOMES))
def test_windows_spellings_of_a_home_folder_are_found(case):
    assert classes("first\nopen " + WINDOWS_HOMES[case] + "\n") == [("homepath", 2)]


def test_the_windows_spellings_keep_an_api_path():
    assert classes("GET /v3/users/" + USER + "/keys\nsee /a/users/" + USER + "\n") == []


def test_a_key_nine_levels_deep_in_a_value_is_still_found():
    line = "".join("%s=" % c for c in "abcdefghij") + PW_KEY + "=" + VALUE
    assert classes(line + "\n") == [("secret", 1)]


# --------------------------------------------------------------------------- encodings


def test_a_stray_byte_does_not_hide_a_name_in_a_utf8_file(tmp_path, register_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(("x\nsupplier %s\n" % fixtures.ORG_FORMS[0]).encode("utf-8") + b"tail \xff\n")
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 2, "name")]


def test_a_stray_byte_does_not_hide_a_name_in_a_commit_message(tmp_path, register_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_bytes(("subject\n\nfor %s\n" % fixtures.ORG_FORMS[0]).encode("utf-8") + b"\xfe\n")
    got = gate.scan_message(msg, gate._load_matcher(register_path))
    assert [(x.cls, x.line) for x in got] == [("name", 3)]


# --------------------------------------------------------------------------- the commit message


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=awb-test", "-c", "user.email=awb-test@example.org",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=repo, capture_output=True, text=True, check=False)


def test_a_comment_line_git_keeps_is_scanned_for_keys(tmp_path):
    # git keeps a comment line of a message given with -m: the gate must not drop it
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q").returncode == 0
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert git(repo, "add", "a.txt").returncode == 0
    assert git(repo, "commit", "-q", "--no-verify", "-m", "subject", "-m", "# kept line").returncode == 0
    assert "# kept line" in git(repo, "log", "-1", "--format=%B").stdout

    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("subject\n\n# push with " + TOKEN + "\n# " + "db_" + PW_KEY + " = \"" + VALUE + "\"\n",
                   encoding="utf-8")
    assert [(x.cls, x.line) for x in gate.scan_message(msg, None)] == [("token", 3), ("secret", 4)]
    # git's own comment lines stay clean, and nothing after the scissors line counts
    msg.write_text("subject\n\n# On branch main\n# Changes to be committed:\n#\tnew file:   a.txt\n"
                   "# ------------------------ >8 ------------------------\n+push with " + TOKEN + "\n",
                   encoding="utf-8")
    assert gate.scan_message(msg, None) == []


# --------------------------------------------------------------------------- what a refusal prints


def test_a_refusal_never_prints_the_token_a_file_is_named_after(tmp_path, register_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q").returncode == 0
    (repo / "dir").mkdir()
    (repo / "dir" / (TOKEN + ".txt")).write_text("nothing inside\n", encoding="utf-8")
    assert git(repo, "add", "dir").returncode == 0
    code = gate.main(["--staged", "--repo", str(repo), "--register", str(register_path)])
    out, err = capsys.readouterr()
    assert code == 1
    assert GH_BODY not in out + err
    assert out.splitlines() == ["token  dir/[withheld]:0"]


def test_withheld_path_leaves_a_clean_path_alone():
    assert gate.withheld_path("notes/offer-[name].md") == "notes/offer-[name].md"
    assert gate.withheld_path("commit 0123abcd:a/" + TOKEN) == "commit 0123abcd:a/[withheld]"


# --------------------------------------------------------------------------- links and special files


def test_a_named_pipe_in_a_walked_folder_is_opaque_and_never_hangs(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no named pipes here")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "notes.txt").write_text("clean\n", encoding="utf-8")
    os.mkfifo(tree / "pipe")
    r = run_child("""
        import sys
        from awb import gate
        sys.exit(gate.main(["--no-register", %r]))
    """ % str(tree), limit=30, stdin=subprocess.DEVNULL)
    assert r.returncode == 1, r.stderr[-400:]
    assert r.stdout.splitlines() == ["opaque  %s:0" % (tree / "pipe")]


def test_the_target_of_a_link_is_scanned_as_git_commits_it(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / TOKEN).mkdir()
    (tree / "to-a-folder").symlink_to(TOKEN)                         # a link to a folder: walked past before
    (tree / "to-a-home").symlink_to("/" + HOME + "/" + USER + "/x")    # a link whose text is a home path
    (tree / "plain.txt").write_text("clean\n", encoding="utf-8")
    (tree / "to-plain").symlink_to("plain.txt")
    got = {(Path(f.file).name, f.cls) for f in gate.scan_files(gate._expand([tree]), None)}
    assert ("to-a-folder", "token") in got
    assert ("to-a-home", "homepath") in got
    assert not any(name == "to-plain" for name, _ in got)
    assert not any(name == "plain.txt" for name, _ in got)


# --------------------------------------------------------------------------- PDF


def _pdf(tmp_path: Path, text: str) -> bytes:
    pytest.importorskip("reportlab")
    if not (shutil.which("pdftotext") and shutil.which("pdfinfo")):
        pytest.skip("poppler tools missing")
    from reportlab.pdfgen import canvas

    f = tmp_path / "made.pdf"
    c = canvas.Canvas(str(f))
    for i, line in enumerate((text, "second line of the release notes", "third line of the release notes")):
        c.drawString(72, 720 - 20 * i, line)
    c.save()
    return f.read_bytes()


def test_a_pdf_is_judged_by_its_bytes_too(tmp_path):
    data = _pdf(tmp_path, "release notes for the platform")
    clean = tmp_path / "clean.pdf"
    clean.write_bytes(data)
    assert gate.scan_files([clean], None) == []
    tail = tmp_path / "tail.pdf"
    tail.write_bytes(data + b"\n% push with " + TOKEN.encode("ascii") + b"\n")
    assert [(x.cls, x.line) for x in gate.scan_files([tail], None)] == [("token", 0)]
    inside = tmp_path / "inside.pdf"
    inside.write_bytes(data.replace(b"%%EOF", b"% log under /" + HOME.encode() + b"/" + USER.encode()
                                    + b"/awb.log\n%%EOF"))
    assert [(x.cls, x.line) for x in gate.scan_files([inside], None)] == [("homepath", 0)]


# --------------------------------------------------------------------------- the structured patterns


PATTERN_RUNS = {
    "defanged-host-labels": "'a [dot] ' * 4000",
    "digits-and-dashes": "'12-' * 20000",
    "many-phone-numbers": "'call +49 30 1234567 or ' * 30000",
}


@pytest.mark.parametrize("case", list(PATTERN_RUNS))
def test_the_structured_patterns_take_linear_time_on_a_long_run(case):
    r = run_child("""
        from awb import patterns
        print(len(patterns.find_structured(%s)))
    """ % PATTERN_RUNS[case], limit=20)
    assert r.returncode == 0, r.stderr[-400:]


def test_the_bounded_patterns_still_find_a_host_a_phone_and_an_id():
    from awb import patterns

    text = "mail ops at a.b.c.d.e.f.g.h.zyxwo-logistik.de or call +49 30 1234567, request 4f1c9a7e2b6d40f8\n"
    kinds = [s.cls for s in patterns.find_structured(text)]
    assert kinds == ["url", "phone"]
