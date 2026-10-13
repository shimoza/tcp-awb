"""The guard of tests/conftest.py against the host's real vault can fail (a checker that reports clean is worthless
until it is shown able to fail)."""
from __future__ import annotations

import os
import subprocess
import sys

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


def _inner_run(tmp_path, body: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run one test file under the fixtures of tests/conftest.py in a child, with `env` added to the environment."""
    test = tmp_path / "test_inner.py"
    test.write_text(body, encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("AWB_")}
    child_env.update(env)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
                           "--rootdir", str(conftest.ROOT), "-c", str(conftest.ROOT / "pyproject.toml"), str(test)],
                          cwd=conftest.ROOT, env=child_env, capture_output=True, text=True, timeout=300)


def test_awb_variables_of_the_shell_do_not_reach_a_test(tmp_path):
    """T2: the variables of the owner's shell are gone and `pass` fails at once, so a test with no endpoint of its
    own reaches neither the password store nor the real OBS."""
    body = """
import os
import subprocess


def test_inner():
    shell = ("AWB_BUCKET_KEYS", "AWB_BUCKET_ENDPOINT", "AWB_CLOUD_ENDPOINT", "AWB_CLOUD_KEYS", "AWB_ASK_KEY_FILE")
    assert [k for k in shell if k in os.environ] == []
    assert os.environ["AWB_VAULT"].count("no-host") == 1
    assert subprocess.run(["pass", "show", "x"], capture_output=True).returncode == 1
"""
    shell = {"AWB_BUCKET_KEYS": "pass:ENTRY", "AWB_BUCKET_ENDPOINT": "https://obs.example.invalid",
             "AWB_CLOUD_ENDPOINT": "https://cloud.example.invalid", "AWB_CLOUD_KEYS": "pass:ENTRY",
             "AWB_ASK_KEY_FILE": "/nowhere/key"}
    done = _inner_run(tmp_path, body, shell)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_patched_work_user_gets_a_home_of_the_test(monkeypatch):
    """T3: with `config.is_work_user` patched the paths come from the home of the test, never the real one."""
    from awb import config

    monkeypatch.setattr(config, "is_work_user", lambda: True)
    shared = str(config.paths().shared)
    assert shared.startswith(os.environ["HOME"]) and "no-host" in shared
    assert str(config.paths().kb).startswith(os.environ["HOME"])


def test_the_guard_refuses_the_sockets_of_the_host_services():
    """T3: the key service socket and the other /run/awb-* sockets are refused like the vault."""
    import socket

    for name in ("/run/awb-keys/cloud.sock", "/run/awb-owner.sock"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(PermissionError):
                sock.connect(name)
        finally:
            sock.close()
    assert len(conftest._VAULT_GUARD["hits"]) == 2
    conftest._VAULT_GUARD["hits"] = []


def test_the_guard_refuses_the_network_and_lets_loopback_pass():
    """T1: a connect to a tuple address and a name lookup outside this machine are refused and recorded; loopback
    reaches the socket layer (nothing listens on the port, so the connect is refused by the system, not the guard)."""
    import socket

    assert conftest._network_hit("socket.connect", (None, ("2001:db8::1", 9, 0, 0)))    # a four-part address
    assert not conftest._network_hit("socket.connect", (None, ("::1", 9, 0, 0)))
    for address in (("192.0.2.1", 9), ("198.51.100.7", 443)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(PermissionError):
                sock.connect(address)
        finally:
            sock.close()
    with pytest.raises(PermissionError):
        socket.getaddrinfo("api.example.invalid", 443)
    with pytest.raises(PermissionError):
        socket.gethostbyname("api.example.invalid")
    assert len(conftest._VAULT_GUARD["hits"]) == 4
    conftest._VAULT_GUARD["hits"] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()
    assert socket.getaddrinfo("localhost", 9) and socket.getaddrinfo(None, 9)
    assert conftest._VAULT_GUARD["hits"] == []
