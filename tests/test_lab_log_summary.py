"""Q-0021: a lab log over lablog.LIMIT leaves git (its full text in a git-ignored full/ folder, a summary with its
SHA-256 committed) and the commit gate refuses a text file over gate.TEXT_LIMIT at once with a hint, instead of
scanning it for many minutes. Every project, run, resource and value here is invented."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from awb import config, gate, lablog, labclient, winlabsession
from awb.tcp import board
from tests.fixtures import CUSTOMER_CODE, TENANT_ONE
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import LAB, NOT_LAB, PROJECT, fake, svc  # noqa: F401

RUN = "lr-20261120-0001"
SMALL = 4096


def _project(tmp_path: Path, git: bool = True) -> Path:
    root = tmp_path / "projects" / PROJECT
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# %s\n\n- code: %s\n- customer: %s\n" % (PROJECT, PROJECT, CUSTOMER_CODE),
                                   encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _block(n: int, method: str = "GET", status: int = 200, recorded: str | None = None) -> str:
    lines = ["## 2026-11-20T10:%02d:%02dZ %s ecs /v1/{project_id}/cloudservers" % (n // 60 % 60, n % 60, method),
             "answer: %d" % status, "answer body:", "x" * 300]
    if recorded:
        lines.append("recorded: %s" % recorded)
    return "\n".join(lines) + "\n\n"


def _ignored(root: Path, rel: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "check-ignore", "-q", rel]).returncode == 0


# --------------------------------------------------------------------------- the writer


def test_q21_a_small_log_stays_in_place_with_no_summary_and_no_ignore_line(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    assert lablog.append(root, log, _block(1)) == log
    assert log.read_text(encoding="utf-8") == _block(1)
    assert not (log.parent / lablog.FULL).exists()
    assert not (log.parent / (RUN + lablog.SUMMARY_SUFFIX)).exists()
    assert not (root / ".gitignore").exists()
    assert lablog.refresh(root, log) is None


def test_q21_a_log_over_the_limit_moves_to_full_and_git_keeps_a_summary_with_its_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    folder = root / "notes" / "lab-runs"
    log = folder / (RUN + ".log")
    lablog.append(root, log, "# lab run %s of %s on %s, opened 2026-11-20T09:59:00Z\nnote: -\n\n" % (RUN, PROJECT, LAB))
    n = 1
    where = log
    while where == log:
        where = lablog.append(root, log, _block(n, "POST" if n == 2 else "GET", 201 if n == 2 else 200,
                                                "ecs server sv-%d" % n if n == 2 else None))
        n += 1
    full = folder / lablog.FULL / log.name
    assert where == full and full.is_file() and not log.exists()
    text = full.read_text(encoding="utf-8")
    assert text.startswith("# lab run %s" % RUN) and text.count("## 2026-11-20T") == n - 1
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "notes/lab-runs/full/" in lines and lablog.IGNORE_NOTE in lines
    assert _ignored(root, "notes/lab-runs/full/%s.log" % RUN)
    summary = (folder / (RUN + lablog.SUMMARY_SUFFIX)).read_text(encoding="utf-8")
    assert not _ignored(root, "notes/lab-runs/%s%s" % (RUN, lablog.SUMMARY_SUFFIX))
    assert "- full log: `notes/lab-runs/full/%s.log`" % RUN in summary
    assert "- sha256: %s" % hashlib.sha256(full.read_bytes()).hexdigest() in summary
    assert "- size: %d bytes" % full.stat().st_size in summary
    assert "- start: 2026-11-20T09:59:00Z" in summary
    assert "- calls: %d" % (n - 1) in summary
    assert "- errors: 0" in summary
    assert "- created resources: 1" in summary and "- ecs server sv-2" in summary
    assert "(last entry so far)" in summary
    assert len(summary) < 2048

    # later blocks go to the full log; a refresh writes the summary again with the new hash
    lablog.append(root, log, _block(n, status=404))
    lablog.append(root, log, "refused: the key service refused the call\n\n")
    assert not log.exists()
    assert lablog.refresh(root, log) == folder / (RUN + lablog.SUMMARY_SUFFIX)
    summary = (folder / (RUN + lablog.SUMMARY_SUFFIX)).read_text(encoding="utf-8")
    assert "- sha256: %s" % hashlib.sha256(full.read_bytes()).hexdigest() in summary
    assert "- errors: 2" in summary and "- calls: %d" % n in summary
    assert (root / ".gitignore").read_text(encoding="utf-8").count("notes/lab-runs/full/") == 1


def test_q21_the_ignore_line_joins_an_existing_gitignore_once(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    (root / ".gitignore").write_text("input/opaque/", encoding="utf-8")       # no newline at the end
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    lablog.append(root, log, _block(1) * 20)
    lablog.ignore(root, "notes/lab-runs")
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "input/opaque/" and lines.count("notes/lab-runs/full/") == 1


def test_q21_a_descriptor_open_before_the_move_writes_on_into_the_full_log(tmp_path, monkeypatch):
    """The key service holds a descriptor of the run log during an OBS job: the move is a rename, so its last line
    lands in the full log, not in a file that is gone."""
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    lablog.append(root, log, _block(1))
    held = os.open(log, os.O_WRONLY | os.O_APPEND)
    try:
        lablog.append(root, log, _block(2) * 20)
        os.write(held, b"job line of the key service\n")
    finally:
        os.close(held)
    full = log.parent / lablog.FULL / log.name
    assert full.read_text(encoding="utf-8").endswith("job line of the key service\n")


def test_q21_a_log_written_beside_its_full_log_is_merged_into_it(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    lablog.append(root, log, _block(1) * 20)
    log.write_text("stray block of a late writer\n", encoding="utf-8")
    log.chmod(0o600)
    lablog.append(root, log, "next block\n")
    full = log.parent / lablog.FULL / log.name
    assert not log.exists()
    assert full.read_text(encoding="utf-8").endswith("stray block of a late writer\nnext block\n")


def test_q21_a_linked_full_folder_or_full_log_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    folder = root / "notes" / "lab-runs"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / lablog.FULL).symlink_to(outside, target_is_directory=True)
    log = folder / (RUN + ".log")
    with pytest.raises(OSError):
        lablog.append(root, log, _block(1) * 20)
    assert not list(outside.iterdir())
    (folder / lablog.FULL).unlink()
    (folder / lablog.FULL).mkdir()
    target = tmp_path / "target.log"
    target.write_text("unchanged", encoding="utf-8")
    (folder / lablog.FULL / log.name).symlink_to(target)
    with pytest.raises(OSError):
        lablog.append(root, log, _block(2))
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_q21_the_summary_lists_at_most_max_listed_creates_and_counts_all(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    monkeypatch.setattr(lablog, "MAX_LISTED", 3)
    root = _project(tmp_path)
    log = root / "notes" / "lab-runs" / (RUN + ".log")
    for n in range(1, 21):
        lablog.append(root, log, _block(n, "POST", 201, "vpc vpc vpc-%d, vpc subnet sn-%d" % (n, n)))
    lablog.refresh(root, log)
    summary = (log.parent / (RUN + lablog.SUMMARY_SUFFIX)).read_text(encoding="utf-8")
    assert "- created resources: 40" in summary
    assert len(re.findall(r"(?m)^- vpc ", summary)) == 3 and "- and 37 more (in the full log)" in summary


def test_q21_a_lab_run_through_the_key_service_commits_only_the_summary(svc, fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    assert labclient.run_main(["start", "--tenant", LAB, "--note", "sizing"]) == 0
    run = (root / "notes" / "lab-runs" / ("%s.open" % LAB)).read_text().strip()
    status, _h, _b = labclient.call("POST", "iam", "/v3/users", body={"user": {"name": "q21-user"}})
    assert status == 201
    for _ in range(12):
        labclient.call("GET", "iam", "/v3/echo", params={"pad": "p" * 400})
    with pytest.raises(labclient.LabClientError):
        labclient.call("GET", "iam", "/v3/users", tenant=NOT_LAB, run=run)
    assert labclient.run_main(["end"]) == 0
    capsys.readouterr()
    folder = root / "notes" / "lab-runs"
    full = folder / lablog.FULL / (run + ".log")
    assert full.is_file() and not (folder / (run + ".log")).exists()
    text = full.read_text(encoding="utf-8")
    assert "POST iam /v3/users" in text and "end of run" in text and "no lab tenant" in text
    summary = (folder / (run + lablog.SUMMARY_SUFFIX)).read_text(encoding="utf-8")
    assert "- sha256: %s" % hashlib.sha256(full.read_bytes()).hexdigest() in summary
    assert "(last entry so far)" not in summary
    assert "- calls: 14 " in summary and "- errors: 1" in summary
    assert "- created resources: 1" in summary and "iam users" in summary
    assert re.search(r"(?m)^- start: \d{4}-\d{2}-\d{2}T", summary)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--name-only"], check=True,
                            capture_output=True, text=True).stdout.split()
    assert "notes/lab-runs/%s%s" % (run, lablog.SUMMARY_SUFFIX) in staged and ".gitignore" in staged
    assert not [p for p in staged if p.endswith(".log")]
    assert gate.main(["--staged", "--no-register", "--repo", str(root)]) == 0


def test_q21_a_windows_lab_log_over_the_limit_moves_too(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root = _project(tmp_path)
    path = root / "notes" / "winlab" / "jobs" / "job-0001.log"
    for n in range(20):
        winlabsession._append(path, ["## 2026-11-20T11:00:%02dZ 300 bytes" % n, "y" * 300])
    full = path.parent / lablog.FULL / path.name
    assert full.is_file() and not path.exists()
    assert "notes/winlab/jobs/full/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    summary = (path.parent / ("job-0001" + lablog.SUMMARY_SUFFIX)).read_text(encoding="utf-8")
    assert "- full log: `notes/winlab/jobs/full/job-0001.log`" in summary


# --------------------------------------------------------------------------- the gate


def test_q21_the_gate_refuses_a_text_file_over_the_limit_at_once_with_the_hint(tmp_path, monkeypatch, capsys):
    big = tmp_path / "notes" / "lab-runs" / (RUN + ".log")
    big.parent.mkdir(parents=True)
    with open(big, "wb") as fh:
        line = b"## 2026-11-20T10:00:00Z GET ecs /v1/{project_id}/cloudservers answer 200\n"
        fh.write(line * (gate.TEXT_LIMIT // len(line) + 2))
    assert big.stat().st_size > gate.TEXT_LIMIT

    def no_scan(*args, **kwargs):
        raise AssertionError("a text over the limit was scanned")

    with monkeypatch.context() as m:
        m.setattr(gate, "_scan_decoded", no_scan)
        m.setattr(gate, "_scan_text", no_scan)
        assert [(f.cls, f.line) for f in gate._scan_bytes(str(big), big.read_bytes(), None)] == [("too-large", 0)]
    capsys.readouterr()
    assert gate.main(["--no-register", str(big)]) == 1
    out, err = capsys.readouterr()
    assert re.fullmatch(r"too-large  \S+:0\n", out), out
    hint = [x for x in err.splitlines() if "over 8 MiB" in x]
    assert len(hint) == 1 and "lab log over 2 MiB" in hint[0] and "awb/lablog.py" in hint[0]


def test_q21_a_lab_log_under_the_limit_keeps_the_secret_only_rule(tmp_path):
    log = tmp_path / "notes" / "lab-runs" / (RUN + ".log")
    log.parent.mkdir(parents=True)
    home = "/ho" + "me/builder/run.sh"
    log.write_text("## 2026-11-20T10:00:00Z GET ecs /x\nscript under %s\n" % home, encoding="utf-8")
    assert gate.scan_files([log], None) == []
    secret = "db_pass" + "word = \"" + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc" + "\"\n"
    log.write_text("## 2026-11-20T10:00:00Z GET ecs /x\n" + secret, encoding="utf-8")
    assert [f.cls for f in gate.scan_files([log], None)] == ["secret"]


def test_q21_an_opaque_file_over_the_limit_stays_opaque_and_a_smaller_text_is_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "TEXT_LIMIT", 1024)
    binary = tmp_path / "disk.img"
    binary.write_bytes(b"\x00" * 4096)
    assert [f.cls for f in gate.scan_files([binary], None)] == ["opaque"]
    text = tmp_path / "notes.md"
    text.write_text("plain line for %s on %s\n" % (CUSTOMER_CODE, TENANT_ONE) * 10, encoding="utf-8")
    assert text.stat().st_size < 1024 and gate.scan_files([text], None) == []
    text.write_text("plain line\n" * 200, encoding="utf-8")
    assert [f.cls for f in gate.scan_files([text], None)] == ["too-large"]


def test_q21_too_large_is_a_gate_class_the_board_and_the_dataset_know():
    assert "too-large" in gate.CLASSES
    assert set(board.GATE_CLASSES) == set(gate.CLASSES)
    assert board.build_state_word("red: gate too-large") == "red: gate too-large"
    source = (Path(gate.__file__).parent / "tcp" / "dataset.py").read_text(encoding="utf-8")
    assert '"opaque", "too-large")' in source
    assert lablog.LIMIT < gate.TEXT_LIMIT
