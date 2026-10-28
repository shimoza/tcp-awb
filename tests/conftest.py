from __future__ import annotations

import ipaddress
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


def _no_job_wait_hook():
    """`jobs.retry` waits 2, 4 ... seconds between its tries and a stand-in server that is not there (the dead
    address of this file) was waited out in real time: a test of a refused fetch took 6 s. The cloud client slept the
    Retry-After of a busy gateway for real, up to 30 s (F1 section 5). Both wait through `jobs.pause`, which is
    disarmed here. The waiting goes, the tries stay: the same calls are made in the same order. Not touched: a caller
    of `retry` that passes its own `sleep` and a test that replaced `time.sleep` to see the waits (it then gets them
    as before)."""
    import time

    from awb import jobs

    real_pause, real_sleep = jobs.pause, time.sleep

    def pause(seconds):
        if time.sleep is not real_sleep:
            time.sleep(seconds)

    pause.__wrapped__ = real_pause
    jobs.pause = pause


_no_job_wait_hook()


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

AS_ROOT = os.geteuid() == 0
"""True when the suite runs as root, as in a cloud container (F1 section 5): the seal scripts refuse their test-only
variables as root and `awb deploy` refuses root as the owner, so a test that needs either skips there instead of
failing. The owner's runs and the build's run as a user and skip nothing."""

not_as_root = pytest.mark.skipif(AS_ROOT, reason="runs as root: awb deploy refuses root as the owner and runs the "
                                                 "owner's steps never as root")


def skip_as_root(why: str) -> None:
    """Skip the running test with `why` when the suite runs as root; nothing otherwise."""
    if AS_ROOT:
        pytest.skip("runs as root: %s" % why)


def _real_vault_places() -> tuple[tuple[str, ...], frozenset[str]]:
    """The folders and sockets of the host's real vault, worked out once before any test patches a path: the vault
    folder and the admin and check sockets the root host file names, the defaults ~/tcp-vault and
    /run/awb/check.sock, and whatever AWB_VAULT, AWB_ADMIN_SOCKET and AWB_CHECK_SOCKET said when the run started.
    Only names are worked out: nothing inside the vault is opened."""
    conf: dict[str, str] = {}
    try:
        for line in Path(config.HOST_CONF).read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.lstrip().startswith("#"):
                conf[k.strip()] = v.strip()
    except (OSError, UnicodeDecodeError):
        pass
    folders = {"~/tcp-vault", conf.get("vault"), os.environ.get("AWB_VAULT")}
    sockets = {"/run/awb/check.sock", conf.get("admin_socket"), conf.get("check_socket"),
               os.environ.get("AWB_ADMIN_SOCKET"), os.environ.get("AWB_CHECK_SOCKET")}
    roots: set[str] = set()
    for f in filter(None, folders):
        f = os.path.abspath(os.path.expanduser(f))
        roots |= {f, os.path.realpath(f)}
        sockets.add(os.path.join(f, "admin.sock"))
    socks: set[str] = set()
    for s in filter(None, sockets):
        s = os.path.abspath(os.path.expanduser(s))
        socks |= {s, os.path.realpath(s)}
    return tuple(sorted(roots)), frozenset(socks)


def _real_data_places() -> tuple[str, ...]:
    """The real shared side and knowledge base of the host, worked out once before any test patches a path: the
    defaults ~/tcp-shared and ~/tcp-kb that `config.paths()` takes for a patched `is_work_user` (it then leaves the
    environment out and the host file of the tests is empty), the folders the root host file names and whatever
    AWB_SHARED and AWB_KB said when the run started. HOME stays the real one, since the repository and its .venv
    live under it and the code writes "$HOME/..." for them (T3H-a); these folders are refused instead (T3)."""
    conf: dict[str, str] = {}
    try:
        for line in Path(config.HOST_CONF).read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.lstrip().startswith("#"):
                conf[k.strip()] = v.strip()
    except (OSError, UnicodeDecodeError):
        pass
    folders = {"~/tcp-shared", "~/tcp-kb", conf.get("shared"), conf.get("kb"), os.environ.get("AWB_SHARED"),
               os.environ.get("AWB_KB")}
    roots: set[str] = set()
    for f in filter(None, folders):
        f = os.path.abspath(os.path.expanduser(f))
        roots |= {f, os.path.realpath(f)}
    return tuple(sorted(roots))


_REAL_VAULT_ROOTS, _REAL_VAULT_SOCKETS = _real_vault_places()
_REAL_DATA_ROOTS = _real_data_places()
_VAULT_GUARD = {"on": False, "busy": False, "hits": []}
_GUARDED_EVENTS = {"open": 0, "os.mkdir": 0, "os.rename": 0, "os.remove": 0, "os.rmdir": 0, "os.chmod": 0,
                   "os.chown": 0, "os.listdir": 0, "os.scandir": 0, "os.symlink": 1, "os.link": 1, "os.truncate": 0,
                   "os.utime": 0, "shutil.rmtree": 0, "shutil.copytree": 1, "shutil.copyfile": 1, "shutil.move": 1,
                   "socket.connect": 1, "socket.bind": 1}
"""Audit events and the position of the path argument in each (os.rename covers os.replace; the destination is
the one that writes). A test that reaches the host's real vault folder or a real vault socket through any of
them is refused at the call and fails (2026-10-12: three codes of tests/fixtures.py were found in the real
register; some test, at some time, wrote there)."""

_NETWORK_EVENTS = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname")
"""The events that leave this machine when the host is not loopback: a test that reaches one is refused (T1)."""
_HOST_SERVICE_PREFIX = "/run/awb-"
"""The sockets and folders of the host's services under /run (the key service /run/awb-keys/cloud.sock among them):
no test opens or connects to one (T3)."""


def _vault_hit(path) -> str | None:
    """The guarded place `path` reaches, or None. Both the path as given and its real path are compared: a link
    planted before the test (into a temporary folder, say) that points at a vault root is refused like the root
    itself (T5). `os.path.realpath` follows the links of the parents when the path does not exist yet, so a file
    to be made under such a link is seen too; it reads link targets and nothing else (nothing inside a vault is
    opened to decide)."""
    if isinstance(path, int) or path is None:
        return None
    try:
        path = os.fsdecode(path)
    except TypeError:
        return None
    if not path or path.startswith("\0"):
        return None
    try:
        full = os.path.normpath(path if os.path.isabs(path) else os.path.join(os.getcwd(), path))
    except OSError:     # a working folder that is gone: a relative path then reaches nothing
        return None
    try:
        places = {full, os.path.realpath(full)}
    except OSError:
        places = {full}
    for p in places:
        if p in _REAL_VAULT_SOCKETS:
            return "a socket of the real vault daemon"
        if p.startswith(_HOST_SERVICE_PREFIX):
            return "a socket of a service of the host"
        for root in _REAL_VAULT_ROOTS:
            if p == root or p.startswith(root + os.sep):
                return "the real vault folder"
        for root in _REAL_DATA_ROOTS:
            if p == root or p.startswith(root + os.sep):
                return "the real shared side or knowledge base"
    return None


def _not_loopback(host) -> bool:
    """True when `host` (a name or an address) is not this machine: anything but an IP literal in the loopback or
    the unspecified range, `localhost` and no host at all."""
    if host is None:
        return False
    try:
        host = os.fsdecode(host)
    except TypeError:
        return False
    host = host.split("%", 1)[0]
    if host in ("", "localhost", "localhost.localdomain"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_unspecified)


def _network_hit(event: str, args: tuple) -> str | None:
    """The host a connect or a name lookup of a test reaches outside this machine, or None (T1)."""
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else None
    elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
        host = args[0] if args else None
    else:
        return None
    return "the network (%s)" % os.fsdecode(host) if _not_loopback(host) else None


def _vault_audit(event: str, args: tuple) -> None:
    pos = _GUARDED_EVENTS.get(event)
    if event in _NETWORK_EVENTS and _VAULT_GUARD["on"] and not _VAULT_GUARD["busy"]:
        where = _network_hit(event, args)
        if where:
            _VAULT_GUARD["hits"].append("%s reached %s" % (event, where))
            raise PermissionError("the tests never reach %s (tests/conftest.py)" % where)
    if pos is None or not _VAULT_GUARD["on"] or _VAULT_GUARD["busy"]:
        return
    if event in ("os.rename", "os.symlink", "os.link") and len(args) > 1:
        candidates = args[:2]
    else:
        candidates = args[pos:pos + 1]
    _VAULT_GUARD["busy"] = True
    try:
        for c in candidates:
            if event.startswith("socket.") and not isinstance(c, (str, bytes)):
                continue
            where = _vault_hit(c)
            if where:
                _VAULT_GUARD["hits"].append("%s reached %s" % (event, where))
                raise PermissionError("the tests never reach %s (tests/conftest.py)" % where)
    finally:
        _VAULT_GUARD["busy"] = False


sys.addaudithook(_vault_audit)
_VAULT_GUARD["on"] = True
"""The guard (the vault places, the host services and the network) is on from the import of this file, before any
test module is imported and before any fixture runs: the import-time code of a test module and a fixture of module
or session scope meet it as a test does (T4). Only the per-test part stays in the fixture below."""


@pytest.fixture(autouse=True)
def no_real_vault():
    """Every test runs with the guard on: a call that would open, list, change or connect to the host's real vault
    folder or a real vault socket is refused, and the test fails even when the code under test caught the refusal.
    A refusal recorded before the test (at the import of its module, in a fixture of a wider scope) errors the first
    test that follows it at its setup, before it runs. The guard covers this process; a child gets the test's AWB_*
    environment, which points elsewhere."""
    earlier = sorted(set(_VAULT_GUARD["hits"]))
    _VAULT_GUARD["hits"] = []
    if earlier:
        pytest.fail("a guarded place of the host was reached before the test (the import of its module or a fixture "
                    "of a wider scope): %s" % "; ".join(earlier), pytrace=False)
    try:
        yield
    finally:
        hits = sorted(set(_VAULT_GUARD["hits"]))
        _VAULT_GUARD["hits"] = []
    if hits:
        pytest.fail("the test reached a guarded place of the host: %s" % "; ".join(hits), pytrace=False)


@pytest.fixture(autouse=True)
def invented_blocklist(tmp_path_factory, monkeypatch):
    """The invented blocklist of the tests in place of the host's and the owner's, and no host file: a test sees the
    same Workbench on a sealed host as on a plain one."""
    from awb import gate, hooks

    none = tmp_path_factory.getbasetemp() / "no-host"
    # nothing of the owner's shell: every AWB_ variable goes first, then the invented ones below are set (T2). A
    # `pass` that exits 1 comes first on PATH, so no test reaches the real password store or the real OBS.
    for name in [k for k in os.environ if k.startswith("AWB_")]:
        monkeypatch.delenv(name)
    stub = none / "bin"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "pass").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (stub / "pass").chmod(0o755)
    monkeypatch.setenv("PATH", str(stub) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(gate, "BLOCKLIST_FILES", (none / "blocklist.txt", none / "home-blocklist.txt"))
    monkeypatch.setenv("AWB_BLOCKLIST", str(BLOCKLIST))
    # no test writes to the real shared side or the real throttle stamps: a test that builds no Workbench of its
    # own still gets a throw-away shared folder (the call log of the cloud client wrote to the owner's home once).
    # The shared side, the vault, the knowledge base and the throttle stamps are a folder of each test's own, not
    # one of the run (F1 section 5): a stamp or a file one test left is never read by the next.
    own = Path(tempfile.mkdtemp(prefix="test-", dir=none))
    monkeypatch.setenv("AWB_SHARED", str(own / "shared"))
    # never the real key service of the host: a test that wants one starts its own
    monkeypatch.setenv("AWB_KEYS_SOCKET", str(none / "cloud.sock"))
    monkeypatch.setenv("AWB_KEYS_CONF", str(none / "keys.conf"))
    monkeypatch.setenv("AWB_THROTTLE_DIR", str(own / "throttle"))
    # a refused `awb paste` keeps its text in the user's own state folder (P1): never the real one of the host
    monkeypatch.setenv("XDG_STATE_HOME", str(own / "state"))
    monkeypatch.setattr(config, "HOST_CONF", str(none / "paths.conf"))
    monkeypatch.setattr(hooks, "HOST_FILE", none / "paths.conf")
    # nothing of the host a run could share with another run (two runs waited on each other for hours on
    # 2026-10-09): not the vault, check and key sockets of the host's own services, not the owner's vault,
    # knowledge base and mirrors, not the network. Each run has its own base folder, so these names are unique per
    # run; a test that wants one of them sets its own.
    monkeypatch.setenv("AWB_VAULT", str(own / "vault"))
    monkeypatch.setenv("AWB_KB", str(own / "kb"))
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
    # never the state of the weekly dataset publish of the host (task KS)
    monkeypatch.setenv("AWB_DATASET_WEEKLY", str(own / "dataset-weekly"))
    monkeypatch.setenv("AWB_PRICE_API", DEAD_URL)
    from awb.tcp import mirror
    monkeypatch.setattr(mirror, "ORG_API", DEAD_URL + "orgs/%s/repos?page=%d")
    monkeypatch.setattr(mirror, "SD_PAGE", DEAD_URL + "service-description")
    monkeypatch.setattr(mirror, "RN_PAGE", DEAD_URL + "release-notes")


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
