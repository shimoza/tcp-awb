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


def test_a_patched_work_user_that_touches_the_real_shared_side_or_kb_fails(tmp_path):
    """T3 (T3H-a): HOME stays the real one and a patched `config.is_work_user` makes `config.paths()` take the real
    ~/tcp-shared and ~/tcp-kb. A test that then writes or lists there fails, even when the code under test catches
    the refusal; the guard refuses the call before anything is written."""
    body = """
import os

from awb import config


def test_inner(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    p = config.paths()
    assert str(p.shared) == os.path.realpath(os.path.expanduser("~/tcp-shared"))
    assert str(p.kb) == os.path.realpath(os.path.expanduser("~/tcp-kb"))
    try:
        open(p.shared / "never-written.txt", "w")
    except OSError:
        pass
    try:
        os.listdir(p.kb)
    except OSError:
        pass
"""
    done = _inner_run(tmp_path, body, {})
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "the test reached a guarded place of the host" in out, out
    assert "open reached the real shared side or knowledge base" in out, out
    assert "os.listdir reached the real shared side or knowledge base" in out, out
    assert not os.path.exists(os.path.join(os.path.expanduser("~/tcp-shared"), "never-written.txt"))


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


def test_a_link_planted_before_the_test_into_the_vault_is_refused(tmp_path, monkeypatch):
    """T5: a temporary folder stands in for the vault root and a link planted next to it points at the folder. An
    open, a listing and a file to be made under the link are refused like the root itself and nothing is written;
    the link is planted before the roots are patched, as one made before the run would be."""
    vault = tmp_path / "vault"
    vault.mkdir()
    link = tmp_path / "link"
    link.symlink_to(vault)
    with monkeypatch.context() as patched:
        patched.setattr(conftest, "_REAL_VAULT_ROOTS", (str(vault),))
        assert conftest._vault_hit(str(link)) == "the real vault folder"
        assert conftest._vault_hit(str(link / "new" / "never-written.tsv")) == "the real vault folder"
        assert conftest._vault_hit(str(tmp_path / "elsewhere")) is None
        with pytest.raises(PermissionError):
            open(link / "never-written.tsv", "w")
        with pytest.raises(PermissionError):
            os.listdir(link)
        with pytest.raises(PermissionError):
            os.mkdir(link / "new")
    assert sorted(os.listdir(tmp_path)) == ["link", "vault"]
    assert os.listdir(vault) == []
    assert len(conftest._VAULT_GUARD["hits"]) == 3
    conftest._VAULT_GUARD["hits"] = []


def test_import_time_code_of_a_test_module_meets_the_guard(tmp_path):
    """T4 with T5: the child's vault root is a temporary folder (`AWB_VAULT` of the start of its run) and a link
    planted next to it points there. A module-level open of a path under the link is refused when pytest imports
    the module, before its first test runs; nothing is written."""
    vault = tmp_path / "vault"
    vault.mkdir()
    link = tmp_path / "link"
    link.symlink_to(vault)
    body = """
import os

open(os.path.join(%r, "never-written.tsv"), "w")


def test_inner():
    pass
""" % str(link)
    done = _inner_run(tmp_path, body, {"AWB_VAULT": str(vault)})
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "the tests never reach the real vault folder" in out, out
    assert "Interrupted: 1 error during collection" in out, out
    assert "test_inner" not in done.stdout.split("ERRORS")[0], out     # no test ran: no dot, no F before the errors
    assert os.listdir(vault) == []


def test_a_module_fixture_that_reaches_the_vault_fails_the_first_test(tmp_path):
    """T4: a fixture of module scope that opens a path of the vault root and catches the refusal is refused all the
    same and the first test after it errors at its setup with the place that was reached, before it runs; the
    second test, which did not reach it, passes."""
    vault = tmp_path / "vault"
    vault.mkdir()
    body = """
import os

import pytest


@pytest.fixture(scope="module")
def wide():
    try:
        open(os.path.join(%r, "never-written.tsv"), "w")
    except OSError:
        pass
    return "set up"


def test_first(wide):
    assert wide == "set up"


def test_second(wide):
    assert wide == "set up"
""" % str(vault)
    done = _inner_run(tmp_path, body, {"AWB_VAULT": str(vault)})
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "a guarded place of the host was reached before the test" in out, out
    assert "open reached the real vault folder" in out, out
    assert "ERROR at setup of test_first" in out, out
    assert done.stdout.split()[0] == "E.", out
    assert os.listdir(vault) == []


def test_each_test_has_its_own_shared_side_vault_kb_and_throttle_stamps(tmp_path):
    """F1 section 5: the four roots were one folder for the whole run, so a throttle stamp or a file of the shared
    side one test left was read by the next. Planted: the first of two tests writes into each root; the second finds
    other folders with nothing in them."""
    body = """
import os
from pathlib import Path

ROOTS = ("AWB_SHARED", "AWB_VAULT", "AWB_KB", "AWB_THROTTLE_DIR")
SEEN = {}


def test_first():
    for name in ROOTS:
        root = Path(os.environ[name])
        root.mkdir(parents=True, exist_ok=True)
        (root / "left-by-the-first").write_text("x", encoding="utf-8")
        SEEN[name] = root


def test_second():
    assert len(SEEN) == len(ROOTS)
    for name in ROOTS:
        root = Path(os.environ[name])
        assert root != SEEN[name], name
        assert not (root / "left-by-the-first").exists(), name
"""
    done = _inner_run(tmp_path, body, {})
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.split()[0] == "..", done.stdout
