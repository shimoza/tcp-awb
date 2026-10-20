"""K4 (docs/reviews/REVIEW-2026-10-20.md), built across two users: a session's project is the granted project whose
key token it presents, never the code its request names. The key service runs as the owner and the sessions as the
work user, so the binding reads nothing of the caller's process (PR 69 did and was reverted). Temporary vault and
invented codes only."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import cli, config, grants, projects
from awb.tcp import keys
from tests.test_keys import PROJECT, call, load, osvc, calls_logged_here, no_waiting  # noqa: F401

OTHER = "tcp-zz99"
OWN_BUCKET = PROJECT + "-lab-1"
FOREIGN_BUCKET = OTHER + "-lab-1"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
FOLDER_LINE = "a call to a test tenant runs from the folder of an active project"
NOBODY = 65534


@pytest.fixture
def bound(osvc):
    """The service knows two granted projects, each with the key token its grant recorded."""
    svc, fake = osvc
    load(svc)
    rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._grant_tokens = lambda: {PROJECT: TOKEN_A, OTHER: TOKEN_B}
    svc._console_uid = lambda: None
    for b in (OWN_BUCKET, FOREIGN_BUCKET):
        fake.buckets.setdefault(b, {"policy": None, "objects": {}})
    return svc, fake, os.getuid() + 1


def obs_read(bucket, project, **extra):
    return dict({"op": "call", "tenant": "test-1", "method": "GET", "service": "obs", "path": "/%s/notes.txt" % bucket,
                 "project": project}, **extra)


# --------------------------------------------------------------------------- the token of a project folder

def _git_folder(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "SCOPE.md").write_text("- code: %s\n" % PROJECT)
    return root


def test_a_key_token_is_written_once_kept_out_of_git_and_read_back(tmp_path):
    folder = _git_folder(tmp_path / PROJECT)
    token = projects.write_key_token(folder)
    f = folder / projects.BIND_FILE
    assert len(token) == 64 and f.stat().st_mode & 0o777 == 0o640
    assert projects.read_key_token(folder) == token
    assert (folder / ".git").stat().st_mode & 0o750 == 0o750               # the owner reads it as OPEN.md
    status = subprocess.run(["git", "status", "--porcelain"], cwd=folder, capture_output=True, text=True)
    assert status.returncode == 0 and "token" not in status.stdout
    with pytest.raises(FileExistsError):
        projects.write_key_token(folder)
    assert projects.read_key_token(folder) == token


def test_a_linked_or_malformed_token_file_is_no_token(tmp_path):
    folder = _git_folder(tmp_path / PROJECT)
    (tmp_path / "elsewhere").write_text(TOKEN_A)
    (folder / projects.BIND_FILE).symlink_to(tmp_path / "elsewhere")
    assert projects.read_key_token(folder) is None
    (folder / projects.BIND_FILE).unlink()
    (folder / projects.BIND_FILE).write_text("not a token\n")
    assert projects.read_key_token(folder) is None


def test_the_client_presents_the_token_of_its_own_folder_and_the_work_user_writes_a_missing_one(tmp_path, monkeypatch):
    folder = _git_folder(tmp_path / PROJECT)
    (folder / "sub").mkdir()
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert keys.own_token(folder / "sub") is None                      # the owner never writes one
    assert not (folder / projects.BIND_FILE).exists()
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    token = keys.own_token(folder / "sub")                              # a project spawned before the token
    assert token == projects.read_key_token(folder)
    assert keys.own_token(folder) == token
    assert keys.own_token(tmp_path) is None                             # outside every project folder


def test_a_call_and_a_lease_carry_the_token_and_nothing_else_does(osvc, tmp_path, monkeypatch):
    svc, _fake = osvc
    folder = _git_folder(tmp_path / PROJECT)
    token = projects.write_key_token(folder)
    seen = []
    svc._caller_project = lambda req, uid: seen.append((req.get("op"), req.get("token"))) or keys._NOT_ASKED
    monkeypatch.chdir(folder)
    keys.request(svc.call_path, {"op": "ping"})
    call(svc, method="GET", service="obs", path="/" + OWN_BUCKET, project=PROJECT)
    keys.request(svc.call_path, {"op": "lease", "tenant": "test-1", "minutes": 1})
    assert seen == [("ping", None), ("call", token), ("lease", token)]


def test_spawn_writes_the_token_and_the_owners_grant_records_it(home, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    pr = projects.spawn(home, "project", "a lab for the token", None, home.register)
    token = projects.read_key_token(Path(pr.path))
    assert token is not None
    assert cli.main(["projects", "kind", pr.code, "project"]) == 0
    assert grants.token(home, pr.code) == token and grants.tokens(home) == {pr.code: token}
    assert token not in capsys.readouterr().out
    assert (home.vault / grants.TOKENS_NAME).stat().st_mode & 0o777 == 0o600
    assert cli.main(["projects", "kind", pr.code, "query"]) == 0
    assert grants.token(home, pr.code) is None                          # a query has no tenant and no token


def test_a_grant_without_a_token_in_the_folder_says_what_to_do(home, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    pr = projects.spawn(home, "project", "a lab spawned before the token", None, home.register)
    (Path(pr.path) / projects.BIND_FILE).unlink()
    assert cli.main(["projects", "kind", pr.code, "project"]) == 0
    assert "holds no key token yet" in capsys.readouterr().out
    assert grants.token(home, pr.code) is None


# --------------------------------------------------------------------------- the binding in the key service

def test_the_caller_is_the_project_of_the_token_never_the_code_it_names(bound):
    svc, _fake, uid = bound
    assert svc._caller_project({"token": TOKEN_A, "project": OTHER}, uid) == PROJECT
    assert svc._caller_project({"token": TOKEN_B}, uid) == OTHER
    assert svc._caller_project({"token": "c" * 64, "project": PROJECT}, uid) is None
    assert svc._caller_project({"project": PROJECT}, uid) is None
    assert svc._caller_project({"token": ["x"]}, uid) is None


def test_the_owner_and_the_console_are_not_bound(bound):
    svc, _fake, uid = bound
    assert svc._caller_project({}, os.getuid()) is keys._NOT_ASKED
    svc._console_uid = lambda: uid + 1
    assert svc._caller_project({}, uid + 1) is keys._NOT_ASKED


def test_a_session_of_one_project_naming_another_is_refused_before_anything_is_signed(bound):
    svc, fake, uid = bound
    req = obs_read(FOREIGN_BUCKET, OTHER)
    for caller in (PROJECT, None):
        with pytest.raises(keys.Refused, match=FOLDER_LINE):
            svc._call(dict(req), uid, caller)
    assert fake.calls == []
    assert svc._call(dict(req), uid, OTHER)["ok"]                        # the session of that project itself


def test_a_session_bound_to_its_project_still_reads_only_its_own_buckets(bound):
    svc, fake, uid = bound
    with pytest.raises(keys.Refused, match="an OBS read reaches only a bucket named after the project"):
        svc._call(obs_read(FOREIGN_BUCKET, PROJECT), uid, PROJECT)
    assert fake.calls == []
    assert svc._call(obs_read(OWN_BUCKET, PROJECT), uid, PROJECT)["status"] == 404


def test_a_lease_for_another_project_is_refused(bound):
    svc, fake, uid = bound
    with pytest.raises(keys.Refused, match="a lease is for the folder of an active project"):
        svc._lease({"tenant": "test-1", "minutes": 15, "project": OTHER}, uid, PROJECT)
    assert fake.calls == []


def test_a_grant_without_a_token_names_the_owners_command(bound):
    svc, _fake, uid = bound
    svc._grant_tokens = lambda: {OTHER: TOKEN_B}
    with pytest.raises(keys.Refused, match="the grant of %s holds no key token: the owner runs awb projects kind %s "
                                           "project once more" % (PROJECT, PROJECT)):
        svc._call(obs_read(OWN_BUCKET, PROJECT), uid, None)


def test_the_owners_cli_reaches_every_project_through_the_socket(bound):
    svc, _fake, _uid = bound
    for bucket, project in ((OWN_BUCKET, PROJECT), (FOREIGN_BUCKET, OTHER), (FOREIGN_BUCKET, PROJECT)):
        a = keys.request(svc.call_path, obs_read(bucket, project))
        assert a["ok"] and a["status"] == 404, a


# --------------------------------------------------------------------------- two users, as on the host

def _as_nobody(cwd: Path, sock: Path, reqs: list[dict]) -> list[dict]:
    """The requests sent by a child process of another uid (a session of the work user), from `cwd`."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setgroups([])
            os.setgid(NOBODY)
            os.setuid(NOBODY)
            os.chdir(cwd)
            out = [keys.request(sock, dict(q), timeout=30) for q in reqs]
            os.write(w, json.dumps(out).encode())
        except BaseException as err:                                    # the parent reads the reason
            os.write(w, json.dumps({"child": repr(err)}).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 1 << 16):
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    out = json.loads(data)
    assert isinstance(out, list), out
    return out


@pytest.mark.skipif(os.geteuid() != 0, reason="two uids need root to fork a child as another user")
# the child only sends requests on a socket and exits; the service threads of the parent are not touched in it
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_across_two_uids_a_session_reaches_its_own_project_and_never_another(bound, monkeypatch):
    svc, fake, _uid = bound
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    os.chmod(svc.call_path.parent, 0o755)
    os.chmod(svc.call_path, 0o666)
    root = Path(tempfile.mkdtemp(prefix="awbk4", dir="/tmp"))
    try:
        os.chmod(root, 0o755)
        folders = {}
        for code, token in ((PROJECT, TOKEN_A), (OTHER, TOKEN_B)):
            f = root / code
            (f / ".git").mkdir(parents=True)
            (f / "sub").mkdir()
            (f / "SCOPE.md").write_text("- code: %s\n" % code)
            (f / projects.BIND_FILE).write_text(token + "\n")
            os.chmod(f / projects.BIND_FILE, 0o640)
            os.chown(f / projects.BIND_FILE, NOBODY, NOBODY)     # the work user's file, as spawn leaves it
            folders[code] = f
        answers = _as_nobody(folders[PROJECT] / "sub", svc.call_path, [
            obs_read(OWN_BUCKET, PROJECT),                               # its own project: signed
            obs_read(FOREIGN_BUCKET, OTHER),                             # it names the other project
            obs_read(FOREIGN_BUCKET, PROJECT),                           # its own code, the other's bucket
            obs_read(FOREIGN_BUCKET, OTHER, token="c" * 64),             # a guessed token
        ])
        assert answers[0]["ok"] and answers[0]["status"] == 404, answers[0]
        assert answers[1] == {"ok": False, "error": FOLDER_LINE}
        assert answers[2]["ok"] is False and "an OBS read reaches only a bucket named after" in answers[2]["error"]
        assert answers[3] == {"ok": False, "error": FOLDER_LINE}
        assert len(fake.calls) == 1
        outside = _as_nobody(root, svc.call_path, [obs_read(OWN_BUCKET, PROJECT)])
        assert outside == [{"ok": False, "error": FOLDER_LINE}]
        assert len(fake.calls) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
