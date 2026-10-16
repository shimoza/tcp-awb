"""The send gate and a project folder the owner cannot read: it refuses with the project's code, never a traceback
and never a pass; a closed project answers through the fingerprint that close writes, with the same verdict as its
files. Every text is invented; the projects are made the way tests/test_send_gate.py makes them."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from awb import codes, projects, review
from tests.test_review_board import TEXT, spawn
from tests.test_send_gate import tier3

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder of mode 000")

LONG = ("the quota of the test holds three nodes of s3.large.2 and two disks of ssd in eu-de, the backup runs "
        "every night at two and keeps seven copies, the network has one subnet and one gateway\n")


def candidates(folder: Path, out: Path) -> dict[str, Path]:
    """Files that carry a deliverable of `folder` in every way the gate knows and two that carry none."""
    import docx

    out.mkdir(exist_ok=True)
    long = folder / "deliverables" / "notes.md"
    long.write_text(LONG, encoding="utf-8")
    files = {
        "copy": TEXT,
        "changed": LONG.replace("seven", "eight"),
        "part": LONG[LONG.index("ota of"):LONG.index("keeps") + 3],
        "wrapped": "before it " + LONG + " and after it",
        "plain": "internal notes of the day\n",
        "short": "three nodes\n",
    }
    paths = {}
    for key, text in files.items():
        paths[key] = out / ("%s.md" % key)
        paths[key].write_text(text, encoding="utf-8")
    paths["bytes"] = out / "bytes.md"
    paths["bytes"].write_bytes(long.read_bytes())
    doc = docx.Document()
    doc.add_paragraph(LONG.strip())
    doc.save(str(out / "inside.docx"))
    paths["docx"] = out / "inside.docx"
    return paths


def verdicts(own: Path, files: dict[str, Path], home) -> dict[str, tuple[str, str]]:
    return {key: review.send_check(own, f, True, workbench=home) for key, f in files.items()}


def test_the_fingerprint_matches_words_as_the_text_does():
    deliverables = [LONG.split(), ["abcdefghij"], ["nodes"], ["two", "disks"], TEXT.casefold().split()]
    files = [
        LONG.split(), ["xxthe"] + LONG.split()[1:] + ["yy"], ["xxthe"] + LONG.split()[1:-1] + ["gatewayyy"],
        LONG.split()[2:12], ["ota"] + LONG.split()[2:12] + ["thr"], LONG.split()[2:4], ["cdefg"], ["cd"],
        ["abcdefghij"], ["xnodesx", "a"], ["wo", "dis"], ["two", "disksx"], ["xtwo", "disks"], ["uota"],
        [w.replace("seven", "eight") for w in LONG.split()], ["internal", "notes"], [], TEXT.casefold().split()[2:],
    ]
    seen = set()
    for d in deliverables:
        row = review._fp_of_words("0f" * 16, d)
        for f in files:
            want = review._same_text(f, d)
            assert review._fp_same_text(f, "0f" * 16, row) == want, (deliverables.index(d), files.index(f))
            seen.add(want)
    assert seen == {True, False}


def test_a_closed_project_answers_through_its_fingerprint_with_the_same_verdict(home, register_path, tmp_path,
                                                                                  monkeypatch):
    a = spawn(home, register_path)
    b = spawn(home, register_path)
    tier3(b, tmp_path)
    files = candidates(b, tmp_path / "out")
    projects.close(home, b.name, force=True)
    assert review.fingerprint_path(home, b.name).is_file()
    readable = verdicts(a, files, home)
    for key in ("copy", "changed", "part", "wrapped", "bytes", "docx"):
        assert readable[key][0] == review.SEND_REFUSE, key
    assert readable["plain"][0] == review.SEND_WARN

    real = review._readable_tree
    b_real = Path(os.path.realpath(b))

    def unreadable(root):
        if root == b_real:
            raise PermissionError(13, "denied")
        return real(root)

    monkeypatch.setattr(review, "_readable_tree", unreadable)
    assert verdicts(a, files, home) == readable
    review.fingerprint_path(home, b.name).unlink()
    v, message = review.send_check(a, files["plain"], True, workbench=home)
    assert v == review.SEND_REFUSE and b.name in message and "awb close %s" % b.name in message


@needs_permissions
def test_a_folder_of_mode_000_refuses_with_its_code(home, register_path, tmp_path):
    a = spawn(home, register_path)
    b = spawn(home, register_path)
    tier3(b, tmp_path)
    files = candidates(b, tmp_path / "out")
    readable = verdicts(a, files, home)
    taken = {pr.code for pr in projects.load(home)}
    stray = home.projects_root / codes.new_project_code(projects.PLATFORM, taken)
    (stray / "deliverables").mkdir(parents=True)
    os.chmod(b, 0o000)
    try:
        v, message = review.send_check(a, files["plain"], True, workbench=home)
        assert v == review.SEND_REFUSE and b.name in message and "\n" not in message
        os.chmod(b, 0o700)
        projects.close(home, b.name, force=True)
        os.chmod(b, 0o000)
        assert verdicts(a, files, home) == readable
        os.chmod(stray, 0o000)
        v, message = review.send_check(a, files["plain"], True, workbench=home)
        assert v == review.SEND_REFUSE and stray.name in message
    finally:
        os.chmod(b, 0o750)
        os.chmod(stray, 0o750)


@needs_permissions
def test_close_says_when_it_cannot_write_the_fingerprint(home, register_path):
    b = spawn(home, register_path)
    os.chmod(b / "deliverables", 0o000)
    try:
        pr = projects.close(home, b.name, force=True)
    finally:
        os.chmod(b / "deliverables", 0o750)
    assert pr.state == "closed" and "awb close %s" % b.name in pr.fingerprint_note
    assert not review.fingerprint_path(home, b.name).exists()
