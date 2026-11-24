"""Side sessions, the asks of 2026-11-24: tcp-bj3o-1 (a plain take and then take --merge, and a second and a third
merge, no longer conflict: SIDE-RESULT.md never enters the main branch, the result is merged as
notes/side-NAME.md) and tcp-bytg-1 (awb calc in a side worktree records into the record of its project, so the
main session and a side session never give one K id twice)."""
from __future__ import annotations

import subprocess
import time

from awb import calc, side
from tests.test_side import _git, _wait, proj, side_register  # noqa: F401


def _wait_for(root, name, limit=30.0):
    """_wait of test_side reads the first line only; this one waits for the line of NAME."""
    end = time.time() + limit
    while time.time() < end:
        lines = [x for x in side.status_lines(root) if x.split()[0] == name]
        if lines and lines[0].split()[1] == "done":
            return
        time.sleep(0.1)
    raise AssertionError(side.status_lines(root))


def test_a_plain_take_and_then_take_merge_merges(proj):
    side.start("one", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "one")
    side.take("one", cwd=proj)
    assert (proj / "notes" / "side-one.md").exists()
    out = side.take("one", merge=True, cwd=proj)
    assert "merged side/one" in out
    assert "notes/side-one.md" in _git(proj, "ls-files").split()
    assert _git(proj, "status", "--porcelain") == ""


def test_three_side_merges_in_a_row_do_not_conflict_and_leave_no_side_result(proj):
    for name in ("a", "b", "c"):
        side.start(name, "notes/brief-mail.md", cwd=proj)
        _wait_for(proj, name)
        wt = proj / ".side" / name
        (wt / side.RESULT).write_text("result of the side work %s\n" % name)
        _git(wt, "add", side.RESULT)
        r = subprocess.run(["git", "-C", str(wt), "commit", "-qm", "result " + name], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    for name in ("a", "b", "c"):
        assert "merged side/%s" % name in side.take(name, merge=True, cwd=proj)
    files = _git(proj, "ls-files").split()
    assert side.RESULT not in files and not (proj / side.RESULT).exists()
    assert {"notes/side-a.md", "notes/side-b.md", "notes/side-c.md"} <= set(files)
    assert (proj / "notes" / "side-c.md").read_text() == "result of the side work c\n"
    assert _git(proj, "status", "--porcelain") == ""


def test_a_conflict_on_another_file_still_aborts_and_keeps_the_side(proj):
    side.start("x", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "x")
    (proj / "deliverables").mkdir()
    (proj / "deliverables" / "report.md").write_text("the main report\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "main report")
    try:
        side.take("x", merge=True, cwd=proj)
    except side.SideError as err:
        assert "conflicts" in str(err)
    else:
        raise AssertionError("the merge went through")
    assert (proj / ".side" / "x").is_dir()
    assert _git(proj, "status", "--porcelain") == "?? notes/side-x.md\n"
    assert (proj / "deliverables" / "report.md").read_text() == "the main report\n"


def test_calc_in_a_side_worktree_records_into_the_project(proj):
    side.start("k", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "k")
    wt = proj / ".side" / "k"
    first = calc.record(proj, "1+1", [], "2")
    second = calc.record(wt, "2+2", [], "4")
    third = calc.record(proj, "3+3", [], "6")
    assert [first.id, second.id, third.id] == ["K-1", "K-2", "K-3"]
    assert not (wt / "calc").exists()
    assert calc.find(wt, "K-2").result == "4" and calc.find(proj, "K-2").result == "4"


def test_calc_in_a_plain_folder_named_like_a_side_stays_there(tmp_path):
    folder = tmp_path / "p" / ".side" / "k"
    folder.mkdir(parents=True)
    rec = calc.record(folder, "1+2", [], "3")
    assert rec.id == "K-1" and (folder / "calc" / "calc.tsv").exists()
