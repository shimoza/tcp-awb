"""The switch of the web side (seal/web/): the templates name no host, every placeholder is documented, every module
they start exists, and the private names the adapters still deployed outside the repository call stay until the
switch."""
import importlib
import re
from pathlib import Path

from awb import obs, projects, register
from awb.tcp import sweep

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "seal" / "web"
PLACEHOLDERS = {"@OWNER@", "@DOMAIN@", "@VAULT@", "@SHARED@", "@WORK_HOME@"}


def templates():
    return sorted(p for p in WEB.rglob("*") if p.is_file() and p.suffix in (".service", ".socket", ".conf"))


def test_the_templates_name_no_host_and_only_documented_placeholders():
    readme = (WEB / "README.md").read_text(encoding="utf-8")
    assert len(templates()) == 11
    for path in templates():
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text and "/opt/awb-web" not in text, path.name
        assert set(re.findall(r"@[A-Z_]+@", text)) <= PLACEHOLDERS, path.name
        assert path.name in readme or path.parent.name in readme, path.name
    for placeholder in PLACEHOLDERS:
        assert "`%s`" % placeholder in readme


def test_every_module_a_template_starts_exists():
    started = []
    for path in templates():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart=") and line != "ExecStart=":
                started += re.findall(r"-m (awb\.[\w.]+)", line)
                started += [m.replace("/opt/tcp-awb/src/", "") for m in re.findall(r"/opt/tcp-awb/src/\S+\.py", line)]
    assert len(started) == 7
    for name in started:
        if name.endswith(".py"):
            assert (REPO / name).is_file(), name
        else:
            importlib.import_module(name)


def test_the_private_names_of_the_deployed_adapters_stay_until_the_switch():
    assert projects._known_tags is projects.known_tags
    assert projects._locked is projects.locked
    assert (obs._text, obs._child, obs._local) == (obs.xml_text, obs.xml_child, obs.xml_local)
    assert obs.Client._sign is obs.Client.sign and obs.Client._url is obs.Client.url
    assert sweep._tags is sweep.tags
    assert callable(register._check)


# --- the switch script: rendered units, backups, the retired drop-in, the order of the restarts -------------

import os
import stat
import subprocess

INSTALL = WEB / "install.sh"


def _stubs(tmp_path):
    """systemctl, useradd, getent and id that log their arguments; the console user exists once useradd ran."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    marker = tmp_path / "console-user"
    scripts = {
        "systemctl": 'echo "systemctl $*" >> "%s"' % log,
        "useradd": 'echo "useradd $*" >> "%s"; touch "%s"' % (log, marker),
        "getent": 'echo "awb:x:1001:1001::/srv/work-home:/bin/sh"',
        "id": '[ "$1" = "-gn" ] && { echo awb; exit 0; }; [ -e "%s" ]' % marker,
    }
    for name, body in scripts.items():
        f = bin_dir / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    conf = tmp_path / "paths.conf"
    conf.write_text("owner = ownr\nwork_user = awb\nvault = /srv/vault-test\nshared = /srv/shared-test\n")
    units = tmp_path / "units"
    env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]), AWB_INSTALL_CONF=str(conf),
               AWB_INSTALL_UNITS=str(units), SUDO_USER="ownr")
    return env, units, log


def _install(env, *args):
    return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=60)


def test_the_switch_script_renders_backs_up_retires_and_restarts_in_order(tmp_path):
    env, units, log = _stubs(tmp_path)
    (units / "awb-ask.service.d").mkdir(parents=True)
    (units / "awb-web.service").write_text("the unit of before\n")
    (units / "awb-ask.service.d" / "90-awb-web.conf").write_text("[Service]\n")
    dry = _install(env, "--domain", "awb.example.test", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert (units / "awb-web.service").read_text() == "the unit of before\n" and not log.exists()
    done = _install(env, "--domain", "awb.example.test")
    assert done.returncode == 0, done.stderr
    written = sorted(p.relative_to(units).as_posix() for p in units.rglob("*") if p.is_file()
                     and not p.name.endswith((".before-switch", ".off")))
    assert written == sorted(p.relative_to(WEB).as_posix() for p in templates())
    for rel in written:
        text = (units / rel).read_text()
        assert "@" not in text and "/opt/awb-web" not in text, rel
    assert "--domain awb.example.test" in (units / "awb-web.service").read_text()
    assert "ReadWritePaths=/srv/vault-test /srv/shared-test/outbox" in (units / "awb-customers.service").read_text()
    assert "User=ownr" in (units / "awb-materials.service").read_text()
    assert (units / "awb-web.service.before-switch").read_text() == "the unit of before\n"
    assert (units / "awb-ask.service.d" / "90-awb-web.conf.off").exists()
    calls = log.read_text().splitlines()
    assert calls[0].startswith("useradd --system --gid awb") and calls[0].endswith(" awb-console")
    assert calls[1] == "systemctl daemon-reload" and calls[2] == "systemctl restart awb-keyd.service"
    assert calls[-1] == "systemctl restart awb-console-tenants.service awb-web.service"
    again = _install(env, "--domain", "awb.example.test")
    assert again.returncode == 0 and "write " not in again.stdout and "useradd" not in again.stdout
    assert (units / "awb-web.service.before-switch").read_text() == "the unit of before\n"


def test_the_switch_script_refuses_a_missing_or_odd_domain_and_another_user(tmp_path):
    env, units, log = _stubs(tmp_path)
    assert _install(env).returncode == 2
    assert _install(env, "--domain", "https://awb.example.test").returncode == 2
    assert _install(env, "--domain", "Awb.Example.Test").returncode == 2
    assert _install(dict(env, SUDO_USER="someone"), "--domain", "awb.example.test").returncode == 2
    assert not units.exists() and not log.exists()


def test_the_rollback_puts_back_what_the_switch_replaced(tmp_path):
    env, units, log = _stubs(tmp_path)
    (units / "awb-ask.service.d").mkdir(parents=True)
    (units / "awb-web.service").write_text("the unit of before\n")
    (units / "awb-ask.service.d" / "90-awb-web.conf").write_text("[Service]\n")
    assert _install(env, "--domain", "awb.example.test").returncode == 0
    back = _install(env, "--rollback")
    assert back.returncode == 0, back.stderr
    assert (units / "awb-web.service").read_text() == "the unit of before\n"
    assert not (units / "awb-web.service.before-switch").exists()
    assert (units / "awb-ask.service.d" / "90-awb-web.conf").exists()
    assert not (units / "awb-console-tenants.service").exists() and (units / "awb-console-tenants.service.off").exists()
    calls = log.read_text().splitlines()
    assert "systemctl disable --now awb-console-tenants.service" in calls
    assert calls[-1] == "systemctl restart awb-portal.service awb-ask.service awb-console-data.service awb-web.service"
