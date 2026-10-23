"""Review of 2026-10-13, findings B5 and B7: every unit of a build task has an IPC namespace and a /dev/shm of its own
and no /dev/mqueue; a login of the task goes back to the build user's home only when it names the same account."""
import json
import re
from pathlib import Path

from awb import buildrun
from tests.test_build_runner import TOKEN, cred, nowhere, plant_login, seeded, world  # noqa: F401

VERIFY = Path(__file__).parents[1] / "seal" / "verify.sh"
SHM_PROPS = ["--property=PrivateTmp=yes", "--property=PrivateIPC=yes",
             "--property=TemporaryFileSystem=/dev/shm:size=64M,mode=1777,nosuid,nodev", "--property=NoNewPrivileges=yes"]


def login_json(account: str | None, expires: int, token: str = "fake-login-token-two") -> str:
    oauth = {"accessToken": token, "refreshToken": token + "-r", "expiresAt": expires, "scopes": ["user:inference"]}
    if account is not None:
        oauth["subscriptionType"] = account
    return json.dumps({"claudeAiOauth": oauth})


def test_every_task_unit_has_private_ipc_and_its_own_shm_and_no_mqueue(world):
    """B5: the session unit, the helper units and the probe unit carry PrivateIPC=yes and a TemporaryFileSystem of
    their own over /dev/shm, capped, next to the private /tmp; /dev/mqueue is the first inaccessible path after
    /etc/awb. The probe units of verify.sh carry the same three properties and verify.sh checks what the unit sees."""
    units = buildrun.SystemdUnits("awb-build")
    spec = buildrun.Spec("awb-task-B-012", ["/opt/claude-build/bin/claude", "-p"], 5400, "/srv/awb-build/wt/B-012-1",
                         "/srv/awb-build/home/B-012-1", rw=["/srv/awb-build/wt/B-012-1"],
                         hidden=["/srv/awb-build/runs"], allow=["127.0.0.53", "192.0.2.10"])
    argv = units.argv(spec)
    i = argv.index(SHM_PROPS[0])
    assert argv[i:i + 4] == SHM_PROPS and argv.index("--") > i
    assert "--property=InaccessiblePaths=-/etc/awb -/dev/mqueue -/srv/awb-build/runs" in argv
    assert buildrun.SHM_FS == "/dev/shm:size=64M,mode=1777,nosuid,nodev"
    # the units the runner started in a whole run: the prepare, the session, the handover, the suite and the clean
    # unit, each with the three properties and /dev/mqueue hidden
    run = world.run("good")
    assert run.state == "done"
    kinds = {s.name.split("-")[4] if s.name.count("-") == 4 else "session" for s in world.units.started}
    assert {"prep", "session", "hand", "suite"} <= kinds
    for s in world.units.started:
        a = units.argv(s)
        assert all(p in a for p in SHM_PROPS), s.name
        hidden = [x for x in a if x.startswith("--property=InaccessiblePaths=")]
        assert len(hidden) == 1 and hidden[0].startswith("--property=InaccessiblePaths=-/etc/awb -/dev/mqueue"), s.name
    # the probe unit sees the same
    probe = units.argv(buildrun.probe_spec(world.host, ["/usr/bin/id", "-un"], ["192.0.2.10"]))
    assert all(p in probe for p in SHM_PROPS) and "-/dev/mqueue" in [x for x in probe if "Inaccessible" in x][0]
    text = VERIFY.read_text(encoding="utf-8")
    func = re.search(r"(?ms)^probe\(\) \{\n.*?^\}\n", text).group(0)
    for prop in ("PrivateIPC=yes", "TemporaryFileSystem=/dev/shm:size=64M,mode=1777,nosuid,nodev", "InaccessiblePaths=-/dev/mqueue"):
        assert "--property=%s" % prop in func, prop
    want = "the probe unit sees an empty /dev/shm of at most 64 MB and no /dev/mqueue"
    assert text.count('"%s"' % want) == 2     # the dry run's list and the check itself
    assert "ls -A /dev/shm" in text and "-le 65536" in text and "! ls /dev/mqueue" in text


def test_a_login_goes_back_only_with_the_same_account_and_a_later_expiry(world, tmp_path):
    """B7: the clean unit copies a login of the task over the build user's only when both name the same
    subscriptionType and the task's expires later; one of another account or without the field is never copied,
    whatever its expiry, and the unit says so with its exit. In key mode nothing is copied."""
    root = world.root
    own = plant_login(world)
    own.write_text(login_json("max", 1_900_000_000_000, TOKEN))
    good = own.read_bytes()
    home = root / "home" / "B-900-1"
    exp = 1_900_000_000_000

    def task_home(content: str | None):
        import shutil

        shutil.rmtree(home, ignore_errors=True)
        (home / ".claude").mkdir(parents=True)
        if content is not None:
            (home / buildrun.LOGIN_IN_HOME).write_text(content)

    kept, same = buildrun.LOGIN_KEPT, 0
    for content, rc in ((login_json("pro", exp + 9), kept),            # another account, later
                        (login_json("pro", exp - 9), kept),            # another account, earlier
                        (login_json(None, exp + 9), kept),             # no field
                        (login_json("", exp + 9), kept),               # an empty field
                        (login_json("max", exp - 1), same),            # the same account, not later
                        (login_json("max", exp), same),
                        ("not json", same), (None, same)):             # not a login, no login: nothing to say
        task_home(content)
        assert buildrun.unit_clean(str(root), "B-900", "1", "home", "login") == rc, content
        assert own.read_bytes() == good and not home.exists()
    # the same account and a later expiry: copied, 600
    task_home(login_json("max", exp + 9))
    assert buildrun.unit_clean(str(root), "B-900", "1", "home", "login") == 0
    back = json.loads(own.read_text())["claudeAiOauth"]
    assert (back["accessToken"], back["expiresAt"], back["subscriptionType"]) == ("fake-login-token-two", exp + 9, "max")
    assert oct(own.stat().st_mode & 0o777) == "0o600" and not home.exists()
    newer = own.read_bytes()
    # `key` leaves the login of the task home out and copies it first all the same
    task_home(login_json("pro", exp + 99))
    assert buildrun.unit_clean(str(root), "B-900", "1", "key", "login") == kept
    assert own.read_bytes() == newer and home.is_dir() and not (home / buildrun.LOGIN_IN_HOME).exists()
    # key mode: the clean unit never looks at a login, whatever it names
    for what in ("home", "key", "all"):
        task_home(login_json("max", exp + 999))
        assert buildrun.unit_clean(str(root), "B-900", "1", what) == 0
        assert own.read_bytes() == newer
    # the build user's login without the field: a task's login never goes over it
    own.write_text(login_json(None, exp, TOKEN))
    before = own.read_bytes()
    task_home(login_json(None, exp + 9))
    assert buildrun.unit_clean(str(root), "B-900", "1", "home", "login") == kept
    task_home(login_json("max", exp + 9))
    assert buildrun.unit_clean(str(root), "B-900", "1", "home", "login") == kept
    assert own.read_bytes() == before


def test_a_refused_copy_is_one_line_of_the_result_and_a_refreshed_login_of_the_same_account_goes_back(world, tmp_path):
    """B7 through the runner: the stand-in client refreshes the login of its task home with every field kept. With
    the subscriptionType there the refreshed login goes back and the result says nothing; without it the build
    user's login stays as it was and result.md and checks.log carry the one fixed line, never the token."""
    world.host.model = "login"
    login = plant_login(world)
    login.write_text(login_json("max", 1_900_000_000_000, TOKEN))
    run = world.run("refresh", extra=cred(login))
    assert run.state == "done"
    back = json.loads(login.read_text())["claudeAiOauth"]
    assert back["accessToken"] == TOKEN + "-refreshed" and back["subscriptionType"] == "max"
    result = (run.folder / "result.md").read_text(encoding="utf-8")
    assert buildrun.LOGIN_KEPT_LINE not in result and "- login:" not in result
    nowhere(world, run, TOKEN)
    # a login without the field, refreshed by the task: not copied, said once
    login = plant_login(world)
    login.write_text(login_json(None, 1_900_000_000_000, TOKEN))
    before = login.read_bytes()
    run = world.run("refresh", extra=cred(login))
    assert run.state == "done"
    assert login.read_bytes() == before
    result = (run.folder / "result.md").read_text(encoding="utf-8")
    checks = (run.folder / "checks.log").read_text(encoding="utf-8")
    assert result.count(buildrun.LOGIN_KEPT_LINE) == 1 and checks.count(buildrun.LOGIN_KEPT_LINE) == 1
    assert "another account or none" in buildrun.LOGIN_KEPT_LINE
    nowhere(world, run, TOKEN)
    clean = [s for s in world.units.started if s.argv[3:4] == ["clean"]]
    assert clean and all(s.argv[-1] == "login" for s in clean)
    # key mode: the clean unit is started without `login` and nothing goes back
    key = tmp_path / "model.key"
    key.write_text("fake-model-key-for-tests\n")
    key.chmod(0o400)
    world.host.model, world.host.model_key = "key", key
    world.units.started.clear()
    run = world.run("key", extra="KEY: fake-model-key-for-tests\n")
    assert run.state == "done"
    clean = [s for s in world.units.started if s.argv[3:4] == ["clean"]]
    assert clean and all(s.argv[-1] != "login" for s in clean)
    assert login.read_bytes() == before
    assert buildrun.LOGIN_KEPT_LINE not in (run.folder / "result.md").read_text(encoding="utf-8")
