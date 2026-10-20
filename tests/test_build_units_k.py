"""Review of 2026-10-20, findings K8 and K22: a login of the task goes back as its token fields only and never far
ahead; a task file that breaks the parser does not keep the task home."""
import json
import shutil

from awb import buildrun
from tests.test_build_runner import TOKEN, plant_login, seeded, world  # noqa: F401

EXP = 1_900_000_000_000


def login(expires: int, token: str = "fake-login-token-two", **extra) -> str:
    oauth = {"accessToken": token, "refreshToken": token + "-r", "expiresAt": expires, "subscriptionType": "max"}
    oauth.update(extra)
    return json.dumps({"claudeAiOauth": oauth})


def task_home(world, content: str | bytes):
    home = world.root / "home" / "B-901-1"
    shutil.rmtree(home, ignore_errors=True)
    (home / ".claude").mkdir(parents=True)
    f = home / buildrun.LOGIN_IN_HOME
    f.write_bytes(content if isinstance(content, bytes) else content.encode())
    return home


def test_k8_only_the_token_fields_go_back_and_the_old_object_keeps_the_rest(world):
    own = plant_login(world)
    own.write_text(login(EXP, TOKEN, accountUuid="acct-one", note="kept"))
    home = task_home(world, login(EXP + 9, accountUuid="acct-one", extra_key="planted", subscriptionType="max"))
    assert buildrun.unit_clean(str(world.root), "B-901", "1", "home", "login") == 0
    back = json.loads(own.read_text())["claudeAiOauth"]
    assert back["accessToken"] == "fake-login-token-two" and back["expiresAt"] == EXP + 9
    assert back["note"] == "kept" and "extra_key" not in back and not home.exists()


def test_k8_an_expiry_far_ahead_and_another_account_id_are_not_copied(world):
    own = plant_login(world)
    own.write_text(login(EXP, TOKEN, accountUuid="acct-one"))
    before = own.read_bytes()
    for content in (login(EXP + buildrun.LOGIN_EXPIRY_STEP_MS + 1, accountUuid="acct-one"),
                    login(EXP + 9, accountUuid="acct-two"),
                    login(EXP + 9)):
        task_home(world, content)
        assert buildrun.unit_clean(str(world.root), "B-901", "1", "home", "login") == buildrun.LOGIN_KEPT
        assert own.read_bytes() == before
    task_home(world, login(EXP + buildrun.LOGIN_EXPIRY_STEP_MS, accountUuid="acct-one"))
    assert buildrun.unit_clean(str(world.root), "B-901", "1", "home", "login") == 0
