from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from awb import config
from tests import fixtures


BLOCKLIST = Path(__file__).resolve().parent / "blocklist.txt"


_HOST_WORK_USER = config.work_user()
SEALED_FOR_ANOTHER_USER = _HOST_WORK_USER is not None and _HOST_WORK_USER != pwd.getpwuid(os.geteuid()).pw_name
"""True on a sealed host when the tests run as the owner: a hook started as its own process then stays silent by
design (it checks the work user only), so a test that needs a hook process answering is skipped there."""


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
