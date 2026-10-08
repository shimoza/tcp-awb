"""The `awb` command through cli.main(argv): exit codes and what it prints.

Every command's standard output and standard error is collected. Only `awb register list --forms` may print a
written form. Everything else prints codes, classes, counts, states and paths made of codes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from awb import cli, codes, projects, register
from tests import fixtures as fx

FULL, SHORT = fx.CUSTOMER_FORMS[0], fx.CUSTOMER_FORMS[1]
INVENTED = [fx.PLANTED_CANDIDATE, fx.PLANTED_PERSON]


class Console:
    """Runs commands and keeps everything they printed, for the final check over all of it."""

    def __init__(self, capsys):
        self.capsys = capsys
        self.seen: list[str] = []

    def run(self, *argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out, err = self.capsys.readouterr()
        self.seen.append(out + err)
        return code, out, err

    def assert_clean(self) -> None:
        text = "\n".join(self.seen)
        fx.assert_no_fixture_name(text, "command output")
        low = text.lower()
        assert not [n for n in INVENTED if n.lower() in low], "an invented unregistered name was printed"


@pytest.fixture
def con(capsys, home):
    return Console(capsys)


def test_init_creates_both_places(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AWB_SHARED", str(tmp_path / "s"))
    monkeypatch.setenv("AWB_VAULT", str(tmp_path / "v"))
    monkeypatch.setenv("AWB_PROJECTS", str(tmp_path / "p"))
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "register: 0 forms of 0 codes" in out
    reg = tmp_path / "v" / "register.tsv"
    assert register.load(reg) == [] and reg.read_text(encoding="utf-8").startswith("code\tkind\tform")
    assert oct(reg.stat().st_mode & 0o777) == oct(0o600)
    for d in ("inbox", "originals", "reports"):
        assert (tmp_path / "v" / d).is_dir()
    assert (tmp_path / "s" / "outbox").is_dir()
    # a second init keeps the register as it is
    register.add(reg, fx.CUSTOMER_CODE, "CUST", FULL)
    assert cli.main(["init"]) == 0
    assert "register: 1 forms of 1 codes" in capsys.readouterr().out


def test_register_commands(con, home):
    code, out, _ = con.run("register", "new", "CUST")
    assert code == 0
    fresh = out.strip()
    assert codes.is_code(fresh) and codes.kind_of(fresh) == "CUST"
    assert fresh not in register.codes(register.load(home.register))

    code, out, _ = con.run("register", "new", "PERS", "--parent", fx.CUSTOMER_CODE)
    assert code == 0 and out.strip() == fx.CUSTOMER_CODE + "-PERS-2"

    sub = fx.CUSTOMER_CODE + "-PERS-2"
    code, out, _ = con.run("register", "add", sub, "PERS", fx.PLANTED_PERSON)
    assert code == 0 and sub in out
    assert any(e.form == fx.PLANTED_PERSON for e in register.load(home.register))

    # a wrong kind is refused with a reason and nothing is written
    before = home.register.read_bytes()
    code, _, err = con.run("register", "add", fresh, "ORG", fx.PLANTED_CANDIDATE)
    assert code == 2 and "kind" in err
    assert home.register.read_bytes() == before

    code, out, _ = con.run("register", "list")
    assert code == 0
    assert fx.CUSTOMER_CODE in out and sub in out and "PART-RET2" in out
    row = next(line for line in out.splitlines() if line.startswith(fx.CUSTOMER_CODE + " "))
    assert row.split()[1:] == ["CUST", str(len(fx.CUSTOMER_FORMS)), "0"]

    code, out, _ = con.run("register", "retire", sub)
    assert code == 0 and "retired 1 forms" in out
    code, _, err = con.run("register", "retire", "CUST-ZZZZ")
    assert code == 2

    code, _, _ = con.run("register", "new", "NOPE")
    assert code == 2
    code, _, _ = con.run("register", "add", fx.CUSTOMER_CODE)
    assert code == 2
    con.assert_clean()

    # the vault side view: forms only with --forms
    code, out, _ = con.run("register", "list", "--forms")
    con.seen.pop()
    assert code == 0 and FULL in out and fx.PLANTED_PERSON in out


def test_register_add_reads_the_form_from_stdin(con, home, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(fx.PLANTED_PERSON + "\n"))
    code, out, _ = con.run("register", "add", fx.CUSTOMER_CODE + "-PERS-2", "PERS", "-")
    assert code == 0
    assert any(e.form == fx.PLANTED_PERSON for e in register.load(home.register))
    con.assert_clean()


def _plant(home) -> list[Path]:
    a = home.inbox / ("%s-Bericht.txt" % SHORT)
    a.write_text("bericht zu %s, Server 198.51.100.8, Partner %s\n" % (FULL, fx.PLANTED_CANDIDATE), encoding="utf-8")
    b = home.inbox / "zweite.txt"
    b.write_text("rueckfrage an %s zum Load Balancer\n" % fx.PERSON_FORMS[0], encoding="utf-8")
    return [a, b]


def test_intake_wipes_then_checks(con, home):
    """Replaces test_intake_blocked_then_forced_then_checked (wipe mode, T4): no stop and no --force; the copies of
    a new customer pass the name check."""
    files = _plant(home)
    code, out, _ = con.run("intake", "--customer", "new", *map(str, files))
    assert code == 0, out
    assert "2 files, 2 outputs" in out and "wiped company 1" in out
    cust = re.search(r"\bCUST-[A-Z2-7]{4}\b", out).group(0)
    outputs = sorted((home.outbox / cust).glob("F-*.md"))
    assert len(outputs) == 2
    assert list(home.inbox.iterdir()) == []
    code, _, err = con.run("intake", "--customer", cust, "--force")
    assert code == 2, "--force is gone"

    code, out, _ = con.run("check", "--register", str(home.register), *map(str, outputs))
    assert code == 0 and out == ""
    code, out, _ = con.run("check", *map(str, outputs), str(home.outbox / cust / "intake-report.md"))
    assert code == 0

    probe = home.shared / "probe.txt"
    probe.write_text("notiz: %s am Standort %s\n" % (FULL, fx.PLACE_FORMS[0]), encoding="utf-8")
    code, out, _ = con.run("check", str(probe))
    assert code == 1
    hits = [json.loads(line) for line in out.splitlines()]
    assert [h["cls"] for h in hits] == ["name", "name"]
    assert all(set(h) == {"file", "start", "length", "cls"} for h in hits)
    con.assert_clean()


def test_intake_usage_and_errors(con, home):
    f = home.inbox / "a.txt"
    f.write_text("nur Terraform\n", encoding="utf-8")
    code, _, err = con.run("intake", str(f))
    assert code == 2 and "--customer" in err
    code, _, err = con.run("intake", "--customer", fx.ORG_CODE, str(f))
    assert code == 2 and "CUST" in err
    code, _, err = con.run("intake", "--customer", fx.CUSTOMER_CODE, str(home.inbox / ("%s.txt" % SHORT)))
    assert code == 2 and "file 1 of 1" in err
    f.unlink()
    code, _, err = con.run("intake", "--customer", fx.CUSTOMER_CODE)
    assert code == 2 and "inbox is empty" in err
    con.assert_clean()


def test_gate_selftest_and_findings(con, home):
    code, out, _ = con.run("gate", "--selftest")
    assert code == 0 and "selftest passed" in out
    clean = home.shared / "clean.md"
    clean.write_text("release notes for %s in tcp-q7m4\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    code, out, _ = con.run("gate", "--register", str(home.register), str(clean))
    assert code == 0 and out == ""
    dirty = home.shared / "dirty.md"
    dirty.write_text("first line\nlog under /ho" "me/builder/awb.log and %s\n" % SHORT, encoding="utf-8")
    code, out, _ = con.run("gate", str(dirty))
    assert code == 1
    assert sorted(line.split()[0] for line in out.splitlines()) == ["homepath", "name"]
    assert all(line.endswith(":2") for line in out.splitlines())
    code, _, _ = con.run("gate")
    assert code == 2
    con.assert_clean()


def test_spawn_and_projects_list(con, home):
    code, out, _ = con.run("projects", "list")
    assert code == 0 and "no projects" in out
    code, out, _ = con.run("spawn", "engagement", "--goal", "Design the landing zone for the first workload",
                           "--customer", fx.CUSTOMER_CODE, "--tag", "network")
    assert code == 0, out
    pcode = re.search(r"\btcp-[a-z2-7]{4}\b", out).group(0)
    assert (home.projects_root / pcode / "SCOPE.md").is_file()
    code, out, _ = con.run("projects", "list")
    assert code == 0 and pcode in out and fx.CUSTOMER_CODE in out
    assert "Design the landing zone for the first workload" in out
    code, out, _ = con.run("projects", "list", "--paths")
    assert code == 0 and str(home.projects_root / pcode) in out

    # a name in the goal is refused. The refusal does not repeat it
    code, _, err = con.run("spawn", "lab", "--goal", "Migration for %s" % FULL)
    assert code == 2 and "name check" in err
    code, _, err = con.run("spawn", SHORT, "--goal", "anything at all")
    assert code == 2 and "kind" in err
    code, _, _ = con.run("spawn", "lab")
    assert code == 2
    assert len(projects.load(home)) == 1
    con.assert_clean()


def test_usage_errors_exit_2(con):
    assert con.run()[0] == 2
    assert con.run("nosuchcommand")[0] == 2
    assert con.run("register")[0] == 2
    assert con.run("projects")[0] == 2
    code, _, err = con.run("register", "list", "--bogus", SHORT)
    assert code == 2
    assert con.run("--help")[0] == 0
    con.assert_clean()


def test_python_dash_m_awb_runs_the_command(home):
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    res = subprocess.run([sys.executable, "-m", "awb", "gate", "--selftest"], capture_output=True, text=True,
                         env=env, cwd=root, timeout=120)
    assert res.returncode == 0 and "selftest passed" in res.stdout
    res = subprocess.run([sys.executable, "-m", "awb", "register", "list"], capture_output=True, text=True,
                         env=env, cwd=root, timeout=120)
    assert res.returncode == 0 and fx.CUSTOMER_CODE in res.stdout
    fx.assert_no_fixture_name(res.stdout + res.stderr, "command output")


def test_register_retire_never_echoes_its_argument(con, home):
    before = home.register.read_bytes()
    for arg in (FULL, SHORT, "Migration " + fx.PLANTED_CANDIDATE):
        code, _, err = con.run("register", "retire", arg)
        assert code == 2 and "not a code" in err
    assert home.register.read_bytes() == before
    con.assert_clean()


# --------------------------------------------------------------------------- awb register keep (release 2)

KEPT = "Technisches Konzept"


def test_register_keep_takes_any_phrase_and_prints_no_value(con, home, monkeypatch):
    import io
    import stat

    phrases = [FULL, fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE, "Migration " + fx.PLANTED_CANDIDATE, fx.CUSTOMER_CODE,
               "a | b # c (d)"]
    for n, phrase in enumerate(phrases, start=1):
        code, out, err = con.run("register", "keep", phrase)
        assert code == 0 and out == "kept one phrase, %d in the keep list\n" % n and err == ""
    code, out, _ = con.run("register", "keep", FULL.upper())
    assert code == 0 and out == "the phrase was kept before, %d in the keep list\n" % len(phrases)
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEPT + "\n"))
    code, out, _ = con.run("register", "keep", "-")
    assert code == 0 and "%d in the keep list" % (len(phrases) + 1) in out and KEPT not in out
    con.assert_clean()

    # the keep list lives in the vault, mode 600, one phrase per line
    assert home.keep_list.parent == home.vault
    assert stat.S_IMODE(home.keep_list.stat().st_mode) == 0o600
    assert home.keep_list.read_text(encoding="utf-8").splitlines() == phrases + [KEPT]
    # the register is untouched by it
    assert not any(e.form == fx.PLANTED_PERSON for e in register.load(home.register))


def test_register_keep_usage_errors_never_echo(con, home):
    for argv in (("register", "keep"), ("register", "keep", fx.PLANTED_PERSON, "--list"),
                 ("register", "keep", "   "), ("register", "keep", fx.PLANTED_PERSON, SHORT)):
        code, out, err = con.run(*argv)
        assert code == 2, argv
    assert not home.keep_list.exists()
    con.assert_clean()


def test_register_keep_list_prints_the_phrases_on_the_vault_side_only(con, home):
    code, out, _ = con.run("register", "keep", "--list")
    assert code == 0 and out == "the keep list is empty\n"
    con.run("register", "keep", fx.PLANTED_PERSON)
    con.run("register", "keep", KEPT)
    code, out, _ = con.run("register", "keep", "--list")
    con.seen.pop()   # the vault side view: phrases on purpose
    assert code == 0 and out.splitlines() == [fx.PLANTED_PERSON, KEPT]
    con.assert_clean()

    if os.geteuid() == 0:
        pytest.skip("root reads the vault whatever its mode")
    # where the vault cannot be read (a working session after the seal) nothing is printed
    os.chmod(home.vault, 0)
    try:
        code, out, err = con.run("register", "keep", "--list")
    finally:
        os.chmod(home.vault, 0o700)
    assert code == 2 and out == "" and "vault side" in err
    con.assert_clean()


def test_a_kept_phrase_is_never_wiped_in_the_next_intake(con, home):
    """Replaces test_a_kept_phrase_is_no_candidate_in_the_next_intake (wipe mode, T4): wiped first, kept after."""
    f = home.inbox / "konzept.txt"
    f.write_text("anbei das angebot der %s, bitte pruefen.\n" % fx.PLANTED_CANDIDATE, encoding="utf-8")
    code, out, _ = con.run("intake", "--customer", fx.CUSTOMER_CODE, str(f))
    assert code == 0 and "wiped company 1" in out
    code, out, _ = con.run("register", "keep", fx.PLANTED_CANDIDATE)
    assert code == 0 and fx.PLANTED_CANDIDATE not in out
    f.write_text("anbei das angebot der %s, bitte pruefen.\n" % fx.PLANTED_CANDIDATE, encoding="utf-8")
    code, out, _ = con.run("intake", "--customer", fx.CUSTOMER_CODE, str(f))
    assert code == 0 and "wiped nothing" in out
    outputs = sorted((home.outbox / fx.CUSTOMER_CODE).glob("F-*.md"))
    assert any(fx.PLANTED_CANDIDATE in o.read_text(encoding="utf-8") for o in outputs)
    con.assert_clean()


# --------------------------------------------------------------------------- Release 2: the delegated commands


def test_help_lists_every_command(con):
    code, out, _ = con.run("--help")
    assert code == 0
    for name in list(cli.DELEGATED) + ["init", "intake", "register", "spawn", "projects"]:
        assert re.search(r"(?m)^\s+%s\s" % re.escape(name), out), name
    for name in cli.DELEGATED:
        assert "awb %s" % name in cli.__doc__, name
    con.assert_clean()


@pytest.mark.parametrize("name", sorted(cli.DELEGATED))
def test_every_delegated_command_runs(con, name):
    code, out, err = con.run(name, "--help")
    assert code == 0, err[-300:]
    assert out.startswith("usage: awb %s" % name)
    code, out, err = con.run(name)
    assert code in (1, 2) and "Traceback" not in out + err
    con.assert_clean()


def test_init_leaves_an_encrypted_register_alone(con, home):
    data = b"an encrypted register"
    home.register.unlink()
    home.register_encrypted.write_bytes(data)
    code, out, _ = con.run("init")
    assert code == 0
    assert "register: encrypted" in out
    assert not home.register.exists(), "init must not write a plaintext register next to the encrypted one"
    assert home.register_encrypted.read_bytes() == data
    con.assert_clean()


def test_register_add_of_a_customer_opens_its_outbox_so_the_work_side_can_spawn(con, home, register_path,
                                                                                 monkeypatch):
    code, out, _ = con.run("register", "new", "CUST")
    fresh = out.strip()
    assert not (home.outbox / fresh).exists()
    code, _, _ = con.run("register", "add", fresh, "CUST", fx.PLANTED_CANDIDATE)
    assert code == 0 and (home.outbox / fresh).is_dir()
    # the work side cannot read the register: the outbox folder is what lets spawn take the code
    monkeypatch.setattr(projects, "_readable_here", lambda *a: False)
    pr = projects.spawn(home, "lab", "a first look at the workloads", fresh, register_path)
    assert pr.customer == fresh
