from __future__ import annotations

import os
import pwd
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from awb import config
from tests import fixtures
from tests.timeouts import (pytest_addoption, pytest_runtest_call, pytest_runtest_setup,  # noqa: F401
                            pytest_runtest_teardown, pytest_timeout_set_timer)


ROOT = Path(__file__).resolve().parent.parent
"""The tree under test: a checkout, a scratch worktree or a read-only export of the self-build (SB0)."""

SUITE_PYTHON = sys.executable
"""The interpreter the suite was started with, by its absolute path (the shared venv of a build, .venv here)."""


def _python_runs_this_tree(python: str) -> bool:
    """True when `python -I -m awb` imports the package of this tree, as the venv of a checkout with the editable
    install does. The shared venv of a build has no package installed: its suite imports the tree it runs in."""
    try:
        out = subprocess.run([python, "-I", "-B", "-c", "import awb, os; print(os.path.realpath(awb.__path__[0]))"],
                             capture_output=True, text=True, timeout=60, cwd="/", stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == os.path.realpath(ROOT / "awb")


def _tree_python(base: Path) -> Path:
    """A throw-away venv over the suite's interpreter that finds the package of this tree through one .pth line, as
    the installed venv finds the live release on a sealed host: the children the tests start as `<python> -I -m
    awb` (a daemon's hand-over, a hook, the wrapper) run the code under test, never another checkout's and never
    "No module named awb". It holds a symlink to the base interpreter, pyvenv.cfg, the .pth lines and the awb entry
    script of a venv; nothing is installed and the tree is not written: the children run with `-I`, which drops
    PYTHONDONTWRITEBYTECODE, so the .pth switches bytecode off itself (B-001: the children wrote awb/__pycache__
    into the build's export and test_repository_scans_clean read it as opaque files)."""
    import site

    real = Path(os.path.realpath(sys._base_executable if hasattr(sys, "_base_executable") else sys.executable))
    (base / "bin").mkdir(parents=True)
    (base / "pyvenv.cfg").write_text("home = %s\ninclude-system-site-packages = false\nversion = %s\n" % (
        real.parent, ".".join(map(str, sys.version_info[:3]))), encoding="utf-8")
    for name in ("python", "python3", "python%d.%d" % sys.version_info[:2]):
        (base / "bin" / name).symlink_to(real)
    site_dir = base / "lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages"
    site_dir.mkdir(parents=True)
    lines = [str(ROOT)] + [d for d in site.getsitepackages() if os.path.isdir(d)] + [
        "import sys; sys.dont_write_bytecode = True"]
    (site_dir / "awb-tree.pth").write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = base / "bin" / "awb"
    script.write_text("#!%s\nimport sys\nfrom awb.cli import main\nsys.exit(main())\n" % (base / "bin" / "python"),
                      encoding="utf-8")
    script.chmod(0o755)
    return base / "bin" / "python"


_TREE_PYTHON_DIR: Path | None = None


def pytest_configure(config):
    """Before the collection: when the suite's interpreter does not run this tree (a build's shared venv, a scratch
    worktree with the venv of another checkout), `sys.executable` becomes the tree interpreter above for the run."""
    global _TREE_PYTHON_DIR
    if _TREE_PYTHON_DIR is not None or _python_runs_this_tree(sys.executable):
        return
    _TREE_PYTHON_DIR = Path(tempfile.mkdtemp(prefix="awb-tree-python-"))
    sys.executable = str(_tree_python(_TREE_PYTHON_DIR / "venv"))


def pytest_unconfigure(config):
    global _TREE_PYTHON_DIR
    if _TREE_PYTHON_DIR is not None:
        sys.executable = SUITE_PYTHON
        shutil.rmtree(_TREE_PYTHON_DIR, ignore_errors=True)
        _TREE_PYTHON_DIR = None


_COPYTREE = shutil.copytree


def _writable_copytree(src, dst, *args, **kwargs):
    """shutil.copytree that leaves a copy of this tree writable for its owner: the tests copy parts of the tree into
    tmp_path to change them, and a read-only export would hand its modes on to the copy (SB0)."""
    out = _COPYTREE(src, dst, *args, **kwargs)
    try:
        inside = Path(os.path.realpath(src)).is_relative_to(os.path.realpath(ROOT))
    except (OSError, ValueError):
        inside = False
    if inside:
        for folder, dirs, files in os.walk(dst):
            for entry in [folder] + [os.path.join(folder, f) for f in files]:
                st = os.lstat(entry)
                if not stat.S_ISLNK(st.st_mode) and not st.st_mode & stat.S_IWUSR:
                    os.chmod(entry, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)
    return out


shutil.copytree = _writable_copytree

_SERVE_FOREVER = socketserver.BaseServer.serve_forever


def _quick_serve_forever(self, poll_interval=0.01):
    """socketserver's default looks at the shutdown flag every half second, so each stand-in server cost its test up
    to 0.5 s in shutdown() (teardown of a test with six servers took 2.5 s). Only the default changes: a caller
    that passes an interval gets it and no production default is touched (the daemons run in their own process)."""
    return _SERVE_FOREVER(self, poll_interval)


socketserver.BaseServer.serve_forever = _quick_serve_forever


def _no_retry_wait_hook():
    """`jobs.retry` waits 2, 4 ... seconds between its tries and a stand-in server that is not there (the dead
    address of this file) was waited out in real time: a test of a refused fetch took 6 s. The waiting goes, the tries
    stay: the same calls are made in the same order. Not touched: a caller that passes its own `sleep` and a test
    that replaced `time.sleep` to see the waits (it then gets them as before)."""
    import time

    from awb import jobs

    real_retry, real_sleep = jobs.retry, time.sleep

    def retry(fn, **kw):
        if kw.get("sleep") is None and time.sleep is real_sleep:
            kw["sleep"] = lambda seconds: None
        return real_retry(fn, **kw)

    retry.__wrapped__ = real_retry
    jobs.retry = retry


_no_retry_wait_hook()


def _engine_state() -> tuple:
    """The identity of every function, class member and constant of the modules the intake self-test runs through,
    the engine's own and the ones it calls (patterns, register, codes, the word lists, the confusables). A test that
    patches any of them (a blind matcher, a broken sanitiser, a blind pattern) changes this tuple."""
    import types

    from awb import codes, confusables_latin, intake, matcher, normalize, patterns, planted, register, wipe
    from awb import words as word_lists

    out = []
    for mod in (intake, matcher, normalize, planted, wipe, patterns, register, codes, word_lists, confusables_latin):
        for name, value in sorted(vars(mod).items()):
            if name in ("_RUN_EXEMPT", "selftest"):     # the per-run state of the last wipe; the hook itself
                continue
            out.append((mod.__name__, name, id(value)))
            if isinstance(value, type) and value.__module__ == mod.__name__:
                out.extend((name, k, id(v)) for k, v in sorted(vars(value).items())
                           if not isinstance(v, types.MemberDescriptorType) and k not in ("__dict__", "__weakref__"))
    return tuple(out)


def _cached_selftest_hook():
    """`intake.run` takes no file in before the self-test passed and the self-test takes about a second: it ran
    again for every intake of every test. Its verdict depends on the engine alone, so the verdict of the untouched
    engine is kept for the run. Not cached: a call from a test (the tests of the self-test itself run it for
    real) and any call while the engine is patched, whatever the patch."""
    import sys

    from awb import intake

    real = intake.selftest
    pristine = _engine_state()
    verdict: list[list[str]] = []

    def selftest():
        caller = sys._getframe(1).f_code.co_filename
        if caller != intake.__file__ or _engine_state() != pristine:
            return real()
        if not verdict:
            verdict.append(real())
        return list(verdict[0])

    selftest.__wrapped__ = real
    intake.selftest = selftest


_cached_selftest_hook()

BLOCKLIST = Path(__file__).resolve().parent / "blocklist.txt"
DEAD_URL = "http://127.0.0.1:9/"
"""A local address where nothing listens: a call that should not leave the test is refused at once."""


_HOST_WORK_USER = config.work_user()
AS_BUILD_USER = config.is_build_user()
"""True when the suite runs as the build user of the self-build (check 9 of a build task, in its suite unit, where
/etc/awb is hidden): the code then reads this uid as the build user, as it must on the host."""
SEALED_FOR_ANOTHER_USER = (_HOST_WORK_USER is not None and _HOST_WORK_USER != pwd.getpwuid(os.geteuid()).pw_name) \
    or AS_BUILD_USER
"""True on a sealed host when the tests run as the owner or as the build user: a hook started as its own process
then stays silent by design (it checks the work user only; the build user gets the build hooks alone), so a test
that needs a hook process answering is skipped there."""

not_as_build_user = pytest.mark.skipif(AS_BUILD_USER, reason="the suite runs as the build user: the test takes the "
                                                             "user who runs it for the owner of the host")


@pytest.fixture(autouse=True)
def invented_blocklist(tmp_path_factory, monkeypatch):
    """The invented blocklist of the tests in place of the host's and the owner's, and no host file: a test sees the
    same Workbench on a sealed host as on a plain one."""
    from awb import gate, hooks

    none = tmp_path_factory.getbasetemp() / "no-host"
    monkeypatch.setattr(gate, "BLOCKLIST_FILES", (none / "blocklist.txt", none / "home-blocklist.txt"))
    monkeypatch.setenv("AWB_BLOCKLIST", str(BLOCKLIST))
    # no test writes to the real shared side or the real throttle stamps: a test that builds no Workbench of its
    # own still gets a throw-away shared folder (the call log of the cloud client wrote to the owner's home once)
    monkeypatch.setenv("AWB_SHARED", str(none / "shared"))
    # never the real key service of the host: a test that wants one starts its own
    monkeypatch.setenv("AWB_KEYS_SOCKET", str(none / "cloud.sock"))
    monkeypatch.setenv("AWB_KEYS_CONF", str(none / "keys.conf"))
    monkeypatch.setenv("AWB_THROTTLE_DIR", str(none / "throttle"))
    monkeypatch.setattr(config, "HOST_CONF", str(none / "paths.conf"))
    monkeypatch.setattr(hooks, "HOST_FILE", none / "paths.conf")
    # nothing of the host a run could share with another run (two runs waited on each other for hours on
    # 2026-10-09): not the vault, check and key sockets of the host's own services, not the owner's vault,
    # knowledge base and mirrors, not the network. Each run has its own base folder, so these names are unique per
    # run; a test that wants one of them sets its own.
    monkeypatch.setenv("AWB_VAULT", str(none / "vault"))
    monkeypatch.setenv("AWB_KB", str(none / "kb"))
    monkeypatch.setenv("AWB_PROJECTS", str(none / "projects"))
    monkeypatch.setenv("AWB_CONF", str(none / "paths.conf"))
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(none / "check.sock"))
    monkeypatch.setenv("AWB_ADMIN_SOCKET", str(none / "admin.sock"))
    monkeypatch.setenv("AWB_KEYS_ADMIN", str(none / "keys-admin.sock"))
    monkeypatch.setenv("AWB_MIRRORS", str(none / "mirrors"))
    # never his build queue nor the runner's records (presentations/self-build SB2)
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(none / "build-queue"))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(none / "build-runs"))
    monkeypatch.setenv("AWB_BUILD_ROOT", str(none / "build-root"))
    monkeypatch.setenv("AWB_PRICE_API", DEAD_URL)
    from awb.tcp import mirror
    monkeypatch.setattr(mirror, "ORG_API", DEAD_URL + "orgs/%s/repos?page=%d")
    monkeypatch.setattr(mirror, "SD_PAGE", DEAD_URL + "service-description")


needs_hook_process = pytest.mark.skipif(SEALED_FOR_ANOTHER_USER,
                                        reason="sealed host: a hook process is silent for the owner by design")


@pytest.fixture
def home(tmp_path, monkeypatch) -> config.Paths:
    """A throw-away Workbench: shared side, vault side and a projects root, with the fixture register."""
    monkeypatch.setenv("AWB_SHARED", str(tmp_path / "tcp-shared"))
    monkeypatch.setenv("AWB_VAULT", str(tmp_path / "tcp-vault"))
    monkeypatch.setenv("AWB_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("AWB_KB", str(tmp_path / "tcp-kb"))
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(tmp_path / "run" / "check.sock"))
    monkeypatch.setenv("AWB_CONF", str(tmp_path / "no-host-conf"))
    p = config.ensure_layout(config.paths())
    p.register.write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    os.chmod(p.register, 0o600)
    return p


@pytest.fixture
def register_path(home) -> Path:
    return home.register
