"""awb/bucket.py against a local stand-in bucket (tests/obs_fake.py) and a throw-away Workbench."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

import awb
from awb import bucket, cli, config, obs, projects
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_obs import fake_pass

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"


@pytest.fixture
def fake():
    with FakeOBS() as f:
        yield f


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def test_a_missing_folder_is_created_under_the_month_of_the_project(home, fake, project):
    c = fake.client()
    folder, created = bucket.ensure_folder(c, project)
    assert created and folder == "%s/%s/" % (project.created[:7], project.code)
    assert {folder, folder + "in/", folder + "out/"} <= set(fake.objects)
    again, created = bucket.ensure_folder(c, project)
    assert again == folder and not created


def test_a_folder_he_made_under_another_month_is_used_where_it_is(home, fake, project):
    fake.objects["2026-01/%s/in/brief.pptx" % project.code] = b"x"
    folder, created = bucket.ensure_folder(fake.client(), project)
    assert folder == "2026-01/%s/" % project.code and not created
    assert "2026-01/%s/out/" % project.code in fake.objects


def test_two_months_for_one_project_are_refused(home, fake, project):
    for month in ("2026-01", "2026-02"):
        fake.objects["%s/%s/in/a.txt" % (month, project.code)] = b"x"
    with pytest.raises(bucket.BucketError, match="more than one month"):
        bucket.find_folder(fake.client(), project.code)


def test_sync_creates_folders_and_reports_drift_without_its_name(home, fake, project):
    fake.objects["2026-09/%s offer/in/a.txt" % fx.CUSTOMER_FORMS[1]] = b"x"   # named after the customer by hand
    text = "\n".join(bucket.sync(home, fake.client()))
    assert project.code in text and "created" in text
    assert "2026-09  1 folder(s) match no project" in text
    fx.assert_no_fixture_name(text, "the output of sync")


def test_pull_takes_new_files_into_the_intake_once(home, fake, project):
    folder, _ = bucket.ensure_folder(fake.client(), project)
    fake.objects[folder + "in/notes %s.txt" % fx.CUSTOMER_FORMS[1]] = (
        "umsetzung mit terraform fuer %s.\n" % fx.CUSTOMER_FORMS[0]).encode("utf-8")
    fake.objects[folder + "stray.txt"] = b"left alone"
    r = bucket.pull(home, fake.client(), project.code)
    assert (r["new"], r["known"], r["outside"]) == (1, 0, 1)
    res = r["intake"]
    assert not res.blocked and len(res.outputs) == 1
    fx.assert_no_fixture_name(res.outputs[0].read_text(encoding="utf-8"), "an output of the pull")
    record = home.vault / bucket.PULLED
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    again = bucket.pull(home, fake.client(), project.code)
    assert (again["new"], again["known"], again["intake"]) == (0, 1, None)


def test_pull_needs_a_folder_and_takes_a_project_without_a_customer_under_its_code(home, fake, register_path,
                                                                                   monkeypatch, project):
    """Replaces test_pull_needs_a_customer_and_a_folder (2026-10-09): a project without a customer is pulled under
    its own code with no customer code."""
    lab = projects.spawn(home, "lab", GOAL, None, register_path)
    with pytest.raises(bucket.BucketError, match="no folder"):
        bucket.pull(home, fake.client(), lab.code)
    folder, _ = bucket.ensure_folder(fake.client(), lab)
    fake.objects[folder + "in/brief.txt"] = ("Termin mit Herrn %s.\n" % fx.PLANTED_PERSON).encode("utf-8")
    res = bucket.pull(home, fake.client(), lab.code)["intake"]
    assert res.customer == lab.code and len(res.outputs) == 1
    assert "[person 1]" in res.outputs[0].read_text(encoding="utf-8")
    with pytest.raises(bucket.BucketError, match="no folder"):
        bucket.pull(home, fake.client(), project.code)
    with pytest.raises(bucket.BucketError, match="not a registered project"):
        bucket.pull(home, fake.client(), "tcp-zzzz")


def test_put_writes_to_out_and_keeps_an_existing_file(home, fake, project, tmp_path):
    f = tmp_path / "offer.md"
    f.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    key, stats = bucket.put(home, fake.client(), project.code, f)
    assert key.endswith("%s/out/offer.md" % project.code) and stats is None
    assert fake.objects[key] == f.read_bytes()
    with pytest.raises(bucket.BucketError, match="--replace"):
        bucket.put(home, fake.client(), project.code, f)
    bucket.put(home, fake.client(), project.code, f, replace=True)


def test_put_with_reveal_uploads_the_named_text_only(home, fake, project, tmp_path):
    f = tmp_path / "offer.md"
    f.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    key, stats = bucket.put(home, fake.client(), project.code, f, reveal=True)
    assert fake.objects[key].decode("utf-8") == "offer for %s\n" % fx.CUSTOMER_FORMS[0]
    assert f.read_text(encoding="utf-8") == "offer for %s\n" % fx.CUSTOMER_CODE
    assert stats["replaced"] == 1
    assert not any((home.vault / "tmp").iterdir()), "the named temporary file is gone"


def test_move_renames_a_drifted_folder_by_copy_check_and_delete(home, fake, project):
    old = "2026-09/offer draft/"
    for name in ("in/a.pptx", "in/b.pdf", "out/c.md"):
        fake.objects[old + name] = name.encode()
    r = bucket.move(home, fake.client(), old, project.code)
    assert r == {"moved": 3, "target": "2026-09/%s/" % project.code}
    assert not any(k.startswith(old) for k in fake.objects)
    assert fake.objects["2026-09/%s/in/a.pptx" % project.code] == b"in/a.pptx"


def test_move_deletes_nothing_when_the_copy_does_not_match(home, fake, project, monkeypatch):
    old = "2026-09/offer draft/"
    fake.objects[old + "in/a.pptx"] = b"12345"
    monkeypatch.setattr(obs.Client, "copy", lambda self, source, target: self.put_bytes(target, b"1"))
    with pytest.raises(bucket.BucketError, match="nothing was deleted"):
        bucket.move(home, fake.client(), old, project.code)
    assert fake.objects[old + "in/a.pptx"] == b"12345"


def test_the_work_user_cannot_use_the_bucket(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(bucket.BucketError, match="owner's"):
        bucket.settings()


def test_the_command_line_end_to_end(home, fake, project, tmp_path, monkeypatch, capsys):
    """Since Z3 step 11 the command line reaches the bucket through the key service alone (tests/test_bucket_keys.py
    runs it end to end against a real key service); without one it stops with the unlock line and reads no key
    setting (his word P31)."""
    monkeypatch.setenv("AWB_KEYS_ADMIN", "/nonexistent/awb-keys-admin.sock")  # short: a tmp_path socket is too long
    monkeypatch.setenv("AWB_BUCKET_KEYS", "pass:cloud/owner")
    assert cli.main(["bucket", "folder", project.code]) == 2
    assert bucket.LOCKED_LINE in capsys.readouterr().err
    assert cli.main(["bucket", "sync"]) == 2
    assert bucket.LOCKED_LINE in capsys.readouterr().err


def test_pull_prints_the_wiped_counts_and_never_blocked(home, fake, project, monkeypatch, capsys):
    """T4: the pull runs the intake in wipe mode; its line names the counts per class, never a stop or --force."""
    folder, _ = bucket.ensure_folder(fake.client(), project)
    fake.objects[folder + "in/memo.txt"] = ("Termin mit Herrn %s bei der %s.\n"
                                            % (fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE)).encode("utf-8")
    monkeypatch.setattr(bucket, "client", lambda: fake.client())
    assert bucket.main(["pull", project.code]) == 0
    out = capsys.readouterr().out
    assert "1 outputs in the outbox, wiped person 1, company 1" in out
    assert "blocked" not in out and "--force" not in out
    fx.assert_no_fixture_name(out, "the output of the pull")
    assert fx.PLANTED_CANDIDATE not in out


# Hardening regressions use no sockets, installed package or repository outside tmp_path.
class MemoryBucket:
    can_copy = True

    def __init__(self):
        self.objects = {}
        self.calls = []

    def list(self, prefix="", delimiter="", page=0):
        import hashlib
        objects, prefixes = [], set()
        for key, data in sorted(self.objects.items()):
            if not key.startswith(prefix):
                continue
            tail = key[len(prefix):]
            if delimiter and delimiter in tail:
                prefixes.add(prefix + tail.split(delimiter)[0] + delimiter)
            else:
                objects.append((key, len(data), hashlib.md5(data).hexdigest()))
        return obs.Listing(objects, sorted(prefixes), {})

    def head(self, key):
        import hashlib
        if key not in self.objects:
            return None
        data = self.objects[key]
        return {"Content-Length": str(len(data)), "ETag": hashlib.md5(data).hexdigest()}

    def get(self, key, to, if_match=None):
        self.calls.append(("get", key))
        Path(to).write_bytes(self.objects[key])
        return Path(to)

    def put_bytes(self, key, data, content_type=None):
        self.calls.append(("put", key))
        self.objects[key] = data

    def put_file(self, key, file, content_type=None):
        self.put_bytes(key, Path(file).read_bytes(), content_type)

    def copy(self, source, target):
        self.calls.append(("copy", source))
        self.objects[target] = self.objects[source]

    def delete(self, key):
        self.calls.append(("delete", key))
        del self.objects[key]


@pytest.fixture
def bh(home, monkeypatch):
    from types import SimpleNamespace
    root = home.projects_root / "tcp-q7m4"
    root.mkdir()
    project = SimpleNamespace(code=root.name, path=str(root), created="2026-11-16", customer=fx.CUSTOMER_CODE,
                              state="active")
    monkeypatch.setattr(projects, "load", lambda p: [project])
    client = MemoryBucket()
    folder, _ = bucket.ensure_folder(client, project)
    return project, client, folder


def wire_reply(monkeypatch, answer, body=b"", on_connect=None):
    import json

    class Wire:
        def __init__(self):
            self.data = bytearray(answer if isinstance(answer, bytes) else json.dumps(answer).encode() + b"\n")
            self.data.extend(body)
            self.sent = bytearray()
            self.closed = False

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            if on_connect:
                on_connect()

        def sendall(self, data):
            self.sent.extend(data)

        def recv(self, n):
            chunk = bytes(self.data[:n])
            del self.data[:n]
            return chunk

        def close(self):
            self.closed = True

    wire = Wire()
    monkeypatch.setattr(bucket.socket, "socket", lambda *a: wire)
    return wire


def private_value():
    return "pass" + "word=" + "invented-" + "value-2346"


def test_bh_reply_requires_complete_object_and_boolean_success(monkeypatch):
    for reply in (b'{"ok": true}', b'{"ok": true, "ok": false}\n', b'{"ok": 1}\n',
                  b'{"ok": true, "value": NaN}\n', b'[]\n', b'\xff\n'):
        wire_reply(monkeypatch, reply)
        with pytest.raises(bucket.BucketError):
            bucket.KeyServiceClient()._ask({"op": "bucket_list"})


def test_bh_reply_limit_includes_the_chunk_with_newline(monkeypatch):
    monkeypatch.setattr(bucket, "MAX_ANSWER", 32)
    wire_reply(monkeypatch, {"ok": True, "padding": "x" * 64})
    with pytest.raises(bucket.BucketError, match="too large"):
        bucket.KeyServiceClient()._ask({"op": "bucket_list"})


def test_bh_service_errors_never_echo_values(monkeypatch):
    for reply in ({"ok": False, "error": private_value()},
                  {"ok": False, "kind": "http", "status": 403, "error": private_value()},
                  {"ok": False, "kind": "http", "status": private_value(), "error": private_value()}):
        wire_reply(monkeypatch, reply)
        with pytest.raises((bucket.BucketError, obs.OBSError)) as caught:
            bucket.KeyServiceClient()._ask({"op": "bucket_list"})
        assert private_value() not in str(caught.value)


def test_bh_failed_and_invalid_download_replies_leave_destination_untouched(tmp_path, monkeypatch):
    dst = tmp_path / "download"
    for reply in ({"ok": False, "length": 1}, {"ok": True}, {"ok": True, "length": -1},
                  {"ok": True, "length": True}, {"ok": True, "length": "1"}):
        dst.write_bytes(b"original")
        wire_reply(monkeypatch, reply, b"x")
        with pytest.raises(bucket.BucketError):
            bucket.KeyServiceClient().get("out/file", dst)
        assert dst.read_bytes() == b"original"


def test_bh_short_download_preserves_existing_file_and_removes_stage(tmp_path, monkeypatch):
    dst = tmp_path / "download"
    dst.write_bytes(b"original")
    wire_reply(monkeypatch, {"ok": True, "length": 8}, b"short")
    with pytest.raises(bucket.BucketError, match="ended early"):
        bucket.KeyServiceClient().get("out/file", dst)
    assert dst.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [dst]


def test_bh_download_refuses_linked_files_and_parents(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file"
    target.write_bytes(b"private")
    link = tmp_path / "link"
    link.symlink_to(target)
    parent = tmp_path / "parent"
    parent.symlink_to(outside, target_is_directory=True)
    for dst in (link, parent / "file"):
        wire_reply(monkeypatch, {"ok": True, "length": 3}, b"new")
        with pytest.raises(bucket.BucketError):
            bucket.KeyServiceClient().get("out/file", dst)
        assert target.read_bytes() == b"private"


def test_bh_download_checks_requested_etag_before_publication(tmp_path, monkeypatch):
    dst = tmp_path / "download"
    wire_reply(monkeypatch, {"ok": True, "length": 1, "etag": "new"}, b"x")
    with pytest.raises(bucket.BucketError):
        bucket.KeyServiceClient().get("out/file", dst, if_match="old")
    assert not dst.exists()


def test_bh_listing_schema_and_scope_are_checked(monkeypatch):
    good = {"ok": True, "objects": [], "prefixes": [], "modified": {}}
    bad = ({"objects": [["scope/file", -1, "etag"]]}, {"objects": [["elsewhere/file", 1, "etag"]]},
           {"objects": [["scope/file", True, "etag"]]}, {"objects": [["scope/file", 1]]},
           {"objects": [["scope/file", 1, "tag\nforged"]]}, {"prefixes": "scope/"},
           {"modified": []}, {"prefixes": ["elsewhere/"]})
    for change in bad:
        for method in ("list", "lab_list"):
            wire_reply(monkeypatch, dict(good, **change))
            with pytest.raises(bucket.BucketError):
                getattr(bucket.KeyServiceClient(), method)("scope/")


def test_bh_upload_refuses_missing_linked_and_special_sources(tmp_path, monkeypatch):
    import os
    source = tmp_path / "source"
    source.write_bytes(b"private")
    link = tmp_path / "link"
    link.symlink_to(source)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for file in (tmp_path / "missing", link, fifo, tmp_path):
        wire = wire_reply(monkeypatch, {"ok": True})
        with pytest.raises(bucket.BucketError):
            bucket.KeyServiceClient().put_file("out/file", file)
        assert not wire.sent


def test_bh_upload_uses_the_opened_file_even_if_path_changes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"changed!")
    wire = wire_reply(monkeypatch, {"ok": True}, on_connect=lambda: replacement.replace(source))
    bucket.KeyServiceClient().put_file("out/file", source)
    assert wire.sent.partition(b"\n")[2] == b"original"


def test_bh_unknown_project_does_not_echo_input(home):
    with pytest.raises(bucket.BucketError) as caught:
        bucket._project(home, private_value())
    assert private_value() not in str(caught.value)


def test_bh_registered_project_cannot_escape_projects_root(home, bh, tmp_path):
    project, client, _ = bh
    project.path = str(tmp_path)
    source = tmp_path / "offer.txt"
    source.write_text("offer")
    with pytest.raises(bucket.BucketError):
        bucket.put(home, client, project.code, source)


def test_bh_original_operations_enforce_session_guard_with_any_client(home, bh, monkeypatch, tmp_path):
    from awb import vault
    project, client, folder = bh
    client.objects[folder + "in/file.txt"] = b"private"
    source = tmp_path / "offer.txt"
    source.write_text("offer")
    monkeypatch.setattr(vault, "in_assistant_session", lambda: True)
    for action in (lambda: bucket.fetch(home, client, project.code),
                   lambda: bucket.move(home, client, "2026-01/drift/", project.code),
                   lambda: bucket.put(home, client, project.code, source, reveal=True)):
        with pytest.raises(bucket.BucketError, match="assistant session"):
            action()
    assert not list(home.inbox.iterdir())


def test_bh_history_refuses_links_and_special_files(home, tmp_path):
    import os
    record = home.vault / bucket.PULLED
    target = tmp_path / "outside"
    target.write_bytes(b"unchanged")
    for kind in ("symlink", "hardlink", "fifo"):
        if kind == "symlink":
            record.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, record)
        else:
            os.mkfifo(record)
        for action in (lambda: bucket.pulled(home), lambda: bucket._remember(home, "key", "tag")):
            with pytest.raises(bucket.BucketError):
                action()
        record.unlink()
        assert target.read_bytes() == b"unchanged"


def test_bh_history_rejects_corruption_and_unreadable_storage(home, monkeypatch):
    record = home.vault / bucket.PULLED
    for data in (b"\xff", b"partial", b"key\ttag\tnot-a-date\n"):
        record.write_bytes(data)
        with pytest.raises(bucket.BucketError):
            bucket.pulled(home)
    original_open = bucket.os.open
    def denied(path, *args, **kwargs):
        if str(path).endswith(bucket.PULLED):
            raise PermissionError(private_value())
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(bucket.os, "open", denied)
    with pytest.raises(bucket.BucketError) as caught:
        bucket.pulled(home)
    assert private_value() not in str(caught.value)


def test_bh_history_cannot_be_forged_by_etag_or_key(home):
    for key, etag in (("key", "tag\nforged\tentry"), ("key\rforged", "tag")):
        with pytest.raises(bucket.BucketError):
            bucket._remember(home, key, etag)
    assert bucket.pulled(home) == set()


def test_bh_fetch_refuses_linked_inbox(home, bh, tmp_path):
    project, client, folder = bh
    client.objects[folder + "in/file.txt"] = b"private"
    outside = tmp_path / "outside"
    outside.mkdir()
    home.inbox.rmdir()
    home.inbox.symlink_to(outside, target_is_directory=True)
    with pytest.raises(bucket.BucketError):
        bucket.fetch(home, client, project.code)
    assert not list(outside.iterdir())


def test_bh_fetch_does_not_follow_dangling_name_collision(home, bh, tmp_path):
    project, client, folder = bh
    client.objects[folder + "in/file.txt"] = b"private"
    outside = tmp_path / "outside"
    (home.inbox / "file.txt").symlink_to(outside)
    result = bucket.fetch(home, client, project.code)
    assert not outside.exists()
    assert result["files"][0].read_bytes() == b"private"
    assert stat.S_IMODE(result["files"][0].stat().st_mode) == 0o600


def test_bh_failed_fetch_leaves_no_partial_file_or_history(home, bh, monkeypatch):
    project, client, folder = bh
    client.objects[folder + "in/file.txt"] = b"private"
    def broken(key, dst, **kwargs):
        Path(dst).write_bytes(b"partial")
        raise OSError(private_value())
    monkeypatch.setattr(client, "get", broken)
    with pytest.raises(bucket.BucketError) as caught:
        bucket.fetch(home, client, project.code)
    assert private_value() not in str(caught.value)
    assert not list(home.inbox.iterdir()) and bucket.pulled(home) == set()


def test_bh_fetch_verifies_bytes_before_remembering_listing_etag(home, bh, monkeypatch):
    project, client, folder = bh
    client.objects[folder + "in/file.txt"] = b"original"
    monkeypatch.setattr(client, "get", lambda key, dst, **kw: Path(dst).write_bytes(b"modified"))
    with pytest.raises(bucket.BucketError):
        bucket.fetch(home, client, project.code)
    assert bucket.pulled(home) == set() and not list(home.inbox.iterdir())


def test_bh_concurrent_fetches_download_and_remember_once(home, bh, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    project, client, folder = bh
    client.objects[folder + "in/file.txt"] = b"private"
    start = threading.Barrier(2)
    get = client.get
    def slow_get(*args, **kw):
        time.sleep(0.05)
        return get(*args, **kw)
    monkeypatch.setattr(client, "get", slow_get)
    def run():
        start.wait(timeout=5)
        return bucket.fetch(home, client, project.code)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [f.result(timeout=10) for f in futures]
    assert sorted(r["new"] for r in results) == [0, 1]
    assert len([c for c in client.calls if c[0] == "get"]) == 1
    assert len((home.vault / bucket.PULLED).read_text().splitlines()) == 1


def test_bh_put_refuses_links_and_special_sources(home, bh, tmp_path):
    import os
    project, client, _ = bh
    source = tmp_path / "source.txt"
    source.write_text("private")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    for path in (link, fifo, tmp_path / "missing", tmp_path):
        with pytest.raises(bucket.BucketError):
            bucket.put(home, client, project.code, path)


def test_bh_put_reviews_and_scrubs_one_snapshot(home, bh, tmp_path, monkeypatch):
    from awb import putcopy
    project, client, _ = bh
    source = tmp_path / "source.txt"
    source.write_text("checked bytes")
    def review(root, file):
        assert Path(file).read_text() == "checked bytes"
        source.write_text("changed bytes")
        return "review: none"
    monkeypatch.setattr(putcopy, "review_line", review)
    key, _ = bucket.put(home, client, project.code, source)
    assert client.objects[key] == b"checked bytes"


def test_bh_secret_filename_never_reaches_key_or_html_title(home, bh, tmp_path):
    project, client, _ = bh
    source = tmp_path / (private_value() + ".md")
    source.write_text("ordinary text")
    key, _ = bucket.put(home, client, project.code, source)
    assert private_value() not in key
    assert private_value().encode() not in b"".join(client.objects.values())


def test_bh_cli_hides_registered_filename_and_untrusted_review(home, bh, tmp_path, monkeypatch, capsys):
    from awb import putcopy
    project, client, _ = bh
    source = tmp_path / (fx.CUSTOMER_FORMS[0] + ".txt")
    source.write_text("offer")
    monkeypatch.setattr(bucket, "client", lambda: client)
    monkeypatch.setattr(putcopy, "review_line", lambda *a: "review: passed " + private_value())
    assert bucket.main(["put", project.code, str(source)]) == 0
    captured = capsys.readouterr()
    assert private_value() not in captured.out + captured.err
    fx.assert_no_fixture_name(captured.out + captured.err, "bucket put output")


def test_bh_bad_markdown_is_rejected_before_upload_or_archive(home, bh, tmp_path):
    project, client, folder = bh
    client.objects[folder + "out/offer v1.md"] = b"older"
    source = tmp_path / "offer v2.md"
    source.write_bytes(b"\xff")
    before = dict(client.objects)
    with pytest.raises(bucket.BucketError, match="UTF-8"):
        bucket.put(home, client, project.code, source)
    assert client.objects == before


def test_bh_reveal_cannot_reintroduce_a_secret(home, bh, tmp_path, monkeypatch):
    from awb import reveal
    project, client, _ = bh
    source = tmp_path / "offer.txt"
    source.write_text("coded text")
    def named(text, target, p):
        target.write_text(private_value())
        return {"replaced": 1}
    monkeypatch.setattr(reveal, "write_named", named)
    key, _ = bucket.put(home, client, project.code, source, reveal=True)
    assert private_value().encode() not in client.objects[key]
    assert b"[secret]" in client.objects[key]


def test_bh_concurrent_put_without_replace_does_not_overwrite(home, bh, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from awb import putcopy
    project, client, folder = bh
    source = tmp_path / "offer.txt"
    source.write_text("offer")
    start = threading.Barrier(2)
    scrub = putcopy.scrub
    def slow_scrub(*args):
        time.sleep(0.05)
        return scrub(*args)
    monkeypatch.setattr(putcopy, "scrub", slow_scrub)
    def run():
        start.wait(timeout=5)
        try:
            bucket.put(home, client, project.code, source)
            return "put"
        except bucket.BucketError:
            return "refused"
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == ["put", "refused"]
    assert client.calls.count(("put", folder + "out/offer.txt")) == 1


def test_bh_move_compares_etags_and_rechecks_source_before_delete(home, bh, monkeypatch):
    project, client, _ = bh
    client.objects.clear()
    old = "2026-01/drift/"
    key = old + "in/file.txt"
    for corrupt_source in (False, True):
        client.objects = {key: b"original"}
        def corrupt(src, dst):
            client.objects[dst] = client.objects[src]
            client.objects[src if corrupt_source else dst] = b"modified"
        monkeypatch.setattr(client, "copy", corrupt)
        with pytest.raises(bucket.BucketError, match="nothing was deleted"):
            bucket.move(home, client, old, project.code)
        assert key in client.objects


def test_bh_move_rejects_unsafe_folder_and_out_of_scope_listing(home, bh, monkeypatch):
    project, client, _ = bh
    client.objects.clear()
    for old in ("2026-01/../", "2026-01/./", "2026-01//", "2026-01/name\n/"):
        client.objects = {old + "in/file": b"x"}
        with pytest.raises(bucket.BucketError):
            bucket.move(home, client, old, project.code)
        assert not any(c[0] == "copy" for c in client.calls)
    client.objects.clear()
    client.objects["elsewhere/in/file"] = b"x"
    original_list = client.list
    def foreign(prefix="", delimiter="", page=0):
        if prefix == "2026-01/drift/":
            return obs.Listing([("elsewhere/in/file", 1, "etag")], [], {})
        return original_list(prefix, delimiter, page)
    monkeypatch.setattr(client, "list", foreign)
    with pytest.raises(bucket.BucketError):
        bucket.move(home, client, "2026-01/drift/", project.code)
    assert not any(c[0] == "copy" for c in client.calls)


def test_bh_archive_error_and_reason_are_safe(monkeypatch):
    client = MemoryBucket()
    def failed_head(*args):
        raise obs.OBSError(private_value())
    monkeypatch.setattr(client, "head", failed_head)
    assert bucket.archive_out(client, "old", "new") == ("failed", "the archive failed")
    client.can_copy = False
    def fail(*args):
        raise bucket.BucketError(private_value())
    monkeypatch.setattr(client, "archive", fail, raising=False)
    assert private_value() not in bucket.archive_out(client, "old", "new")[1]
    monkeypatch.setattr(client, "archive", lambda *a: {"reason": private_value()})
    assert private_value() not in bucket.archive_out(client, "old", "new")[1]


def test_bh_cli_dependency_errors_are_private(home, monkeypatch, capsys):
    from awb import intake, register, reveal
    for error in (OSError, ValueError, bucket.BucketError, obs.OBSError, projects.ProjectError, intake.IntakeError,
                  register.RegisterError, reveal.RevealError):
        def fail():
            raise error(private_value())
        monkeypatch.setattr(bucket, "client", fail)
        assert bucket.main(["sync"]) == 2
        captured = capsys.readouterr()
        assert private_value() not in captured.out + captured.err


def test_bh_download_verifies_bytes_even_when_service_repeats_requested_etag(tmp_path, monkeypatch):
    import hashlib
    dst = tmp_path / "download"
    digest = hashlib.md5(b"original").hexdigest()
    wire_reply(monkeypatch, {"ok": True, "length": 8, "etag": digest}, b"modified")
    with pytest.raises(bucket.BucketError):
        bucket.KeyServiceClient().get("out/file", dst, if_match=digest)
    assert not dst.exists()


def test_bh_successful_download_is_complete_and_private(tmp_path, monkeypatch):
    import os
    dst = tmp_path / "download"
    data = b"x" * (1 << 17)
    wire = wire_reply(monkeypatch, {"ok": True, "length": len(data)}, data)
    before = os.umask(0o022)
    try:
        assert bucket.KeyServiceClient().get("out/file", dst) == dst
    finally:
        os.umask(before)
    assert dst.read_bytes() == data
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [dst] and wire.closed


def test_bh_history_failed_append_rolls_back_partial_row(home, monkeypatch):
    bucket._remember(home, "key", "tag")
    record = home.vault / bucket.PULLED
    before = record.read_bytes()
    def broken(fd):
        raise OSError(private_value())
    monkeypatch.setattr(bucket.os, "fsync", broken)
    with pytest.raises(bucket.BucketError) as caught:
        bucket._remember(home, "next", "tag")
    assert private_value() not in str(caught.value)
    assert record.read_bytes() == before


def test_bh_put_read_failure_is_private_and_changes_no_remote_file(home, bh, tmp_path, monkeypatch):
    project, client, _ = bh
    source = tmp_path / "offer.txt"
    source.write_text("offer")
    original_open = bucket.os.open
    original_read = Path.read_bytes
    def denied_open(path, *args, **kwargs):
        if str(path) in (str(source), source.name):
            raise PermissionError(private_value())
        return original_open(path, *args, **kwargs)
    def denied_read(path):
        if path == source:
            raise PermissionError(private_value())
        return original_read(path)
    monkeypatch.setattr(bucket.os, "open", denied_open)
    monkeypatch.setattr(Path, "read_bytes", denied_read)
    before = dict(client.objects)
    with pytest.raises(bucket.BucketError) as caught:
        bucket.put(home, client, project.code, source)
    assert private_value() not in str(caught.value)
    assert client.objects == before


def test_bh_version_listing_cannot_archive_another_project(home, bh, tmp_path, monkeypatch):
    project, client, folder = bh
    source = tmp_path / "offer v2.csv"
    source.write_text("new")
    foreign = "2026-11/tcp-zzzz/out/offer v1.csv"
    client.objects[foreign] = b"old"
    original_list = client.list
    def forged(prefix="", delimiter="", page=0):
        if prefix == folder + bucket.OUT:
            return obs.Listing([(foreign, 3, "etag")], [], {})
        return original_list(prefix, delimiter, page)
    monkeypatch.setattr(client, "list", forged)
    with pytest.raises(bucket.BucketError):
        bucket.put(home, client, project.code, source)
    assert client.objects[foreign] == b"old"
    assert not any(call[0] in ("copy", "delete") for call in client.calls)


def test_bh_history_read_is_bounded(home, monkeypatch):
    monkeypatch.setattr(bucket, "MAX_ANSWER", 64)
    (home.vault / bucket.PULLED).write_text("key\ttag\t2026-11-16\n" * 20)
    with pytest.raises(bucket.BucketError):
        bucket.pulled(home)


def test_bh_cli_parser_never_echoes_quoted_argument(capsys):
    value = private_value() + "'suffix"
    assert bucket.main([value]) == 2
    captured = capsys.readouterr()
    assert private_value() not in captured.out + captured.err


def test_bh_malformed_text_cannot_bypass_secret_scrubbing(home, bh, tmp_path):
    project, client, _ = bh
    for index, data in enumerate((b"\xff", b"\x00")):
        source = tmp_path / ("offer%d.txt" % index)
        source.write_bytes(data + private_value().encode())
        before = dict(client.objects)
        with pytest.raises(bucket.BucketError):
            bucket.put(home, client, project.code, source)
        assert client.objects == before
