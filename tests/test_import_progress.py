"""`awb import` says what it does: a first line before the slow part, one line per file on standard error (the file
id and the seconds, never a name), and the import of a set of small Markdown files stays within a time bound."""

from __future__ import annotations

import base64
import random
import re
import time

import pytest

from awb import bucket, config, importcmd, intake, projects, start
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"
LINE = re.compile(r"file (\d+) of (\d+): (F-[A-Z0-9]{4}), (\d+\.\d) s")
WORDS = ("network gateway tunnel subnet router balancer cluster storage backup restore latency policy rollout "
         "window release ticket review budget quota region zone volume snapshot").split()
SET_SIZE = 13
SMALL, LARGE = 1024, 16 * 1024


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def synthetic_set(folder, count: int = SET_SIZE, seed: int = 7) -> list:
    """`count` Markdown files of 1 to 16 KB with headings, tables, code blocks, base64 blocks and one registered
    form per paragraph: the shape of a real import, invented words only."""
    rnd = random.Random(seed)
    files = []
    for i in range(count):
        size = SMALL + (LARGE - SMALL) * i // max(count - 1, 1)
        parts = ["# notes %d" % i, ""]
        k = 0
        while sum(len(x) + 1 for x in parts) < size:
            k += 1
            kind = k % 5
            if kind == 0:
                parts += ["## section %d" % k, ""]
            elif kind == 1:
                parts += [" ".join(rnd.choice(WORDS) for _ in range(40)) + ". " + fx.CUSTOMER_FORMS[0] + " agreed.", ""]
            elif kind == 2:
                parts += ["| host | role | note |", "|---|---|---|"]
                parts += ["| h%d | %s | %s |" % (j, rnd.choice(WORDS), rnd.choice(WORDS)) for j in range(6)] + [""]
            elif kind == 3:
                parts += ["```", "terraform apply -var zone=%s" % rnd.choice(WORDS), "kubectl get pods", "```", ""]
            else:
                blob = base64.b64encode(bytes(rnd.getrandbits(8) for _ in range(300))).decode()
                parts += [blob[n:n + 76] for n in range(0, len(blob), 76)] + [""]
        f = folder / ("note-%02d.md" % i)
        f.write_text("\n".join(parts)[:size] + "\n", encoding="utf-8")
        files.append(f)
    return files


def _project(home, capsys):
    it = iter(["\n"])
    assert start.start(home, "project", GOAL, "new", read=lambda: next(it, ""), tty=True) == 0
    (pr,) = projects.load(home)
    capsys.readouterr()
    return pr


def test_the_import_prints_a_first_line_and_one_line_per_file_on_standard_error_without_a_name(home, capsys, tmp_path):
    pr = _project(home, capsys)
    files = []
    for i in range(3):
        f = tmp_path / ("%s-%d.md" % (fx.PERSON_FORMS[1], i))
        f.write_text("notes %d for %s, agreed with %s.\n" % (i, fx.CUSTOMER_FORMS[0], fx.PERSON_FORMS[0]),
                     encoding="utf-8")
        files.append(f)
    assert importcmd.run(home, pr.code, files, tty=True) == 0
    captured = capsys.readouterr()
    lines = captured.err.strip().splitlines()
    assert lines[0] == "checking 3 files"
    found = [LINE.fullmatch(x) for x in lines[1:]]
    assert all(found), lines
    assert [(m.group(1), m.group(2)) for m in found] == [("1", "3"), ("2", "3"), ("3", "3")]
    assert len({m.group(3) for m in found}) == 3
    for name in fx.ALL_REGISTERED + [f.name for f in files]:
        assert name not in captured.err, "a line on standard error names an original"
    assert "file 1 of 3" not in captured.out, "the progress is on standard error, not on standard output"


def test_a_single_file_says_checking_1_file(home, capsys, tmp_path):
    pr = _project(home, capsys)
    f = tmp_path / "one.md"
    f.write_text("a short note.\n", encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True) == 0
    assert capsys.readouterr().err.splitlines()[0] == "checking 1 file"


def test_the_synthetic_set_of_13_markdown_files_imports_within_the_time_bound(home, tmp_path):
    files = synthetic_set(tmp_path)
    assert len(files) == SET_SIZE and all(SMALL <= f.stat().st_size <= LARGE + 1 for f in files)
    lines: list[str] = []
    started = time.monotonic()
    res = intake.run(files, fx.CUSTOMER_CODE, home, progress=lines.append)
    elapsed = time.monotonic() - started
    assert len([o for o in res.outputs if o.name != "intake-report.md"]) == SET_SIZE
    assert not res.held
    assert len(lines) == SET_SIZE + 1
    # measured about 7 s on a laptop-class core (it was about 21 s before the hot spots were fixed); the bound is
    # generous for a slow runner and still far below the 90 s of the report
    assert elapsed < 30, "the set took %.1f s" % elapsed
