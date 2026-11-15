"""The send gate after its review of 2026-11-15: it fails closed. An unreadable project folder, a fifo, a device, a
linked review folder, a malformed project register, an unlistable projects root, a secret, a file that changes during the check and any error of the check refuse, never a traceback, never a
read that blocks and never a pass; a refusal never echoes a folder name. Every name comes from tests/fixtures.py;
the projects are made the way tests/test_send_gate.py makes them."""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from awb import codes, projects, review
from tests import fixtures as fx
from tests.test_review_board import PLATFORM_LENS, REQUEST_LENS, TEXT, reviewed, spawn
from tests.test_send_gate import tier3

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder of mode 000")
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no fifo on this system")

WAIT = 15


def within(fn, fifo: Path | None = None):
    """fn() in a thread; its result, or the string "blocked" when it is still running after WAIT seconds (then the
    fifo gets a writer and closes it, so that the reader wakes up)."""
    out: list = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    t.join(WAIT)
    if t.is_alive():
        if fifo is not None:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        t.join(WAIT)
        return "blocked"
    return out[0]


def plain(tmp_path: Path) -> Path:
    f = tmp_path / "plain.md"
    f.write_text("internal notes of the day\n", encoding="utf-8")
    return f


@needs_permissions
def test_an_unreadable_own_project_folder_refuses_and_never_raises(home, register_path, tmp_path):
    a = spawn(home, register_path)
    f = plain(tmp_path)
    os.chmod(a, 0o000)
    try:
        for wb in (None, home):
            v, message = review.send_check(a, f, True, workbench=wb)
            assert v == review.SEND_REFUSE and "\n" not in message
    finally:
        os.chmod(a, 0o750)


@needs_fifo
def test_a_fifo_is_refused_without_blocking(home, register_path, tmp_path):
    a = spawn(home, register_path)
    fifo = tmp_path / "pipe.md"
    os.mkfifo(fifo)
    res = within(lambda: review.send_check(a, fifo, True, workbench=home), fifo)
    assert res != "blocked" and res[0] == review.SEND_REFUSE


def test_a_device_is_refused(home, register_path):
    a = spawn(home, register_path)
    assert os.path.exists(os.devnull)
    assert review.send_check(a, Path(os.devnull), False)[0] == review.SEND_REFUSE


@needs_fifo
def test_a_fifo_among_the_lenses_does_not_block_the_gate(home, register_path, tmp_path):
    a = spawn(home, register_path)
    d = tier3(a, tmp_path)
    assert review.send_check(a, d, True)[0] == review.SEND_OK
    fifo = review.review_dir(a, d.name) / review.LENSES / "0.json"
    os.mkfifo(fifo)
    res = within(lambda: review.send_check(a, d, True), fifo)
    assert res != "blocked" and res[0] == review.SEND_OK


def test_a_file_that_changes_during_the_check_is_judged_by_the_bytes_hashed(home, register_path, tmp_path,
                                                                             monkeypatch):
    a = spawn(home, register_path)
    f = tmp_path / "notes.md"
    f.write_text("internal notes about %s\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    real = review._match_deliverable

    def swap(*args, **kwargs):
        f.write_text("internal notes of the day\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(review, "_match_deliverable", swap)
    digest: list = []
    v, message = review.send_check(a, f, False, digest_out=digest)
    assert v == review.SEND_REFUSE and fx.CUSTOMER_FORMS[0] not in message


def test_a_deliverable_swapped_back_during_the_check_does_not_pass(home, register_path, tmp_path, monkeypatch):
    a = spawn(home, register_path)
    d = tier3(a, tmp_path)
    good = d.read_bytes()
    d.write_text(TEXT + "One more sentence nobody reviewed.\n", encoding="utf-8")
    real = review._record_state

    def restore(*args, **kwargs):
        d.write_bytes(good)
        return real(*args, **kwargs)

    monkeypatch.setattr(review, "_record_state", restore)
    assert review.send_check(a, d, True)[0] == review.SEND_REFUSE


def test_review_material_behind_a_linked_reviews_folder_never_leaves(home, register_path, tmp_path):
    a = spawn(home, register_path)
    d = tier3(a, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(a / "reviews"), str(elsewhere))
    os.symlink(elsewhere, a / "reviews")
    contract = elsewhere / d.name / review.CONTRACT
    v, message = review.send_check(a, contract, False)
    assert v == review.SEND_REFUSE and "review material" in message


@needs_permissions
def test_a_folder_name_is_never_echoed_in_a_refusal(home, register_path, tmp_path):
    a = spawn(home, register_path)
    stray = home.projects_root / ("tcp-" + fx.CUSTOMER_FORMS[1])
    (stray / "deliverables" / "sub").mkdir(parents=True)
    os.chmod(stray / "deliverables" / "sub", 0o000)
    try:
        v, message = review.send_check(a, plain(tmp_path), True, workbench=home)
    finally:
        os.chmod(stray / "deliverables" / "sub", 0o750)
    assert v == review.SEND_REFUSE and fx.CUSTOMER_FORMS[1] not in message


@needs_permissions
def test_an_unreadable_hcs_folder_under_the_projects_root_refuses(home, register_path, tmp_path):
    a = spawn(home, register_path)
    taken = {pr.code for pr in projects.load(home)}
    stray = home.projects_root / codes.new_project_code("hcs", taken)
    (stray / "deliverables").mkdir(parents=True)
    os.chmod(stray, 0o000)
    try:
        v, message = review.send_check(a, plain(tmp_path), True, workbench=home)
    finally:
        os.chmod(stray, 0o750)
    assert v == review.SEND_REFUSE and stray.name in message


def test_a_malformed_project_register_refuses(home, register_path, tmp_path):
    a = spawn(home, register_path)
    home.projects_register.write_text("not a register\n", encoding="utf-8")
    v, message = review.send_check(a, plain(tmp_path), True, workbench=home)
    assert v == review.SEND_REFUSE and "not a register" not in message


@needs_permissions
def test_a_projects_root_that_cannot_be_listed_refuses(home, register_path, tmp_path):
    a = spawn(home, register_path)
    f = plain(tmp_path)
    os.chmod(home.projects_root, 0o100)
    try:
        v, _ = review.send_check(a, f, True, workbench=home)
    finally:
        os.chmod(home.projects_root, 0o750)
    assert v == review.SEND_REFUSE


def test_a_secret_in_an_internal_file_refuses_and_is_not_echoed(home, register_path, tmp_path):
    a = spawn(home, register_path)
    value = "Xy7kQ2mLp9vB3rT"
    f = tmp_path / "setup.md"
    f.write_text("the lab login: %s = %s\n" % ("pass" + "word", value), encoding="utf-8")
    v, message = review.send_check(a, f, False)
    assert v == review.SEND_REFUSE and value not in message


def test_a_request_lens_whose_deliverable_is_no_string_does_not_count(home, register_path, tmp_path):
    a = spawn(home, register_path)
    d = reviewed(a, tmp_path, 3, lenses=[("platform-tcp", PLATFORM_LENS),
                                         ("request", {**REQUEST_LENS, "deliverable": ["deliverables/other.md"]})])
    assert review.send_check(a, d, True)[0] == review.SEND_REFUSE


def test_an_error_inside_the_check_refuses_and_never_raises(home, register_path, tmp_path, monkeypatch):
    a = spawn(home, register_path)
    d = tier3(a, tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("a bug of the check")

    monkeypatch.setattr(review, "_request_lens_for", broken)
    v, message = review.send_check(a, d, True)
    assert v == review.SEND_REFUSE and "bug" not in message
