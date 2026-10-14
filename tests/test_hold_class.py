"""The held line with a class word (P2 of F3): held (grant), held (size), held (unreadable), held (folder). The word
names the kind of refusal and no value; a missing grant names awb projects kind, the cure the import is not.

The key service and the stand-in buckets come from tests/test_xchg.py; every name is an invented fixture name.
"""
from __future__ import annotations

from awb import codes
from awb.tcp import xchg
from tests import fixtures as fx
from tests.test_xchg import _grant, buckets, cust_project, lab_project, run, svc  # noqa: F401 (fixtures)

TASK = b"Migrate the database cluster.\n"


def test_a_take_for_a_customer_project_never_granted_names_the_grant(svc, buckets, cust_project, capsys):
    _, own = buckets
    own.objects["inbox/task.txt"] = TASK
    code, out, err = run(["inbox", "take", "the text"], capsys)
    assert code == 1
    assert err.strip() == "held (grant): run awb projects kind %s query as the owner in your own terminal" \
        % cust_project.code
    assert "inbox/task.txt" in own.objects and not svc.notes


def test_a_file_over_the_limit_is_held_with_the_class_size_and_no_number(svc, buckets, cust_project, capsys):
    _, own = buckets
    _grant(capsys, cust_project.code)
    own.objects["inbox/big.txt"] = b"line of a log\n" * 100_000          # 1.4 MB, the limit is 1 MB
    code, out, err = run(["inbox", "take", "the text"], capsys)
    assert code == 1
    assert err.strip() == "held (size): run awb import %s as the owner in your own terminal" % cust_project.code
    assert "inbox/big.txt" in own.objects


def test_a_folder_of_another_customer_is_held_with_the_class_folder(svc, buckets, cust_project, capsys):
    _, own = buckets
    _grant(capsys, cust_project.code)
    own.objects["inbox/%s/task.txt" % fx.PARTNER_CODE.replace("PART", "CUST")] = TASK
    code, out, err = run(["inbox", "take", "the text"], capsys)
    assert code == 1
    assert err.strip() == "held (folder): run awb import %s as the owner in your own terminal" % cust_project.code


def test_a_project_without_a_customer_keeps_the_plain_line(svc, buckets, lab_project, capsys):
    _, own = buckets
    own.objects["inbox/task.txt"] = TASK
    code, _, err = run(["inbox", "take", "the text"], capsys)
    assert code == 1 and err.strip() == codes.HOLD_LINE % lab_project.code


def test_every_class_line_names_a_command_and_no_value():
    for cls in codes.HOLD_CLASSES:
        line = xchg.hold_line("tcp-q7m4", cls, "project")
        assert codes.HOLD_LINE_RE.match(line) and line.startswith("held (%s): run awb " % cls)
        assert line.endswith("as the owner in your own terminal")
        assert not any(ch.isdigit() for ch in line.replace("tcp-q7m4", ""))
    assert xchg.hold_line("tcp-q7m4", "grant", "query") == \
        "held (grant): run awb projects kind tcp-q7m4 query as the owner in your own terminal"
    assert xchg.hold_line("tcp-q7m4", "grant") == \
        "held (grant): run awb projects kind tcp-q7m4 project|query as the owner in your own terminal"
    assert xchg.hold_line("tcp-q7m4", "anything") == codes.HOLD_LINE % "tcp-q7m4"
