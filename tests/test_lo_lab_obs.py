"""Task LO (B-030): large objects to and from OBS in a lab run, moved by the key service as a job
(awb/tcp/labobs.py, the op lab_obs, labclient.obs_put|obs_get|obs_status|obs_abort, `awb lab obs`).

IAM and OBS are the in-memory stand-in tests/lab_fake.py FakeObsLab: no socket to 127.0.0.1, so these tests run
without a loopback. The key service listens on Unix sockets of a short folder. Every login, password, key and id
here is invented and built from pieces at run time.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import config, labclient
from awb.tcp import keys, labadmin, labobs, xchg
from tests.lab_fake import FakeObsLab
from tests.test_keys import short_dir

LAB = "test-10491"
LAB2 = "test-10497"
NOT_LAB = "test-10498"
PROJECT = "tcp-ab2c"
OTHER = "tcp-zz99"
BUCKET = PROJECT + "-lab-1"
KEY = "images/disk.vhdx"
LOGIN = {"domain": "lab-account-" + "fixture", "user": "lab-admin-" + "fixture",
         "password": "-".join(("Invented", "Admin", "4711"))}
ADMIN_STORE = {"%s/%s" % (a, k): v for a in (LAB, LAB2) for k, v in LOGIN.items()}
LAB_AK = "AKLAB" + "PERMANENT" + "FIXTURE"
TENANT_STORE = {"%s/lab/ak" % LAB: LAB_AK, "%s/lab/sk" % LAB: "sk-" + "lab-" + "permanent"}
SETTINGS = {"lab_admin": "%s %s" % (LAB, LAB2)}
SESSION = os.getuid() + 1
PART = 256 * 1024


def _bytes(n: int, seed: int = 7) -> bytes:
    return random.Random(seed).randbytes(n)


def _service(d: Path, tmp: Path, name: str, settings: dict | None = None) -> keys.Service:
    svc = keys.Service(d / ("admin-%s.sock" % name), d / ("call-%s.sock" % name), endpoint="https://lab.invalid",
                       log_dir=tmp / "log",
                       paths_fn=lambda: SimpleNamespace(vault=tmp / "vault", projects_root=tmp / "projects"))
    svc._registered = lambda: {LAB: ("eu-de",), LAB2: ("eu-de",), NOT_LAB: ("eu-de",)}
    svc._active_project = lambda code: code in (PROJECT, OTHER)
    rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._console_uid = lambda: None
    svc.start()
    keys.unlock(svc.admin_path, sorted(TENANT_STORE), reader=lambda e: TENANT_STORE[e],
                settings=dict(SETTINGS, **(settings or {})), admin_reader=lambda e: ADMIN_STORE[e])
    return svc


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = FakeObsLab(LOGIN)
    monkeypatch.setattr(labadmin, "_http", fake.http)
    monkeypatch.setattr(labobs, "_stream", fake.stream)
    monkeypatch.setattr(labobs, "PAUSES", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(labobs, "PART_SIZE", PART)
    monkeypatch.setattr(labobs, "STOP_WAIT", 5.0)
    monkeypatch.setattr(labadmin, "END_PAUSE", 0.0)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    folder = tmp_path / "projects" / PROJECT
    (folder / "notes" / "lab-runs").mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# %s\n\n- code: %s\n" % (PROJECT, PROJECT), encoding="utf-8")
    with short_dir() as d:
        w = SimpleNamespace(fake=fake, folder=folder, tmp=tmp_path, d=d, services=[])

        def service(name="a", settings=None):
            s = _service(d, tmp_path, name, settings)
            w.services.append(s)
            return s

        w.service = service
        w.svc = service()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(w.svc.call_path))
        try:
            yield w
        finally:
            for s in w.services:
                s.stop()


def start_run(w, tenant=LAB, svc=None) -> str:
    a = (svc or w.svc)._call_socket({"op": "lab_run", "action": "start", "tenant": tenant, "project": PROJECT},
                                    SESSION, caller=PROJECT)
    assert a["ok"], a
    return a["run"]


def make_bucket(w, run, bucket=BUCKET):
    a = w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "PUT",
                            "service": "obs", "path": "/" + bucket}, SESSION, caller=PROJECT)
    assert a["ok"] and a["created"] == ["obs bucket"], a


def log_path(w, run) -> Path:
    return w.folder / "notes" / "lab-runs" / ("%s.log" % run)


def lab_obs(w, action, run, file=None, *, svc=None, bucket=BUCKET, key=KEY, tenant=LAB, fds=None, project=PROJECT,
            **extra) -> dict:
    """One lab_obs request of the session, the file and the run log passed as descriptors (what _serve hands on)."""
    req = dict({"op": "lab_obs", "action": action, "tenant": tenant, "project": project, "run": run,
                "bucket": bucket, "key": key}, **extra)
    opened = []
    if fds is None and file is not None:
        opened.append(os.open(file, os.O_RDONLY if action == "put" else os.O_RDWR | os.O_CREAT, 0o600))
        opened.append(os.open(log_path(w, run), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
        fds = opened
    try:
        return (svc or w.svc)._call_socket(req, SESSION, caller=PROJECT, fds=list(fds or []))
    finally:
        for fd in opened:
            os.close(fd)


def record(w, job: str) -> dict:
    return json.loads((labobs.jobs_dir(w.svc.paths_fn()) / ("%s.json" % job)).read_text(encoding="utf-8"))


def wait(w, job: str, svc=None) -> dict:
    live = (svc or w.svc).obs_jobs.live.get(job)
    if live is not None and live.thread is not None:
        live.thread.join(30)
        assert not live.thread.is_alive()
    return record(w, job)


def a_file(w, n=PART * 5 + 1234, name="disk.vhdx", seed=7) -> tuple[Path, bytes]:
    data = _bytes(n, seed)
    path = w.folder / name
    path.write_bytes(data)
    return path, data


def run_record(w, run) -> dict:
    return json.loads((labadmin.runs_dir(w.svc.paths_fn()) / ("%s.json" % run)).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- requirement 2 and 5: multipart, verified


def test_lo_a_put_sends_the_parts_completes_and_verifies_size_and_hash(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    a = lab_obs(w, "put", run, path)
    assert a["ok"] and a["parts"] == 6 and a["part_size"] == PART and a["resumed"] is False, a
    job = wait(w, a["job"])
    want = hashlib.sha256(data).hexdigest()
    assert job["state"] == "done" and job["error"] is None, job
    assert job["sha256"] == want and job["done"] == job["size"] == len(data)
    obj = w.fake.buckets[BUCKET]["objects"][KEY]
    assert obj["data"] == data and obj["meta"]["x-obs-meta-sha256"] == want
    assert w.fake.parts == [1, 2, 3, 4, 5, 6]
    assert w.fake.buckets[BUCKET]["uploads"] == {}
    made = {c["label"]: c["state"] for c in run_record(w, run)["created"]}
    assert made == {"obs bucket": "live", "obs object": "live", "obs multipart": "removed"}
    rows = lab_obs(w, "status", run)["jobs"]
    assert [(r["id"], r["state"], r["done"], r["size"], r["parts_done"]) for r in rows] == [
        (a["job"], "done", len(data), len(data), 6)]
    assert isinstance(rows[0]["rate"], int) and "upload_id" not in rows[0]


def test_lo_the_run_log_records_the_job_without_a_key(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    job = wait(w, lab_obs(w, "put", run, path)["job"])
    log = log_path(w, run).read_text(encoding="utf-8")
    assert "obs job %s: put done" % job["id"] in log
    for line in ("object: obs://%s/%s" % (BUCKET, KEY), "size: %d bytes" % len(data),
                 "sha256: %s" % hashlib.sha256(data).hexdigest(), "duration: ", "state: done"):
        assert line in log, line
    for ak, (sk, st) in w.fake.creds.items():
        assert ak not in log and sk not in log and st not in log
    assert LAB_AK not in log and job["upload_id"] not in log and LOGIN["password"] not in log


def test_lo_a_cut_connection_sends_the_part_again(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    w.fake.cut = {3: 2}
    job = wait(w, lab_obs(w, "put", run, path)["job"])
    assert job["state"] == "done", job
    assert w.fake.parts == [1, 2, 3, 3, 3, 4, 5, 6]
    assert w.fake.buckets[BUCKET]["objects"][KEY]["data"] == data


def test_lo_a_put_broken_for_good_is_interrupted_and_the_same_put_resumes(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    w.fake.cut = {4: 99}
    first = lab_obs(w, "put", run, path)
    job = wait(w, first["job"])
    assert job["state"] == "interrupted" and "put the same file again to resume" in job["error"], job
    assert sorted(job["etags"]) == ["1", "2", "3"] and job["done"] == 3 * PART
    assert w.fake.parts == [1, 2, 3] + [4] * (len(labobs.PAUSES) + 1)
    w.fake.cut, w.fake.parts = {}, []
    again = lab_obs(w, "put", run, path)
    assert again["ok"] and again["resumed"] is True and again["job"] == first["job"] and again["parts_done"] == 3
    job = wait(w, again["job"])
    assert job["state"] == "done" and w.fake.parts == [4, 5, 6]
    assert w.fake.buckets[BUCKET]["objects"][KEY]["data"] == data
    assert sum(1 for m, _p, q in w.fake.calls if m == "POST" and "uploads" in q) == 1
    # another file to the same object is a new job, never a resume
    path.write_bytes(data[:-1] + b"x")
    os.utime(path, ns=(1, 1))
    other = lab_obs(w, "put", run, path)
    assert other["resumed"] is False and other["job"] != first["job"]
    assert wait(w, other["job"])["state"] == "done"


def test_lo_the_part_plan_keeps_to_the_limits_of_obs():
    for size in (1, 100 * 1024, 10 * 1024 ** 3, 128 * 10 ** 9, 128 * 1024 ** 3, 200 * 10 ** 9, 5 * 1024 ** 3 * 9999):
        part, parts = labobs.plan(size)
        assert labobs.PART_MIN <= part <= labobs.PART_MAX and 1 <= parts <= labobs.MAX_PARTS, size
        assert part * parts >= size > part * (parts - 1), size
    assert labobs.plan(128 * 10 ** 9) == (64 * 1024 * 1024, 1908)
    with pytest.raises(labobs._Failed):
        labobs.plan(labobs.PART_MAX * labobs.MAX_PARTS + 1)


# --------------------------------------------------------------------------- requirement 1: the rules of a lab call


def test_lo_refused_without_the_open_run_of_the_project(world):
    w = world
    path, _data = a_file(w)
    (w.folder / "notes" / "lab-runs" / "run-20260101-000000-abcd.log").touch()
    a = lab_obs(w, "put", "run-20260101-000000-abcd", path)
    assert not a["ok"] and a["error"] == labadmin.NO_RUN_LINE % (PROJECT, LAB, LAB)
    run = start_run(w)
    b = lab_obs(w, "put", run, path, tenant=LAB2)
    assert not b["ok"] and b["error"] == labadmin.NO_RUN_LINE % (PROJECT, LAB2, LAB2)
    assert w.fake.calls == [] and labobs.load_jobs(w.svc.paths_fn()) == []


def test_lo_refused_on_a_tenant_that_is_no_lab_tenant(world):
    w = world
    run = start_run(w)
    path, _data = a_file(w)
    a = lab_obs(w, "put", run, path, tenant=NOT_LAB)
    assert not a["ok"] and a["error"] == labadmin.TENANT_LINE % (NOT_LAB, " and ".join(labadmin.ALLOWED))
    assert w.fake.calls == []


def test_lo_refused_on_a_read_only_tenant(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, _data = a_file(w)
    w.svc._readonly = lambda alias: {"auth": "key", "regions": ("eu-de",)} if alias == LAB else None
    a = lab_obs(w, "put", run, path)
    assert not a["ok"] and a["error"] == labadmin.TENANT_LINE % (LAB, " and ".join(labadmin.ALLOWED)), a
    assert labobs.load_jobs(w.svc.paths_fn()) == []


def test_lo_refused_on_a_bucket_not_named_after_the_project(world):
    w = world
    run = start_run(w)
    path, _data = a_file(w)
    for bucket in (OTHER + "-lab-1", "public-images", PROJECT + "x-lab"):
        a = lab_obs(w, "put", run, path, bucket=bucket)
        assert not a["ok"] and "only a bucket named after the project (%s-...)" % PROJECT in a["error"], bucket
    assert w.fake.calls == [] and labobs.load_jobs(w.svc.paths_fn()) == []


def test_lo_refused_on_an_exchange_bucket(world):
    w = world
    svc = w.service("x", {"lab_bucket": PROJECT + "-xchg"})
    run = start_run(w, svc=svc)
    path, _data = a_file(w)
    a = lab_obs(w, "put", run, path, svc=svc, bucket=PROJECT + "-xchg")
    assert not a["ok"] and a["error"] == "the exchange buckets are reached through awb xchg only"
    assert w.fake.calls == []


def test_lo_refused_for_another_project_than_the_session_s(world):
    w = world
    run = start_run(w)
    path, _data = a_file(w)
    a = lab_obs(w, "put", run, path, project=OTHER, bucket=OTHER + "-lab-1")
    assert not a["ok"] and "folder of an active project" in a["error"]


def test_lo_signed_with_the_run_s_temporary_credential_only(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, _data = a_file(w)
    job = wait(w, lab_obs(w, "put", run, path)["job"])
    assert job["state"] == "done", job
    assert w.fake.aks and set(w.fake.aks) <= set(w.fake.creds) and LAB_AK not in w.fake.aks
    assert all(ak.startswith("TMPAK") for ak in w.fake.aks)


# --------------------------------------------------------------------------- requirement 4: the descriptor


def test_lo_a_file_the_work_user_cannot_read_is_refused_and_no_path_is_taken(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    (w.folder / "notes" / "lab-runs" / ("%s.open" % LAB)).write_text(run + "\n", encoding="utf-8")
    path, _data = a_file(w, name="locked.vhdx")
    os.chmod(path, 0)
    assert not os.access(path, os.R_OK), "the tests run as a user the file mode binds"
    with pytest.raises(labclient.LabClientError, match="cannot read"):
        labclient.obs_put(path, "obs://%s/%s" % (BUCKET, KEY), start=w.folder)
    os.chmod(path, 0o600)
    a = lab_obs(w, "put", run, None, path=str(path), fds=[])
    assert not a["ok"] and "passes the open file over the socket (SCM_RIGHTS), never a path" in a["error"]
    fd = os.open(path, os.O_WRONLY)
    log = os.open(log_path(w, run), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        b = lab_obs(w, "put", run, fds=[fd, log])
    finally:
        os.close(fd)
        os.close(log)
    assert not b["ok"] and b["error"] == "refused: the file of a put is not open for reading"
    assert w.fake.calls[-1][0] == "PUT" and labobs.load_jobs(w.svc.paths_fn()) == []


def test_lo_a_get_writes_only_inside_the_project_folder_or_the_scratch_folder(world, monkeypatch):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    wait(w, lab_obs(w, "put", run, path)["job"])
    outside = w.tmp / "elsewhere"
    outside.mkdir()
    a = lab_obs(w, "get", run, outside / "x.vhdx")
    assert not a["ok"] and a["error"] == ("refused: a get writes only inside the project folder or the session "
                                          "scratch folder")
    scratch = w.tmp / ("scratch-%d" % SESSION)
    scratch.mkdir()
    monkeypatch.setattr(labobs, "SCRATCH", str(w.tmp / "scratch-%d"))
    ok = lab_obs(w, "get", run, scratch / "x.vhdx")
    assert ok["ok"], ok
    assert wait(w, ok["job"])["state"] == "done" and (scratch / "x.vhdx").read_bytes() == data
    ro = os.open(w.folder / "ro.vhdx", os.O_RDONLY | os.O_CREAT, 0o600)
    log = os.open(log_path(w, run), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        b = lab_obs(w, "get", run, fds=[ro, log])
    finally:
        os.close(ro)
        os.close(log)
    assert not b["ok"] and b["error"] == "refused: the file of a get is open for reading and writing"


def test_lo_labclient_passes_the_file_over_the_socket_and_the_commands_work(world, monkeypatch, capsys):
    w = world
    monkeypatch.chdir(w.folder)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    capsys.readouterr()
    status, _h, _b = labclient.call("PUT", "obs", "/" + BUCKET)
    assert status == 200
    path, data = a_file(w)
    assert labclient.obs_main(["put", str(path), "obs://%s/%s" % (BUCKET, KEY)]) == 0
    out = capsys.readouterr().out
    job = out.split()[1]
    assert labobs.JOB_RE.match(job) and "started: put of %d bytes in 6 parts" % len(data) in out
    assert labclient.obs_wait(job, every=0.01)["state"] == "done"
    assert labclient.obs_main(["status", job]) == 0
    out = capsys.readouterr().out
    assert "%s  put obs://%s/%s  done" % (job, BUCKET, KEY) in out and hashlib.sha256(data).hexdigest() in out
    back = w.folder / "evidence" / "back.vhdx"
    back.parent.mkdir()
    got = labclient.obs_get("obs://%s/%s" % (BUCKET, KEY), back)
    assert labclient.obs_wait(got["job"], every=0.01)["state"] == "done" and back.read_bytes() == data
    with pytest.raises(labclient.LabClientError, match="only inside the project folder"):
        labclient.obs_get("obs://%s/%s" % (BUCKET, KEY), w.tmp / "x.vhdx")
    assert labclient.obs_main(["get", "obs://%s/missing" % BUCKET, str(w.folder / "m.vhdx")]) == 2
    assert not (w.folder / "m.vhdx").exists()
    assert labclient.run_main(["end"]) == 0
    assert BUCKET not in w.fake.buckets
    log = next((w.folder / "notes" / "lab-runs").glob("run-*.log")).read_text(encoding="utf-8")
    assert "obs put %s obs://%s/%s" % (path, BUCKET, KEY) in log and "job: %s" % job in log
    assert "refused: obs://%s/missing is not there" % BUCKET in log
    from awb.tcp import lab

    assert lab.main(["obs", "status"]) == 0


# --------------------------------------------------------------------------- requirement 3 and 9: a job, a reload


def test_lo_a_put_answers_at_once_and_status_shows_the_running_job(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    release = threading.Event()
    w.fake.hold = {3: release}
    a = lab_obs(w, "put", run, path)
    assert a["ok"] and a["state"] == "hashing"
    assert w.fake.holding.wait(10)
    row = lab_obs(w, "status", run, job=a["job"])["jobs"][0]
    assert row["state"] == "running" and row["done"] == 2 * PART and row["size"] == len(data) and row["parts"] == 6
    release.set()
    assert wait(w, a["job"])["state"] == "done"


def test_lo_a_job_across_a_reload_is_saved_interrupted_and_the_successor_resumes_it(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    release = threading.Event()
    w.fake.hold = {3: release}
    a = lab_obs(w, "put", run, path)
    assert w.fake.holding.wait(10)
    leave = threading.Thread(target=w.svc.leave)
    leave.start()
    while not w.svc.obs_jobs.live[a["job"]].stop.is_set():
        time.sleep(0.01)
    release.set()
    leave.join(30)
    job = record(w, a["job"])
    assert job["state"] == "interrupted" and "the key service was reloaded" in job["error"], job
    assert sorted(job["etags"]) == ["1", "2"]
    assert "put interrupted" in log_path(w, run).read_text(encoding="utf-8")
    after = w.service("b")
    rows = lab_obs(w, "status", run, svc=after)["jobs"]
    assert [(r["id"], r["state"]) for r in rows] == [(a["job"], "interrupted")]
    w.fake.parts = []
    again = lab_obs(w, "put", run, path, svc=after)
    assert again["resumed"] is True and again["job"] == a["job"]
    assert wait(w, a["job"], svc=after)["state"] == "done" and w.fake.parts == [3, 4, 5, 6]
    assert w.fake.buckets[BUCKET]["objects"][KEY]["data"] == data


def test_lo_a_job_whose_process_went_away_shows_as_interrupted(world):
    w = world
    run = start_run(w)
    p = w.svc.paths_fn()
    labobs.save_job(p, {"id": "obs-20261103-101500-abcd", "run": run, "project": PROJECT, "tenant": LAB,
                        "region": "eu-de", "action": "put", "bucket": BUCKET, "key": KEY, "size": 10, "done": 4,
                        "state": "running", "pid": 4194304 + 4711, "etags": {}})
    rows = lab_obs(w, "status", run)["jobs"]
    assert rows[0]["state"] == "interrupted" and "restarted" in rows[0]["error"]
    assert record(w, "obs-20261103-101500-abcd")["state"] == "interrupted"


def test_lo_a_lock_interrupts_a_job_and_abort_stops_one_and_aborts_its_upload(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, _data = a_file(w)
    release = threading.Event()
    w.fake.hold = {2: release}
    a = lab_obs(w, "put", run, path)
    assert w.fake.holding.wait(10)
    stopper = threading.Thread(target=lambda: lab_obs(w, "abort", run, job=a["job"]))
    stopper.start()
    while not w.svc.obs_jobs.live[a["job"]].stop.is_set():
        time.sleep(0.01)
    release.set()
    stopper.join(30)
    job = record(w, a["job"])
    assert job["state"] == "aborted" and job["error"] == "aborted by the session"
    assert w.fake.buckets[BUCKET]["uploads"] == {}
    assert [c["state"] for c in run_record(w, run)["created"] if c["label"] == "obs multipart"] == ["removed"]
    second, _ = a_file(w, name="other.vhdx", seed=9)
    release = threading.Event()
    w.fake.hold, w.fake.holding = {2: release}, threading.Event()
    b = lab_obs(w, "put", run, second, key="images/other.vhdx")
    assert w.fake.holding.wait(10)
    locker = threading.Thread(target=lambda: keys.request(w.svc.admin_path, {"op": "lock"}))
    locker.start()
    while not w.svc.obs_jobs.live[b["job"]].stop.is_set():
        time.sleep(0.01)
    release.set()
    locker.join(30)
    job = record(w, b["job"])
    assert job["state"] == "interrupted" and "locked" in job["error"]


# --------------------------------------------------------------------------- requirement 7: the end of the run


def test_lo_the_run_end_aborts_open_uploads_before_the_bucket_delete(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    # an upload the run does not record (a lab call of the session) and an interrupted job of the run
    a = w.svc._call_socket({"op": "lab_call", "tenant": LAB, "project": PROJECT, "run": run, "method": "POST",
                            "service": "obs", "path": "/%s/manual.bin" % BUCKET, "query": {"uploads": ""}},
                           SESSION, caller=PROJECT)
    assert a["ok"] and a["status"] == 200 and a["created"] == []
    path, _data = a_file(w)
    w.fake.cut = {2: 99}
    assert wait(w, lab_obs(w, "put", run, path)["job"])["state"] == "interrupted"
    assert len(w.fake.buckets[BUCKET]["uploads"]) == 2
    assert w.fake.buckets[BUCKET]["objects"] == {}          # a listing shows no object
    ended = w.svc._call_socket({"op": "lab_run", "action": "end", "tenant": LAB, "project": PROJECT, "run": run},
                               SESSION, caller=PROJECT)
    assert ended["ok"] and ended["failed"] == [], ended
    assert BUCKET not in w.fake.buckets
    assert sum(1 for c in ended["calls"] if c["label"] == "obs multipart" and c["status"] == 204) >= 2
    assert [c["label"] for c in ended["removed"]] == ["obs bucket", "obs object", "obs multipart"]
    jobs = labobs.load_jobs(w.svc.paths_fn())
    assert [j["state"] for j in jobs] == ["aborted"] and jobs[0]["error"] == "the lab run ended"


def test_lo_the_run_end_stops_a_running_job_first(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, _data = a_file(w)
    release = threading.Event()
    w.fake.hold = {2: release}
    a = lab_obs(w, "put", run, path)
    assert w.fake.holding.wait(10)
    answer = {}
    ender = threading.Thread(target=lambda: answer.update(w.svc._call_socket(
        {"op": "lab_run", "action": "end", "tenant": LAB, "project": PROJECT, "run": run}, SESSION, caller=PROJECT)))
    ender.start()
    while not w.svc.obs_jobs.live[a["job"]].stop.is_set():
        time.sleep(0.01)
    release.set()
    ender.join(30)
    assert answer["ok"] and answer["failed"] == [], answer
    assert BUCKET not in w.fake.buckets
    job = wait(w, a["job"])
    assert job["state"] == "aborted" and job["error"] == "the lab run ended"


# --------------------------------------------------------------------------- requirement 8: the limit and the disk


def test_lo_a_file_or_object_over_lab_obs_max_gb_is_refused_with_the_limit_named(world, tmp_path):
    w = world
    assert labobs.max_bytes({}) == (200 * labobs.GB, "200")
    svc = w.service("x", {"lab_obs_max_gb": "1"})
    run = start_run(w, svc=svc)
    big = w.folder / "big.vhdx"
    with open(big, "wb") as fh:
        fh.truncate(int(1.5 * labobs.GB))          # sparse: no disk is used
    a = lab_obs(w, "put", run, big, svc=svc)
    assert not a["ok"] and a["error"] == ("refused: the file is 1.5 GB, over the limit of 1 GB (lab_obs_max_gb in "
                                          "keys.conf)")
    w.fake.buckets[BUCKET] = {"objects": {}, "uploads": {}}
    w.fake.virtual[(BUCKET, "huge.vhdx")] = 3 * labobs.GB
    b = lab_obs(w, "get", run, w.folder / "huge.vhdx", svc=svc, key="huge.vhdx")
    assert not b["ok"] and "the object is 3.0 GB, over the limit of 1 GB (lab_obs_max_gb in keys.conf)" in b["error"]
    conf = tmp_path / "keys.conf"
    conf.write_text("lab_obs_max_gb = 150\n", encoding="utf-8")
    assert xchg.read_settings(conf)["lab_obs_max_gb"] == "150"
    conf.write_text("lab_obs_max_gb = lots\n", encoding="utf-8")
    with pytest.raises(xchg.XchgError, match="lab_obs_max_gb"):
        xchg.read_settings(conf)


def test_lo_a_get_checks_the_disk_before_it_starts(world, monkeypatch):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    w.fake.virtual[(BUCKET, "huge.vhdx")] = 2 * labobs.GB
    monkeypatch.setattr(labobs.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=1000, f_frsize=4096))
    a = lab_obs(w, "get", run, w.folder / "huge.vhdx", key="huge.vhdx")
    assert not a["ok"] and a["error"].startswith("refused: the disk of the file has 0.0 GB free, the object needs "
                                                 "2.0 GB"), a
    assert [m for m, _p, _q in w.fake.calls][-1] == "HEAD" and labobs.load_jobs(w.svc.paths_fn()) == []


# --------------------------------------------------------------------------- the get


def test_lo_a_get_goes_on_after_a_cut_resumes_and_checks_the_hash(world):
    w = world
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w)
    wait(w, lab_obs(w, "put", run, path)["job"])
    target = w.folder / "back.vhdx"
    w.fake.get_cut = 2
    job = wait(w, lab_obs(w, "get", run, target)["job"])
    assert job["state"] == "done" and target.read_bytes() == data and job["sha256"] == hashlib.sha256(data).hexdigest()
    assert w.fake.ranges[0] is None and w.fake.ranges[1] == "bytes=%d-" % (len(data) // 2)
    w.fake.down, w.fake.ranges = True, []
    target2 = w.folder / "back2.vhdx"
    first = lab_obs(w, "get", run, target2)
    job = wait(w, first["job"])
    assert job["state"] == "interrupted" and 0 < job["done"] < len(data) and "get the same file again" in job["error"]
    w.fake.down, w.fake.ranges = False, []
    again = lab_obs(w, "get", run, target2)
    assert again["resumed"] is True and again["job"] == first["job"]
    assert wait(w, first["job"])["state"] == "done" and target2.read_bytes() == data
    assert w.fake.ranges == ["bytes=%d-" % job["done"]]
    w.fake.buckets[BUCKET]["objects"][KEY]["meta"]["x-obs-meta-sha256"] = "0" * 64
    bad = wait(w, lab_obs(w, "get", run, w.folder / "back3.vhdx")["job"])
    assert bad["state"] == "failed" and "SHA-256" in bad["error"]


# Review regressions use the service entry point in process, without a socket or an installed package.
@pytest.fixture
def local_obs(tmp_path, monkeypatch):
    fake = FakeObsLab(LOGIN)
    monkeypatch.setattr(labadmin, "_http", fake.http)
    monkeypatch.setattr(labobs, "_stream", fake.stream)
    monkeypatch.setattr(labobs, "PAUSES", (0.0,))
    monkeypatch.setattr(labobs, "PART_SIZE", PART)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    folder = tmp_path / "projects" / PROJECT
    (folder / "notes" / "lab-runs").mkdir(parents=True)
    p = SimpleNamespace(vault=tmp_path / "vault", projects_root=tmp_path / "projects")
    svc = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock", endpoint="https://lab.invalid",
                       log_dir=tmp_path / "log", paths_fn=lambda: p)
    svc.settings = dict(SETTINGS)
    svc.tenants = keys.collect(sorted(TENANT_STORE), lambda e: TENANT_STORE[e])
    for alias in (LAB, LAB2):
        svc.tenants.setdefault(alias, {})["admin"] = dict(LOGIN)
    svc._registered = lambda: {LAB: ("eu-de",), LAB2: ("eu-de",)}
    svc._active_project = lambda code: code == PROJECT
    svc._project_row = lambda code: SimpleNamespace(code=code, kind="project", state="active", customer=None)
    svc._granted = lambda code: code == PROJECT
    svc._console_uid = lambda: None
    w = SimpleNamespace(svc=svc, fake=fake, folder=folder, tmp=tmp_path)
    try:
        yield w
    finally:
        svc.obs_jobs.stop_all("test finished", wait=5)


def _review_job(**changes):
    return dict({"id": "obs-20261117-120000-abcd", "project": PROJECT, "run": "run-20261117-120000-abcd",
                 "tenant": LAB, "region": "eu-de", "action": "get", "bucket": BUCKET, "key": KEY,
                 "state": "failed", "size": 4, "done": 0, "etags": {}, "file": {}}, **changes)


def _review_record(tmp_path, **changes):
    p = SimpleNamespace(vault=tmp_path / "vault")
    job = _review_job(**changes)
    folder = labobs.jobs_dir(p)
    folder.mkdir(parents=True)
    return p, job, folder / (job["id"] + ".json")


def test_lo_review_identifiers_reject_trailing_newlines(local_obs):
    w = local_obs
    for bucket, key in ((BUCKET + "\n", KEY), (BUCKET, KEY + "\n")):
        with pytest.raises(keys.Refused):
            labobs._target(w.svc, {"bucket": bucket, "key": key}, PROJECT)
    with pytest.raises(keys.Refused):
        labobs.serve(w.svc, {"action": "status", "project": PROJECT,
                            "job": _review_job()["id"] + "\n"}, SESSION, PROJECT, [])


def test_lo_review_vendor_hash_rejects_trailing_newline(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    monkeypatch.setattr(labobs, "vendor_hosts", lambda: (["files.example.org"], []))
    monkeypatch.setattr(labobs, "_file_fd", lambda *a: pytest.fail("malformed hash reached the file"))
    answer = lab_obs(w, "fetch", run, url="https://files.example.org/a", hash="sha256:" + "a" * 64 + "\n")
    assert not answer["ok"] and "vendor hash" in answer["error"]


def test_lo_review_record_id_cannot_escape_storage(tmp_path):
    p = SimpleNamespace(vault=tmp_path / "vault")
    outside = tmp_path / "victim.json"
    outside.write_text("keep")
    with pytest.raises(keys.Refused):
        labobs.save_job(p, _review_job(id=str(outside)[:-5]))
    assert outside.read_text() == "keep"


def test_lo_review_record_links_are_not_read(tmp_path):
    p, job, path = _review_record(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(job))
    path.symlink_to(outside)
    with pytest.raises(keys.Refused):
        labobs.load_jobs(p)
    assert outside.read_text() == json.dumps(job)


def test_lo_review_record_folder_links_are_not_written(tmp_path):
    p = SimpleNamespace(vault=tmp_path / "vault")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    labobs.jobs_dir(p).parent.mkdir(parents=True)
    labobs.jobs_dir(p).symlink_to(outside, target_is_directory=True)
    with pytest.raises(keys.Refused):
        labobs.save_job(p, _review_job())
    assert list(outside.iterdir()) == [] and outside.stat().st_mode & 0o777 == 0o755


def test_lo_review_record_parent_swap_stays_in_pinned_directory(tmp_path, monkeypatch):
    p, job, path = _review_record(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    held = path.parent.with_name("held")
    replace = labobs.os.replace

    def swap(src, dst, **kwargs):
        path.parent.rename(held)
        path.parent.symlink_to(outside, target_is_directory=True)
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(labobs.os, "replace", swap)
    try:
        labobs.save_job(p, job)
    except (OSError, keys.Refused):
        pass
    assert list(outside.iterdir()) == []
    assert (held / path.name).read_text() and not list(held.glob(".job.*"))


def test_lo_review_special_record_is_not_opened_for_blocking_read(tmp_path, monkeypatch):
    p, job, path = _review_record(tmp_path)
    os.mkfifo(path)
    # A real blocking FIFO open would hang the old implementation. Detect that attempt first.
    from io import open as io_open

    def checked_open(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file) == path:
            pytest.fail("attempted a blocking read of a FIFO")
        return io_open(file, *args, **kwargs)

    monkeypatch.setattr("io.open", checked_open)
    assert labobs.load_jobs(p) == []


def test_lo_review_oversized_record_is_not_loaded(tmp_path):
    p, job, path = _review_record(tmp_path)
    job["extra"] = "x" * (4 * 1024 * 1024)
    path.write_text(json.dumps(job))
    assert labobs.load_jobs(p) == []


def test_lo_review_malformed_record_fields_are_not_loaded(tmp_path):
    p, job, path = _review_record(tmp_path)
    for change in ({"file": []}, {"etags": ["x"]}, {"done": -1}, {"done": 5}, {"size": True},
                   {"action": []}, {"key": {"private": "value"}}, {"rate": {}}, {"started_ts": float("nan")}):
        path.write_text(json.dumps(dict(job, **change)))
        assert labobs.load_jobs(p) == [], change


def test_lo_review_unreadable_record_is_not_treated_as_absent(tmp_path):
    p, job, path = _review_record(tmp_path)
    path.write_text(json.dumps(job))
    path.chmod(0)
    try:
        with pytest.raises(keys.Refused):
            labobs.load_jobs(p)
    finally:
        path.chmod(0o600)


def test_lo_review_missing_and_invalid_json_records_are_ignored(tmp_path, monkeypatch):
    p = SimpleNamespace(vault=tmp_path / "vault")
    assert labobs.load_jobs(p) == []
    folder = labobs.jobs_dir(p)
    folder.mkdir(parents=True)
    path = folder / (_review_job()["id"] + ".json")
    path.write_bytes(b"{broken")
    assert labobs.load_jobs(p) == []
    # Fault check for an existing guarantee: a parser failure must never escape as a job.
    with monkeypatch.context() as fault:
        fault.setattr(labobs.json, "loads", lambda raw: _review_job())
        assert labobs.load_jobs(p) != []


def test_lo_review_append_download_descriptor_is_refused(local_obs):
    w = local_obs
    path = w.folder / "download"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with pytest.raises(keys.Refused):
            labobs._file_fd([fd], "get", SESSION, w.svc.paths_fn(), PROJECT)
    finally:
        os.close(fd)


def test_lo_review_hardlinked_download_and_log_are_refused(local_obs):
    w = local_obs
    run = start_run(w)
    for path, action in ((w.folder / "download", "get"), (log_path(w, run), "log")):
        path.touch()
        os.link(path, w.tmp / (action + "-outside"))
        fd = os.open(path, os.O_RDWR | (os.O_APPEND if action == "log" else 0))
        try:
            with pytest.raises(keys.Refused):
                if action == "log":
                    labobs._log_fd([fd, fd], w.svc.paths_fn(), PROJECT, run)
                else:
                    labobs._file_fd([fd], action, SESSION, w.svc.paths_fn(), PROJECT)
        finally:
            os.close(fd)


def test_lo_review_linked_project_does_not_authorize_outside_write(local_obs):
    w = local_obs
    outside = w.tmp / "outside-project"
    w.folder.rename(outside)
    w.folder.symlink_to(outside, target_is_directory=True)
    fd = os.open(outside / "download", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(keys.Refused):
            labobs._file_fd([fd], "get", SESSION, w.svc.paths_fn(), PROJECT)
    finally:
        os.close(fd)


def test_lo_review_failed_start_closes_duplicates_and_clears_live(local_obs, monkeypatch):
    w = local_obs
    fd = os.open(w.folder / "data", os.O_RDWR | os.O_CREAT, 0o600)
    copied = []
    dup = os.dup

    def track(source):
        new = dup(source)
        copied.append(new)
        return new

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(labobs.os, "dup", track)
    monkeypatch.setattr(labobs, "save_job", fail)
    try:
        with pytest.raises(OSError):
            labobs._start(w.svc, w.svc.paths_fn(), _review_job(), fd, fd)
        assert not w.svc.obs_jobs.live
        for copy in copied:
            with pytest.raises(OSError):
                os.fstat(copy)
    finally:
        os.close(fd)
        # Clean up even when exercising the old fault.
        for copy in copied:
            try:
                os.close(copy)
            except OSError:
                pass
        w.svc.obs_jobs.live.clear()


def test_lo_review_concurrent_admission_starts_only_one_job(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    path, _ = a_file(w, n=4)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original = labobs._start
    results = []
    errors = []

    def hold_start(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args)

    def held_work(svc, p, live, job):
        live.stop.wait(5)
        with svc.obs_jobs.lock:
            svc.obs_jobs.live.pop(live.id, None)
            getattr(labobs, "_LIVE_IDS", set()).discard(live.id)
        os.close(live.fd)
        os.close(live.log_fd)

    monkeypatch.setattr(labobs, "_start", hold_start)
    monkeypatch.setattr(labobs, "_work", held_work)

    def request(second=False):
        try:
            if second:
                second_started.set()
            results.append(lab_obs(w, "put", run, path))
        except BaseException as err:
            errors.append(err)

    first = threading.Thread(target=request)
    second = threading.Thread(target=request, args=(True,))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert second_started.wait(5)
        # Old admission lets the second request finish while the first is between check and start.
        second.join(0.2)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert len(results) == 2 and sum(a["ok"] for a in results) == 1, results


def test_lo_review_other_service_live_record_is_not_settled(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    make_bucket(w, run)
    path, _ = a_file(w, n=PART * 2)
    release = threading.Event()
    w.fake.hold = {1: release}
    answer = lab_obs(w, "put", run, path)
    try:
        assert w.fake.holding.wait(5)
        other = SimpleNamespace(obs_jobs=labobs.Jobs())
        jobs = labobs._jobs_of(other, w.svc.paths_fn(), PROJECT)
        assert jobs[0]["state"] in labobs.ACTIVE
    finally:
        release.set()
        wait(w, answer["job"])


def test_lo_review_dependency_errors_do_not_reach_record_or_log(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    path, _ = a_file(w, n=4)
    private = "unlabelled-" + "credential-" + "4711"

    def fail(*args):
        raise labadmin.LabAdminError(private)

    monkeypatch.setattr(labobs, "_put", fail)
    answer = lab_obs(w, "put", run, path)
    job = wait(w, answer["job"])
    assert job["state"] == "interrupted" and "put the same file again" in job["error"]
    assert private not in json.dumps(job) + log_path(w, run).read_text()


def test_lo_review_metadata_failure_does_not_echo_dependency_value(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    private = "private-" + "credential-" + "4711"

    def fail(*args, **kwargs):
        raise labadmin.LabAdminError(private)

    monkeypatch.setattr(labobs._Obs, "small", fail)
    answer = lab_obs(w, "get", run, w.folder / "data")
    assert not answer["ok"] and private not in answer["error"]


def test_lo_review_arbitrary_xml_error_codes_are_not_public():
    private = "Private" + "Account" + "4711"
    code = labobs._code(("<Error><Code>%s</Code></Error>" % private).encode())
    assert code and private not in code
    assert labobs._code(b"<CompleteMultipartUploadResult/>") == ""
    assert labobs._code(b"<Error><Code>NoSuchUpload</Code></Error>") == "NoSuchUpload"


def test_lo_review_status_and_log_screen_secrets(local_obs):
    """A job value is the service's own text: the secret check screens it before a row or a log line, no name check
    runs (check once at entry)."""
    w = local_obs
    secret = "Invented" + "Secret" + "4711"
    job = _review_job(key="pass" + "word=" + secret, error="pass" + "word=" + secret)
    assert secret not in json.dumps(labobs.row(job))
    path = w.folder / "checked.log"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        labobs._log_line(labobs.Live(job["id"], -1, fd), job)
    finally:
        os.close(fd)
    assert secret not in path.read_text()


def test_lo_review_a_job_value_runs_no_name_check(local_obs, monkeypatch):
    from awb import check

    def called(*args):
        raise AssertionError("the name check ran on a job value")

    monkeypatch.setattr(check, "check_text", called)
    assert "plain-object" in json.dumps(labobs.row(_review_job(key="plain-object")))


def test_lo_review_failed_secret_check_withholds_output(local_obs, monkeypatch):
    from awb import labsecrets

    def broken(*args):
        raise RuntimeError("private-" + "checker-" + "detail")

    monkeypatch.setattr(labsecrets, "shorten", broken)
    value = "private-" + "object"
    out = json.dumps(labobs.row(_review_job(key=value)))
    assert value not in out and "checker" not in out


def test_lo_review_portal_refusal_does_not_echo_private_path():
    private = "pass" + "word=" + "Invented" + "4711"
    with pytest.raises(keys.Refused) as caught:
        labobs.check_vendor_url("https://portal.example.org/" + private, [], ["portal.example.org"])
    assert private not in str(caught.value) and "vendor portal login" in str(caught.value)


def _review_download(tmp_path, monkeypatch, action, body, *, headers=None, size=4, done=0):
    import io

    p = SimpleNamespace(vault=tmp_path / "vault")
    svc = SimpleNamespace(obs_jobs=labobs.Jobs())
    path = tmp_path / "download"
    path.write_bytes(b"ab"[:done])
    fd = os.open(path, os.O_RDWR)
    job = _review_job(action=action, size=size, done=done, etag='"v1"', source="https://files.example.org/a",
                      url="https://files.example.org/a", expected=None)
    live = labobs.Live(job["id"], fd, -1)
    monkeypatch.setattr(labobs._Obs, "signed", lambda *a: ("https://obs.example.org/a", {}))
    monkeypatch.setattr(labobs, "PAUSES", (0.0,))
    response = io.BytesIO(body)
    answer = (206 if done else 200, headers or {}, response)
    monkeypatch.setattr(labobs, "_stream", lambda *a, **k: answer)
    monkeypatch.setattr(labobs, "_vendor_open", lambda *a, **k: answer)
    try:
        getattr(labobs, "_" + action)(svc, p, live, job)
    finally:
        os.close(fd)
    return job, path


def test_lo_review_download_rejects_bytes_beyond_head_size(tmp_path, monkeypatch):
    for action in ("get", "fetch"):
        with pytest.raises(labobs._Failed, match="exceeds"):
            _review_download(tmp_path, monkeypatch, action, b"extra", size=4)


def test_lo_review_download_checks_eof_at_chunk_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(labobs, "CHUNK", 4)
    for action in ("get", "fetch"):
        with pytest.raises(labobs._Failed, match="exceeds"):
            _review_download(tmp_path, monkeypatch, action, b"extra", size=4)


def test_lo_review_download_rejects_wrong_content_range(tmp_path, monkeypatch):
    for action in ("get", "fetch"):
        with pytest.raises(labobs._Failed, match="range"):
            _review_download(tmp_path, monkeypatch, action, b"cd", done=2,
                             headers={"Content-Range": "bytes 0-1/4"})


def test_lo_review_vendor_get_rechecks_content_type(tmp_path, monkeypatch):
    with pytest.raises(labobs._Failed, match="web page"):
        _review_download(tmp_path, monkeypatch, "fetch", b"html", headers={"Content-Type": "text/html"})


def test_lo_review_download_rejects_encoded_body(tmp_path, monkeypatch):
    for action in ("get", "fetch"):
        with pytest.raises(labobs._Failed, match="encoding"):
            _review_download(tmp_path, monkeypatch, action, b"gzip", headers={"Content-Encoding": "gzip"})


def test_lo_review_zero_byte_write_fails_without_spinning(tmp_path, monkeypatch):
    calls = []

    def stalled(*args):
        calls.append(1)
        if len(calls) > 1:
            pytest.fail("a zero byte write was retried forever")
        return 0

    monkeypatch.setattr(labobs.os, "pwrite", stalled)
    with pytest.raises(labobs._Failed, match="written"):
        _review_download(tmp_path, monkeypatch, "get", b"data")
    calls.clear()
    monkeypatch.setattr(labobs.os, "write", stalled)
    fd = os.open(tmp_path / "run.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        labobs._log_line(labobs.Live(_review_job()["id"], -1, fd), _review_job())
    finally:
        os.close(fd)
    assert len(calls) == 1


def test_lo_review_non_ascii_size_is_a_clean_refusal(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    monkeypatch.setattr(labobs._Obs, "small", lambda *a, **k: (200, {"Content-Length": "²"}, b""))
    answer = lab_obs(w, "get", run, w.folder / "download")
    assert not answer["ok"] and "cannot be read" in answer["error"]


def test_lo_review_put_verification_does_not_echo_header(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    make_bucket(w, run)
    path, _ = a_file(w, n=4)
    original = labobs._Obs.small
    private = "private-" + "header-" + "4711"

    def head(self, method, *args, **kwargs):
        status, hdrs, body = original(self, method, *args, **kwargs)
        if method == "HEAD" and status == 200:
            hdrs["Content-Length"] = private
        return status, hdrs, body

    monkeypatch.setattr(labobs._Obs, "small", head)
    job = wait(w, lab_obs(w, "put", run, path)["job"])
    assert job["state"] == "failed" and private not in job["error"]


def test_lo_review_get_without_etag_restarts_instead_of_mixing_versions(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    make_bucket(w, run)
    path, _ = a_file(w, n=8)
    wait(w, lab_obs(w, "put", run, path)["job"])
    original = labobs._Obs.small

    def head(self, *args, **kwargs):
        status, hdrs, body = original(self, *args, **kwargs)
        return status, {k: v for k, v in hdrs.items() if k.lower() != "etag"}, body

    monkeypatch.setattr(labobs._Obs, "small", head)
    target = w.folder / "download"
    w.fake.down = True
    first = lab_obs(w, "get", run, target)
    assert wait(w, first["job"])["state"] == "interrupted"
    w.fake.down = False
    second = lab_obs(w, "get", run, target)
    assert second["resumed"] is False
    assert wait(w, second["job"])["state"] == "done"


def test_lo_review_vendor_allowlist_refuses_links_and_special_files(tmp_path, monkeypatch):
    path = tmp_path / "hosts"
    target = tmp_path / "outside"
    target.write_text("files.example.org\n")
    path.symlink_to(target)
    assert labobs.vendor_hosts(path) == ([], [])
    path.unlink()
    os.mkfifo(path)
    from io import open as io_open

    def checked_open(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file) == path:
            pytest.fail("attempted a blocking allowlist read")
        return io_open(file, *args, **kwargs)

    monkeypatch.setattr("io.open", checked_open)
    assert labobs.vendor_hosts(path) == ([], [])


def test_lo_review_vendor_allowlist_refuses_malformed_and_oversized_data(tmp_path):
    path = tmp_path / "hosts"
    for raw in (b"\xff", b"files.example.org\nhttps://bad.example.org\n", b"files.example.org\n" * 4000):
        path.write_bytes(raw)
        assert labobs.vendor_hosts(path) == ([], [])


def test_lo_review_alive_handles_unrepresentable_pid_and_permission_denial(monkeypatch):
    assert labobs._alive(10 ** 100) is False

    def denied(*args):
        raise PermissionError("private-" + "process")

    monkeypatch.setattr(labobs.os, "kill", denied)
    assert labobs._alive(123) is True


def test_lo_review_put_does_not_resume_another_region(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    make_bucket(w, run)
    path, _ = a_file(w, n=PART * 2)
    w.fake.cut = {2: 99}
    first = lab_obs(w, "put", run, path)
    assert wait(w, first["job"])["state"] == "interrupted"
    w.fake.cut = {}
    w.svc._registered = lambda: {LAB: ("eu-de", "eu-nl")}
    second = lab_obs(w, "put", run, path, region="eu-nl")
    assert second["ok"] and second["resumed"] is False
    wait(w, second["job"])


def test_lo_review_new_job_id_collision_preserves_previous_record(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)
    make_bucket(w, run)
    path, _ = a_file(w, n=4)
    first = lab_obs(w, "put", run, path)
    before = wait(w, first["job"])
    alternate = "obs-20261117-120000-zzzz"
    ids = iter((first["job"], alternate))
    monkeypatch.setattr(labobs, "_new_id", lambda: next(ids))
    second = lab_obs(w, "put", run, path, key="other.bin")
    wait(w, second["job"])
    assert second["job"] != first["job"] and record(w, first["job"]) == before


def test_lo_review_changed_partial_file_is_not_resumed(local_obs):
    w = local_obs
    run = start_run(w)
    make_bucket(w, run)
    path, data = a_file(w, n=8)
    wait(w, lab_obs(w, "put", run, path)["job"])
    target = w.folder / "download"
    w.fake.down = True
    first = lab_obs(w, "get", run, target)
    assert wait(w, first["job"])["state"] == "interrupted"
    target.write_bytes(b"x" * target.stat().st_size)
    os.utime(target, ns=(1, 1))
    w.fake.down = False
    second = lab_obs(w, "get", run, target)
    assert second["resumed"] is False
    assert wait(w, second["job"])["state"] == "done" and target.read_bytes() == data


def test_lo_review_log_blocks_do_not_interleave_short_writes(local_obs, monkeypatch):
    w = local_obs
    path = w.folder / "run.log"
    fds = [os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600) for _ in range(2)]
    first_write, release, second_done = threading.Event(), threading.Event(), threading.Event()
    write = os.write
    errors = []

    def short(fd, data):
        wrote = write(fd, data[:8])
        if fd == fds[0] and not first_write.is_set():
            first_write.set()
            assert release.wait(5)
        return wrote

    monkeypatch.setattr(labobs.os, "write", short)
    jobs = [_review_job(id="obs-20261117-120000-aaaa"), _review_job(id="obs-20261117-120000-bbbb")]

    def log(i):
        try:
            labobs._log_line(labobs.Live(jobs[i]["id"], -1, fds[i]), jobs[i])
        except BaseException as err:
            errors.append(err)
        finally:
            if i == 1:
                second_done.set()

    threads = [threading.Thread(target=log, args=(i,)) for i in range(2)]
    threads[0].start()
    try:
        assert first_write.wait(5)
        threads[1].start()
        second_done.wait(0.2)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
        for fd in fds:
            os.close(fd)
    assert not errors and all(not t.is_alive() for t in threads)
    text = path.read_text()
    blocks = text.strip().split("\n\n")
    assert len(blocks) == 2 and all(b.startswith("## ") and b.endswith("state: failed") for b in blocks)


def test_lo_review_download_requires_successful_eof_check(tmp_path, monkeypatch):
    import io

    original = io.BytesIO

    class CutAtEnd(original):
        def read(self, n=-1):
            if self.tell() == 4:
                raise OSError("cut")
            return super().read(n)

    monkeypatch.setattr(io, "BytesIO", CutAtEnd)
    with pytest.raises(labobs._Broken):
        _review_download(tmp_path, monkeypatch, "get", b"data")


def test_lo_review_dup_and_thread_start_failures_release_resources(local_obs, monkeypatch):
    w = local_obs
    fd = os.open(w.folder / "data", os.O_RDWR | os.O_CREAT, 0o600)
    dup = os.dup
    try:
        for stage in ("dup", "thread"):
            copied = []

            def copy(source):
                if stage == "dup" and copied:
                    raise OSError("injected")
                value = dup(source)
                copied.append(value)
                return value

            def fail(*args, **kwargs):
                raise RuntimeError("injected")

            with monkeypatch.context() as patch:
                patch.setattr(labobs.os, "dup", copy)
                patch.setattr(labobs.threading.Thread, "start", fail)
                try:
                    with pytest.raises((OSError, RuntimeError)):
                        labobs._start(w.svc, w.svc.paths_fn(), _review_job(), fd, fd)
                    assert not w.svc.obs_jobs.live
                    for value in copied:
                        with pytest.raises(OSError):
                            os.fstat(value)
                finally:
                    w.svc.obs_jobs.live.clear()
                    getattr(labobs, "_LIVE_IDS", set()).discard(_review_job()["id"])
                    for value in copied:
                        try:
                            os.close(value)
                        except OSError:
                            pass
    finally:
        os.close(fd)


def test_lo_review_run_ending_during_head_prevents_admission(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)

    def head(*args, **kwargs):
        labobs.end_jobs(w.svc, run_record(w, run))
        return 200, {"Content-Length": "4"}, b""

    monkeypatch.setattr(labobs._Obs, "small", head)
    monkeypatch.setattr(labobs, "_start", lambda *args: pytest.fail("a closing run started a download"))
    answer = lab_obs(w, "get", run, w.folder / "download")
    assert not answer["ok"] and "ending" in answer["error"]
    assert not labobs.load_jobs(w.svc.paths_fn())


def test_lo_review_write_permission_is_rechecked_after_head(local_obs, monkeypatch):
    w = local_obs
    run = start_run(w)

    def head(*args, **kwargs):
        w.svc._readonly = lambda alias: {"auth": "key", "regions": ("eu-de",)}
        return 200, {"Content-Length": "4"}, b""

    monkeypatch.setattr(labobs._Obs, "small", head)
    monkeypatch.setattr(labobs, "_start", lambda *args: pytest.fail("a read-only tenant started a download"))
    answer = lab_obs(w, "get", run, w.folder / "download")
    assert not answer["ok"] and not labobs.load_jobs(w.svc.paths_fn())
