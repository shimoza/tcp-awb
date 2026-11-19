"""tcp-bytg 2026-11-18: the gate scans the pieces of a large text in worker processes."""
from __future__ import annotations

import multiprocessing
import os
import threading

from awb import gate


def _secret_line() -> str:
    return "db_pass" + "word = \"" + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc" + "\""


def _small_pieces(monkeypatch):
    monkeypatch.setattr(gate, "PIECE_FROM", 2000)
    monkeypatch.setattr(gate, "PIECE", 500)
    monkeypatch.setattr(gate, "PIECE_OVERLAP", 4)
    monkeypatch.setattr(threading, "active_count", lambda: 1)    # pytest may run a timeout thread


def _data() -> bytes:
    lines = ["reader read %04d: block ok, status READY" % i for i in range(600)]
    for at in (3, 150, 333, 597):
        lines[at] = _secret_line()
    return ("\n".join(lines) + "\n").encode()


def _found(data: bytes) -> list[tuple[int, str]]:
    return [(f.line, f.cls) for f in gate._scan_bytes("notes/lab-runs/x.log", data, None)]


def _pids(monkeypatch) -> set[int]:
    monkeypatch.setattr(gate, "_scan_text", lambda label, piece, matcher, path=None:
                        [gate.Finding(label, 1, "pid %d" % os.getpid())])
    return {int(f.cls.split()[1]) for f in gate._scan_decoded("x.log", _data(), None)}


def test_bytg_parallel_scan_finds_what_the_serial_scan_finds(monkeypatch):
    _small_pieces(monkeypatch)
    data = _data()
    parallel = _found(data)
    monkeypatch.setattr(gate, "PARALLEL_FROM", 10 ** 9)
    assert parallel == _found(data) == [(4, "secret"), (151, "secret"), (334, "secret"), (598, "secret")]


def test_bytg_parallel_scan_runs_in_worker_processes(monkeypatch):
    _small_pieces(monkeypatch)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    pids = _pids(monkeypatch)
    assert pids and os.getpid() not in pids


def test_bytg_a_pool_that_cannot_start_falls_back_to_this_process(monkeypatch):
    _small_pieces(monkeypatch)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)

    def refuse(*a, **k):
        raise OSError("no process left")

    monkeypatch.setattr(multiprocessing, "get_context", refuse)
    assert _found(_data()) == [(4, "secret"), (151, "secret"), (334, "secret"), (598, "secret")]
    assert _pids(monkeypatch) == {os.getpid()}


def test_bytg_a_process_with_other_threads_scans_here(monkeypatch):
    _small_pieces(monkeypatch)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(threading, "active_count", lambda: 2)
    assert _pids(monkeypatch) == {os.getpid()}
