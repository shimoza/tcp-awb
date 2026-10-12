"""The guard of tests/conftest.py against the host's real vault can fail (a checker that reports clean is worthless
until it is shown able to fail)."""
from __future__ import annotations

import os

import pytest

from tests import conftest


def test_the_vault_guard_refuses_the_real_vault():
    """The guard can fail: a write into the real vault folder and a connect to its admin socket are refused and
    recorded (nothing is written: the refusal comes before the open)."""
    import socket

    root = conftest._REAL_VAULT_ROOTS[0]
    with pytest.raises(PermissionError):
        open(os.path.join(root, "never-written.tsv"), "w")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(PermissionError):
            sock.connect(os.path.join(root, "admin.sock"))
    finally:
        sock.close()
    assert len(conftest._VAULT_GUARD["hits"]) == 2
    conftest._VAULT_GUARD["hits"] = []
