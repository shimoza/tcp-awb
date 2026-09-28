"""config: the places of a Workbench and the folders of the shared side on a sealed host.

seal/setup.sh leaves tcp-shared with the setgid bit (2750) and the outbox open to the group (2770): the owner's
intake writes into a tree that belongs to the work user. These tests build that tree in a temporary folder as the
present user; "another user owns it" is simulated by a different effective uid.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from awb import config, intake
from tests import fixtures as fx


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def sealed_shared(home) -> config.Paths:
    """The shared side as seal/setup.sh leaves it: tcp-shared 2750, the outbox and its folders 2770."""
    os.chmod(home.shared, 0o2750)
    os.chmod(home.outbox, 0o2770)
    if not os.stat(home.outbox).st_mode & stat.S_ISGID:
        pytest.skip("this file system does not keep the setgid bit on folders")
    return home


def test_paths_come_from_the_environment_then_the_host_file(tmp_path, monkeypatch):
    conf = tmp_path / "paths.conf"
    conf.write_text("# host file\nshared = %s\nvault=%s\nowner = someone\n" % (tmp_path / "s", tmp_path / "v"),
                    encoding="utf-8")
    for key in ("AWB_SHARED", "AWB_VAULT", "AWB_PROJECTS", "AWB_KB", "AWB_CHECK_SOCKET", "AWB_ADMIN_SOCKET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWB_CONF", str(conf))
    p = config.paths()
    assert (p.shared, p.vault) == (tmp_path / "s", tmp_path / "v")
    assert p.register == tmp_path / "v" / "register.tsv" and p.admin_sock == tmp_path / "v" / "admin.sock"
    monkeypatch.setenv("AWB_VAULT", str(tmp_path / "env-vault"))
    assert config.paths().vault == tmp_path / "env-vault"
    assert config.paths(vault=tmp_path / "arg").vault == tmp_path / "arg"


def test_make_dir_in_a_plain_tree_sets_exactly_the_mode(tmp_path):
    d = config.make_dir(tmp_path / "a" / "b", 0o750, shared=True)
    assert mode(d) == 0o750
    os.chmod(d, 0o777)
    config.make_dir(d, 0o750, shared=True)
    assert mode(d) == 0o750, "an existing folder of the plain layout gets its mode again"
    assert mode(config.make_dir(tmp_path / "private", 0o700)) == 0o700


def test_make_dir_in_a_group_shared_tree_keeps_the_group(sealed_shared):
    outbox = sealed_shared.outbox
    d = config.make_dir(outbox / fx.CUSTOMER_CODE, 0o750, shared=True)
    assert mode(d) == 0o2770, "a new folder of the outbox stays open to the group of the work user"
    assert os.stat(d).st_gid == os.stat(outbox).st_gid
    config.make_dir(outbox, 0o750, shared=True)
    assert mode(outbox) == 0o2770, "the mode setup.sh chose is kept"
    config.make_dir(sealed_shared.shared, 0o750, shared=True)
    assert mode(sealed_shared.shared) == 0o2750
    # a folder of the vault side never takes the group, even under a setgid parent
    assert mode(config.make_dir(outbox / "private", 0o700)) == 0o700


def test_make_dir_leaves_a_folder_of_another_user_alone(tmp_path, monkeypatch):
    d = tmp_path / "theirs"
    d.mkdir()
    os.chmod(d, 0o755)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    config.make_dir(d, 0o750, shared=True)
    assert mode(d) == 0o755


def test_ensure_layout_keeps_a_sealed_shared_side(sealed_shared, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)   # the work user owns the shared side
    os.chmod(sealed_shared.ledger, 0o2770)
    config.ensure_layout(sealed_shared)
    assert (mode(sealed_shared.shared), mode(sealed_shared.outbox), mode(sealed_shared.ledger)) == \
        (0o2750, 0o2770, 0o2770)
    assert mode(sealed_shared.vault) == 0o700


def test_intake_writes_into_a_group_shared_outbox(sealed_shared):
    p = sealed_shared
    f = p.inbox / "angebot.txt"
    f.write_text("das angebot fuer %s geht an %s.\n" % (fx.CUSTOMER_FORMS[0], fx.PERSON_FORMS[0]), encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, p)
    assert not res.blocked and len(res.outputs) == 1
    folder = p.outbox / fx.CUSTOMER_CODE
    assert mode(folder) == 0o2770, "the work user can read and move what the intake wrote"
    for written in res.outputs + [res.public_report]:
        assert written.parent == folder
        assert mode(written) == 0o640 and os.stat(written).st_gid == os.stat(folder).st_gid
    assert (mode(p.shared), mode(p.outbox)) == (0o2750, 0o2770)
    assert mode(res.private_report) == 0o600 and mode(res.private_report.parent) == 0o700


# --------------------------------------------------------------------------- the work user of a sealed host


def _host_file(tmp_path: Path, monkeypatch, work_user: str, **values) -> Path:
    """A stand-in for the root-owned /etc/awb/paths.conf, set through the module constant (never the environment)."""
    f = tmp_path / "etc-awb-paths.conf"
    lines = ["work_user = %s" % work_user] + ["%s = %s" % (k, v) for k, v in values.items()]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "HOST_CONF", str(f))
    return f


def _me() -> str:
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def test_the_work_user_takes_its_paths_from_the_host_file_alone(tmp_path, monkeypatch):
    real = {"shared": tmp_path / "s", "vault": tmp_path / "v", "projects": tmp_path / "p", "kb": tmp_path / "kb",
            "check_socket": tmp_path / "run" / "check.sock"}
    _host_file(tmp_path, monkeypatch, _me(), **real)
    other = tmp_path / "other.conf"
    other.write_text("check_socket = %s\nkb = %s\n" % (tmp_path / "fake.sock", tmp_path / "fake-kb"), encoding="utf-8")
    # what a session could put into its own shell profile: none of it may move a path of the work user
    monkeypatch.setenv("AWB_CONF", str(other))
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(tmp_path / "fake.sock"))
    monkeypatch.setenv("AWB_KB", str(tmp_path / "fake-kb"))
    monkeypatch.setenv("AWB_SHARED", str(tmp_path / "fake-shared"))
    monkeypatch.setenv("AWB_VAULT", str(tmp_path / "fake-vault"))
    monkeypatch.setenv("AWB_PROJECTS", str(tmp_path / "fake-projects"))
    monkeypatch.setenv("AWB_ADMIN_SOCKET", str(tmp_path / "fake-admin.sock"))
    assert config.work_user() == _me()
    assert config.is_work_user()
    p = config.paths()
    assert p.check_socket == real["check_socket"].resolve()
    assert p.kb == real["kb"].resolve()
    assert (p.shared, p.vault, p.projects_root) == (real["shared"].resolve(), real["vault"].resolve(),
                                                     real["projects"].resolve())
    assert p.admin_socket is None
    assert config.host_conf()["kb"] == str(real["kb"])


def test_other_users_keep_the_environment_first(tmp_path, monkeypatch):
    _host_file(tmp_path, monkeypatch, "nobody-%d" % os.getpid(), check_socket=tmp_path / "run" / "check.sock")
    monkeypatch.setenv("AWB_CHECK_SOCKET", str(tmp_path / "env.sock"))
    assert not config.is_work_user()
    assert config.paths().check_socket == (tmp_path / "env.sock").resolve()


def test_without_a_host_file_nobody_is_the_work_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_CONF", str(tmp_path / "missing.conf"))
    assert config.work_user() is None
    assert not config.is_work_user()


def test_awb_conf_cannot_make_anyone_the_work_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_CONF", str(tmp_path / "missing.conf"))
    fake = tmp_path / "fake.conf"
    fake.write_text("work_user = %s\n" % _me(), encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(fake))
    assert config.work_user() is None
    assert not config.is_work_user()


def test_make_dir_refuses_a_link_where_a_folder_should_be(tmp_path):
    """The review of 2026-09-27 (SEC-SEAL-1): the work user owns the shared side and could plant a link in the
    outbox; the owner's intake would have chmoded the link's target, or written through it."""
    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o700)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    link = outbox / fx.CUSTOMER_CODE
    os.symlink(target, link)
    with pytest.raises(FileExistsError, match="a link stands where a folder should be"):
        config.make_dir(link, 0o750, shared=True)
    with pytest.raises(FileExistsError, match="a link stands where a folder should be"):
        config.make_dir(link / "images", 0o750, shared=True)     # a link on the way, not at the end
    with pytest.raises(FileExistsError):
        config.make_dir(link, 0o700)                              # the vault side refuses the link itself too
    assert mode(target) == 0o700 and not (target / "images").exists()
    with pytest.raises(OSError):
        config.chmod_dir(link, 0o770)
    assert mode(target) == 0o700
