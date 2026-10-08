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
PLACEHOLDERS = {"@OWNER@", "@DOMAIN@", "@VAULT@", "@SHARED@", "@WORK_HOME@", "@OWNER_HOST@", "@FRONT_SOCKET@",
                "@FRONT_DIR@", "@CLOUDFLARED_USER@"}


def templates():
    return sorted(p for p in WEB.rglob("*") if p.is_file() and p.suffix in (".service", ".socket", ".timer", ".conf")
                  and p.parent.name != "tunnel")


def tunnel_templates():
    return sorted((WEB / "tunnel").iterdir())


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


def test_the_switch_script_refuses_a_missing_or_odd_domain_and_another_user(tmp_path):
    env, units, log = _stubs(tmp_path)
    assert _install(env).returncode == 2
    assert _install(env, "--domain", "https://awb.example.test").returncode == 2
    assert _install(env, "--domain", "Awb.Example.Test").returncode == 2
    assert _install(dict(env, SUDO_USER="someone"), "--domain", "awb.example.test").returncode == 2
    assert not units.exists() and not log.exists()




# --------------------------------------------------------------------------- T9 step 1: the front socket and the fence

CONF_T9 = ("owner = ownr\nwork_user = awb\nvault = /srv/vault-test\nshared = /srv/shared-test\n"
           "owner_host = owner.example.test\nfront_socket = /run/awb-web/front.sock\ncloudflared_user = cloudflared\n")


def _stubs_t9(tmp_path, conf=CONF_T9):
    """systemctl, useradd, getent, id and nft that log their arguments; a user exists once useradd made it."""
    bin_dir, log, made = tmp_path / "bin", tmp_path / "calls.log", tmp_path / "made"
    bin_dir.mkdir()
    made.mkdir()
    scripts = {
        "systemctl": 'echo "systemctl $*" >> "%s"' % log,
        "nft": 'echo "nft $*" >> "%s"' % log,
        "useradd": 'echo "useradd $*" >> "%s"; for a; do last=$a; done; touch "%s/$last"' % (log, made),
        "getent": 'echo "awb:x:1001:1001::/srv/work-home:/bin/sh"',
        "id": '[ "$1" = "-gn" ] && { echo awb; exit 0; }; [ -e "%s/$1" ]' % made,
    }
    for name, body in scripts.items():
        f = bin_dir / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "paths.conf").write_text(conf)
    units, fence = tmp_path / "units", tmp_path / "etc-awb" / "tunnel-fence.nft"
    env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]), AWB_INSTALL_CONF=str(tmp_path / "paths.conf"),
               AWB_INSTALL_UNITS=str(units), AWB_INSTALL_FENCE=str(fence), SUDO_USER="ownr")
    return env, units, log, fence


def test_the_rendered_front_units_and_the_fence(tmp_path):
    """The gateway listens on the front socket (root, group of the tunnel's user, 0660, its folder 0750) and the
    status socket, takes the peer from paths.conf and keeps 8080 and 8081 for the one release of the switch;
    cloudflared runs as its own user with a copy of the token in its own runtime folder, root loads the fence first; the fence refuses
    every new loopback TCP connection of that user but the resolver and the two old ports, IPv4 and IPv6 (inet), and
    keeps everyone else off the metrics port."""
    env, units, log, fence = _stubs_t9(tmp_path)
    done = _install(env, "--domain", "awb.example.test")
    assert done.returncode == 0, done.stderr
    web = (units / "awb-web.service").read_text()
    assert ("--domain awb.example.test --owner-host owner.example.test --front-socket /run/awb-web/front.sock "
            "--front-peer cloudflared --status-socket /run/awb-web-status.sock --ports 8080 8081") in web
    assert "Sockets=awb-web.socket awb-web-status.socket" in web and "Requires=awb-web.socket awb-web-status.socket" in web
    front = (units / "awb-web.socket").read_text()
    for line in ("ListenStream=/run/awb-web/front.sock", "SocketUser=root", "SocketGroup=cloudflared", "SocketMode=0660",
                 "ExecStartPre=/usr/bin/install -d -m 0750 -o root -g cloudflared /run/awb-web",
                 "Service=awb-web.service"):
        assert line in front.splitlines(), line
    assert "ListenStream=/run/awb-web-status.sock" in (units / "awb-web-status.socket").read_text()
    tunnel = (units / "cloudflared.service.d" / "50-awb-fence.conf").read_text().splitlines()
    for line in ("User=cloudflared", "Group=cloudflared", "RuntimeDirectoryMode=0700",
                 "ExecStartPre=+/usr/sbin/nft -f /etc/awb/tunnel-fence.nft",
                 "ExecStartPre=+/usr/bin/install -m 0400 -o cloudflared -g cloudflared /etc/cloudflared/token "
                 "/run/cloudflared-awb/tunnel", "ExecStart=",
                 "ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel --metrics 127.0.0.1:20241 run --token-file "
                 "/run/cloudflared-awb/tunnel"):
        assert line in tunnel, line
    rules = [x.strip() for x in fence.read_text().splitlines() if x.strip() and not x.strip().startswith("#")]
    assert rules[:3] == ["table inet awb_tunnel_fence", "delete table inet awb_tunnel_fence",
                         "table inet awb_tunnel_fence {"]
    assert ('oifname "lo" meta l4proto tcp ct state new meta skuid "cloudflared" tcp dport != { 53, 8080, 8081 } '
            'counter reject with tcp reset') in rules
    assert ('oifname "lo" meta l4proto tcp ct state new tcp dport 20241 meta skuid != "cloudflared" counter reject '
            'with tcp reset') in rules
    calls = log.read_text().splitlines()
    assert "useradd --system --user-group --no-create-home -d /nonexistent --shell /usr/sbin/nologin cloudflared" in calls
    assert calls[-1] == "systemctl restart cloudflared.service"
    for unit in ("awb-web.socket", "awb-web-status.socket", "awb-customers.socket"):
        assert "systemctl enable --now %s" % unit in calls
    assert calls.index("systemctl daemon-reload") < calls.index("systemctl enable --now awb-web.socket")
    # unchanged on a second run: nothing written, cloudflared not restarted, nothing enabled again
    log.unlink()
    again = _install(env, "--domain", "awb.example.test")
    assert again.returncode == 0 and "write " not in again.stdout
    calls = log.read_text().splitlines()
    assert "systemctl restart cloudflared.service" not in calls and not any("enable --now awb-web" in c for c in calls)


def test_a_missing_or_odd_web_key_of_paths_conf_stops_before_anything_changes(tmp_path):
    for conf, word in ((CONF_T9.replace("owner_host = owner.example.test\n", ""), "owner_host"),
                       (CONF_T9.replace("front_socket = /run/awb-web/front.sock\n", ""), "front_socket"),
                       (CONF_T9.replace("cloudflared_user = cloudflared\n", ""), "cloudflared_user"),
                       (CONF_T9.replace("owner.example.test", "awb.example.test"), "owner_host"),
                       (CONF_T9.replace("/run/awb-web/front.sock", "/tmp/front.sock"), "front_socket")):
        case = tmp_path / word / str(len(conf))
        case.mkdir(parents=True)
        env, units, log, fence = _stubs_t9(case, conf)
        res = _install(env, "--domain", "awb.example.test")
        assert res.returncode == 2 and word in res.stderr, (word, res.stderr)
        assert not units.exists() and not fence.exists() and not log.exists()
    (tmp_path / "msg").mkdir()
    res = _install(_stubs_t9(tmp_path / "msg", CONF_T9.replace("owner_host = owner.example.test\n", ""))[0],
                   "--domain", "awb.example.test")
    assert "sudo awb web host HOST" in res.stderr


def test_the_rollback_puts_back_what_the_switch_replaced_and_lifts_the_fence(tmp_path):
    """Replaces test_the_rollback_puts_back_what_the_switch_replaced (T9 step 1): the rollback also retires the
    tunnel's drop-in and fence, drops the fence's table and restarts cloudflared as before."""
    env, units, log, fence = _stubs_t9(tmp_path)
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
    assert not (units / "awb-web.socket").exists() and (units / "awb-web.socket.off").exists()
    assert (units / "cloudflared.service.d" / "50-awb-fence.conf.off").exists() and not fence.exists()
    calls = log.read_text().splitlines()
    assert "systemctl disable --now awb-console-tenants.service" in calls
    assert "nft delete table inet awb_tunnel_fence" in calls
    assert calls[-2:] == ["systemctl restart awb-portal.service awb-ask.service awb-console-data.service awb-web.service",
                          "systemctl restart cloudflared.service"]


def test_install_sh_only_restarts_the_named_units_with_the_front(tmp_path):
    """Replaces test_install_sh_only_restarts_the_named_units (T9 step 1: paths.conf names the web keys; the
    sockets the run adds are enabled whatever --only names). Planted: a dry run with --only awb-ask.service that
    prints another restart line, or a --domain not taken from the installed awb-web.service, fails."""
    env, units, log, fence = _stubs_t9(tmp_path)
    units.mkdir(parents=True)
    (units / "awb-web.service").write_text("[Service]\nExecStart=/usr/bin/python3 -I /opt/tcp-awb/src/awb/tcp/web/"
                                           "gateway.py --auth /etc/awb-web/auth.json --domain awb.example.test\n")
    dry = _install(env, "--only", "awb-ask.service", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "the site awb.example.test, as the installed awb-web.service names it" in dry.stdout
    systemctl = [x for x in dry.stdout.splitlines() if x.startswith("+ systemctl") and "enable --now" not in x]
    assert systemctl == ["+ systemctl daemon-reload", "+ systemctl restart awb-ask.service",
                         "+ systemctl restart cloudflared.service"]
    assert not log.exists()
    done = _install(env, "--only", "awb-customers.socket", "awb-customers.service", "awb-web.service")
    assert done.returncode == 0, done.stderr
    calls = [x for x in log.read_text().splitlines() if x.startswith("systemctl") and "enable --now" not in x]
    assert calls == ["systemctl daemon-reload", "systemctl stop awb-customers.service",
                     "systemctl restart awb-customers.socket", "systemctl restart awb-web.service",
                     "systemctl restart cloudflared.service"]
    assert "--domain awb.example.test" in (units / "awb-web.service").read_text()
    log.unlink()
    assert _install(env, "--only", "awb-web.service").returncode == 0
    assert log.read_text().splitlines() == ["systemctl daemon-reload", "systemctl restart awb-web.service"]
    (units / "awb-web.service").unlink()
    assert _install(env, "--only", "awb-ask.service", "--dry-run").returncode == 2
    assert _install(env, "--only", "not a unit", "--dry-run").returncode == 2
    assert _install(env, "--rollback", "--only", "awb-ask.service").returncode == 2


# --------------------------------------------------------------------------- T9 step 2: owner-actions and the status


WEB_UNITS = ["awb-ask.service.d/95-project-chat.conf", "awb-console-data.service", "awb-console-tenants.service",
             "awb-customers.service", "awb-customers.socket", "awb-materials.service", "awb-materials.socket",
             "awb-owner-actions.service", "awb-owner-actions.socket", "awb-owner-status.service",
             "awb-owner-status.timer", "awb-portal.service.d/90-awb-web.conf", "awb-project-create.service",
             "awb-project-create.socket", "awb-web-publish.service", "awb-web-publish.socket",
             "awb-web-status.socket", "awb-web.service", "awb-web.socket"]


def test_every_module_a_template_starts_exists_with_owner_actions():
    """Replaces test_every_module_a_template_starts_exists (T9 step 2 adds owner-actions, an eighth module a template
    starts): every module a template starts exists, the gateway and owner-actions among them."""
    started = []
    for path in templates():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart=") and line != "ExecStart=":
                started += re.findall(r"-m (awb\.[\w.]+)", line)
                started += [m.replace("/opt/tcp-awb/src/", "") for m in re.findall(r"/opt/tcp-awb/src/\S+\.py", line)]
    assert len(started) == 8 and "awb.tcp.web.owner_actions" in started and "awb/tcp/web/gateway.py" in started
    for name in started:
        if name.endswith(".py"):
            assert (REPO / name).is_file(), name
        else:
            importlib.import_module(name)


def test_owner_actions_runs_as_its_own_user_without_network_and_the_status_as_the_owner(tmp_path):
    env, units, log, fence = _stubs_t9(tmp_path)
    assert _install(env, "--domain", "awb.example.test").returncode == 0
    socket_unit = (units / "awb-owner-actions.socket").read_text().splitlines()
    for line in ("ListenStream=/run/awb-owner.sock", "SocketUser=root", "SocketGroup=awb-web", "SocketMode=0660"):
        assert line in socket_unit, line
    service = (units / "awb-owner-actions.service").read_text().splitlines()
    for line in ("User=awb-owner", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "ProtectHome=yes",
                 "ProtectSystem=strict", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                 "ReadOnlyPaths=/var/lib/awb-owner-status"):
        assert line in service, line
    assert any("-m awb.tcp.web.owner_actions --peer awb-web --status /var/lib/awb-owner-status/status.json" in x
               for x in service)
    status = (units / "awb-owner-status.service").read_text().splitlines()
    for line in ("Type=oneshot", "User=ownr", "Group=awb-owner", "ExecStart=/usr/local/bin/awb owner status --write",
                 "StateDirectory=awb-owner-status", "StateDirectoryMode=0750", "UMask=0027"):
        assert line in status, line
    assert "OnUnitActiveSec=1min" in (units / "awb-owner-status.timer").read_text()
    calls = log.read_text().splitlines()
    assert "useradd --system --user-group --no-create-home -d /nonexistent --shell /usr/sbin/nologin awb-owner" in calls
    for unit in ("awb-owner-actions.socket", "awb-owner-status.timer"):
        assert "systemctl enable --now %s" % unit in calls
    assert "systemctl enable --now awb-owner-actions.service" not in calls


def test_the_templates_name_no_host_and_only_documented_placeholders_with_publish():
    """Replaces test_the_templates_name_no_host_and_only_documented_placeholders_with_the_owner_level (T9 step 3 adds
    the publish socket and service): the exact set of templates, each naming no host and only placeholders the
    README documents."""
    readme = (WEB / "README.md").read_text(encoding="utf-8")
    assert [p.relative_to(WEB).as_posix() for p in templates()] == WEB_UNITS
    assert [p.name for p in tunnel_templates()] == ["cloudflared.conf", "tunnel-fence.nft"]
    for path in templates() + tunnel_templates():
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text and "/opt/awb-web" not in text, path.name
        assert set(re.findall(r"@[A-Z_]+@", text)) <= PLACEHOLDERS, path.name
        assert path.name in readme or path.parent.name in readme, path.name
    for placeholder in PLACEHOLDERS:
        assert "`%s`" % placeholder in readme


def test_the_switch_script_renders_backs_up_retires_and_restarts_in_order_with_publish(tmp_path):
    """Replaces test_the_switch_script_renders_backs_up_retires_and_restarts_in_order_with_the_owner_level (T9 step
    3: the publish socket is enabled and restarts with the other sockets after its service stopped)."""
    env, units, log, fence = _stubs_t9(tmp_path)
    (units / "awb-ask.service.d").mkdir(parents=True)
    (units / "awb-web.service").write_text("the unit of before\n")
    (units / "awb-ask.service.d" / "90-awb-web.conf").write_text("[Service]\n")
    dry = _install(env, "--domain", "awb.example.test", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert (units / "awb-web.service").read_text() == "the unit of before\n" and not log.exists()
    assert not fence.exists()
    done = _install(env, "--domain", "awb.example.test")
    assert done.returncode == 0, done.stderr
    written = sorted(p.relative_to(units).as_posix() for p in units.rglob("*") if p.is_file()
                     and not p.name.endswith((".before-switch", ".off")))
    assert written == sorted(WEB_UNITS + ["cloudflared.service.d/50-awb-fence.conf"])
    for rel in written:
        text = (units / rel).read_text()
        assert "@" not in text and "/opt/awb-web" not in text, rel
    assert (units / "awb-web.service.before-switch").read_text() == "the unit of before\n"
    assert (units / "awb-ask.service.d" / "90-awb-web.conf.off").exists()
    calls = log.read_text().splitlines()
    assert [c.split()[-1] for c in calls[:3]] == ["awb-console", "awb-owner", "cloudflared"]
    assert calls[3] == "systemctl daemon-reload"
    enabled = [c for c in calls if c.startswith("systemctl enable --now ") and c.endswith((".socket", ".timer"))]
    assert enabled == ["systemctl enable --now %s" % u for u in (
        "awb-customers.socket", "awb-materials.socket", "awb-owner-actions.socket", "awb-owner-status.timer",
        "awb-project-create.socket", "awb-web-publish.socket", "awb-web-status.socket", "awb-web.socket")]
    rest = calls[4 + len(enabled):]
    assert rest == ["systemctl restart awb-keyd.service",
                    "systemctl stop awb-customers.service awb-project-create.service awb-materials.service "
                    "awb-owner-actions.service awb-web-publish.service",
                    "systemctl restart awb-customers.socket awb-project-create.socket awb-materials.socket "
                    "awb-owner-actions.socket awb-web-publish.socket awb-web.socket awb-web-status.socket",
                    "systemctl start awb-owner-status.service",
                    "systemctl restart awb-portal.service awb-ask.service awb-console-data.service",
                    "systemctl enable --now awb-console-tenants.service",
                    "systemctl restart awb-console-tenants.service awb-web.service",
                    "systemctl restart cloudflared.service"]
    again = _install(env, "--domain", "awb.example.test")
    assert again.returncode == 0 and "write " not in again.stdout and "useradd" not in again.stdout


def test_the_publish_unit_is_root_with_one_socket_owner_actions_may_open(tmp_path):
    env, units, log, fence = _stubs_t9(tmp_path)
    secrets = tmp_path / "owner-publish.json"
    secrets.write_text("{}")
    assert _install(env, "--domain", "awb.example.test").returncode == 0
    env["AWB_INSTALL_OWNER_PUBLISH"] = str(secrets)
    sock = (units / "awb-web-publish.socket").read_text().splitlines()
    for line in ("ListenStream=/run/awb-web-publish.sock", "SocketUser=root", "SocketGroup=awb-owner",
                 "SocketMode=0660"):
        assert line in sock, line
    assert "Accept=yes" not in sock
    service = (units / "awb-web-publish.service").read_text().splitlines()
    for line in ("User=root", "ExecStart=/usr/local/bin/awb web publish --from-socket --newest",
                 "ReadWritePaths=/srv/awb-web", "ProtectSystem=strict", "CapabilityBoundingSet=CAP_SETUID CAP_SETGID "
                 "CAP_CHOWN", "RestrictAddressFamilies=AF_UNIX"):
        assert line in service, line
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert "systemctl enable --now awb-web-publish.socket" in calls
    out = _install(env, "--domain", "awb.example.test", "--dry-run").stdout
    assert "+ chown awb-owner:awb-owner %s" % secrets in out and "+ chmod 600 %s" % secrets in out
