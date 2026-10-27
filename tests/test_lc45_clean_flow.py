"""LC4 and LC5: the laptop flow of `awb clean` with the project named once, the default kind, Windows paths, the put
of the last run and the local page of `awb clean serve`. Invented names only (tests/fixtures.py); the page is driven
in-process on 127.0.0.1 with http.client, no browser."""
from __future__ import annotations

import http.client
import io
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from awb import clean, cleanserve, cli, laptop, obs
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

PROJECT = "tcp-q7m4"
CUSTOMER = fx.CUSTOMER_CODE
OTHER = "tcp-r2n5"
AK, SK = "AKLABFAKE", "sk-lab-fake-secret"


@pytest.fixture
def lap(tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    monkeypatch.setenv(laptop.WORK_ENV, str(tmp_path / "work"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    laptop.init()
    return tmp_path


def _register(code=CUSTOMER):
    it = iter([f + "\n" for f in fx.CUSTOMER_FORMS] + ["\n"])
    laptop.ask_forms(code, read=lambda: next(it, ""), tty=True, say=lambda s: None)


def _log(path: Path) -> Path:
    path.write_text("user=%s host=%s from %s\nERROR kafka broker not available\n"
                    % (fx.LOG_USER, fx.LOG_HOST, fx.LOG_PUBLIC_IP), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- LC4


def test_use_remembers_the_project_makes_its_folder_and_a_run_takes_it(lap, capsys):
    _register()
    assert cli.main(["clean", "use", PROJECT]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "project: %s (from awb clean use), customer %s" % (PROJECT, CUSTOMER)
    folder = lap / "work" / PROJECT
    assert folder.is_dir() and laptop.current() == PROJECT and "registered on this laptop" in out
    src = _log(lap / "app.log")
    assert cli.main(["clean", str(src)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "project: %s (from awb clean use), customer %s" % (PROJECT, CUSTOMER)
    assert (folder / "clean" / "app.clean.log").is_file()
    assert lines[-1].startswith("next: awb clean put sends the 1 clean copy")
    assert cli.main(["clean", "use"]) == 0
    assert PROJECT in capsys.readouterr().out


def test_the_work_folder_of_the_current_directory_is_the_project_and_project_wins(lap, monkeypatch):
    laptop.use(OTHER)
    below = lap / "work" / PROJECT / "input" / "day1"
    below.mkdir(parents=True)
    monkeypatch.chdir(below)
    assert laptop.resolve_project(None) == (PROJECT, "the folder")
    assert laptop.resolve_project(OTHER) == (OTHER, "--project")
    monkeypatch.chdir(lap)
    assert laptop.resolve_project(None) == (OTHER, "awb clean use")
    (lap / "laptop" / laptop.CURRENT).unlink()
    with pytest.raises(laptop.LaptopError, match="awb clean use tcp-xxxx"):
        laptop.resolve_project(None)


def test_project_still_writes_into_the_current_folder_as_before(lap, capsys):
    _register()
    src = _log(lap / "app.log")
    assert cli.main(["clean", str(src), "--project", PROJECT]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("project: %s (from --project)" % PROJECT)
    assert (lap / "clean" / "app.clean.log").is_file()


def test_an_unregistered_customer_is_asked_once_on_a_terminal_and_refused_without_one(lap, monkeypatch, capsys):
    src = _log(lap / "app.log")
    assert cli.main(["clean", str(src), "--project", PROJECT]) == 2
    assert "awb clean register %s" % CUSTOMER in capsys.readouterr().err
    monkeypatch.setattr(clean, "_interactive", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))            # an empty first line: no forms
    assert cli.main(["clean", str(src), "--project", PROJECT]) == 0
    out = capsys.readouterr().out
    assert "%s is not registered on this laptop yet." % CUSTOMER in out and "registered without a form" in out
    assert laptop.known(CUSTOMER)


def test_a_windows_path_is_read_as_its_wsl_path():
    assert clean.windows_path("D:\\Documents\\x.docx") == "/mnt/d/Documents/x.docx"
    assert clean.windows_path('"E:\\data\\a b\\req1.docx"') == "/mnt/e/data/a b/req1.docx"
    assert clean.windows_path("D:/Documents/x.docx") == "/mnt/d/Documents/x.docx"
    assert clean.windows_path("/mnt/d/x.docx") == "/mnt/d/x.docx"
    assert clean.windows_path("notes/x.docx") == "notes/x.docx"


def test_the_kind_is_logs_for_a_log_file_and_docs_for_any_other(lap, capsys):
    _register()
    laptop.use(PROJECT)
    assert clean.default_kind(Path("a.log")) == "logs" and clean.default_kind(Path("a.LOG")) == "logs"
    assert clean.default_kind(Path("syslog.txt")) == "docs" and clean.default_kind(Path("a.docx")) == "docs"
    log = _log(lap / "app.log")
    txt = lap / "notes.txt"
    txt.write_text("the kafka cluster runs on ECS\n", encoding="utf-8")
    assert cli.main(["clean", str(log), str(txt)]) == 0
    out = capsys.readouterr().out
    assert "kind: docs (default for a file not ending in .log)" in out and "kind: logs (a .log file)" in out
    clean_dir = lap / "work" / PROJECT / "clean"
    assert (clean_dir / "notes.clean.md").is_file() and (clean_dir / "app.clean.log").is_file()
    assert len(laptop.last_run(PROJECT)) == 2


def test_put_sends_the_copies_of_the_last_run_without_cleaning_again(lap, capsys, monkeypatch):
    _register()
    laptop.use(PROJECT)
    assert cli.main(["clean", "put"]) == 2
    assert "no run on this laptop yet" in capsys.readouterr().err
    src = _log(lap / "app.log")
    assert cli.main(["clean", str(src)]) == 0
    capsys.readouterr()
    assert cli.main(["clean", "put"]) == 2                          # no key in the environment
    assert "AWB_OBS_AK and AWB_OBS_SK" in capsys.readouterr().err
    monkeypatch.setattr(clean, "run", lambda *a, **k: pytest.fail("put cleans nothing again"))
    with FakeOBS("awb", AK, SK) as own:
        own.objects["2026-09/%s/" % PROJECT] = b""
        own.objects["2026-09/%s/in/" % PROJECT] = b""
        said = []
        keys = clean.put_last(PROJECT, client=obs.Client(own.bucket, obs.Keys(AK, SK), endpoint=own.endpoint),
                              say=said.append)
        assert keys == ["2026-09/%s/in/app.clean.log" % PROJECT]
        assert own.objects[keys[0]] == (lap / "work" / PROJECT / "clean" / "app.clean.log").read_bytes()
        assert said[0] == "put: 1 clean copy of the last run"


# --------------------------------------------------------------------------- LC5


class Page:
    def __init__(self, client=None):
        self.state = cleanserve.State()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cleanserve.make_handler(self.state, client=client))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def call(self, method, path, body=b"", headers=None, host="localhost"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp.status, data

    def get(self, path="/", **kw):
        return self.call("GET", path, **kw)

    def t(self, path="/"):
        return "%s%st=%s" % (path, "&" if "?" in path else "?", self.state.token)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def page(lap):
    p = Page()
    yield p
    p.close()


def _multipart(files, fields):
    boundary = "awbtestboundary42"
    parts = []
    for name, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode("utf-8"))
    for filename, data in files:
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode("utf-8")
                     + data + b"\r\n")
    body = b"".join(parts) + ("--%s--\r\n" % boundary).encode("utf-8")
    return body, {"Content-Type": "multipart/form-data; boundary=%s" % boundary}


def test_the_page_needs_its_token(page):
    assert page.get("/")[0] == 403
    assert page.get("/?t=wrong")[0] == 403
    status, text = page.get(page.t())
    assert status == 200 and "<title>awb clean</title>" in text
    assert page.call("POST", "/put")[0] == 403


def test_the_page_refuses_another_host_header(page):
    assert page.get(page.t(), host="evil.example")[0] == 403
    assert page.get(page.t(), host="localhost.evil.example:8765")[0] == 403
    assert page.get(page.t(), host=None)[0] == 403
    assert page.get(page.t(), host="127.0.0.1:8765")[0] == 200
    assert cleanserve.host_ok("localhost:8765") and not cleanserve.host_ok("127.0.0.1:x")


def test_use_on_the_page_sets_the_project_and_the_field_is_prefilled(page, lap):
    status, _ = page.call("POST", page.t("/use"), body=("project=%s" % PROJECT).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303 and laptop.current() == PROJECT and (lap / "work" / PROJECT).is_dir()
    assert 'value="%s"' % PROJECT in page.get(page.t())[1]


def test_upload_and_run_write_the_clean_copy_and_the_report_and_delete_the_original(page, lap, monkeypatch):
    _register()
    laptop.use(PROJECT)
    original = _log(lap / "src.log").read_bytes()
    seen = []
    real = clean.run_files

    def spy(files, *a, **k):
        seen.extend(files)
        return real(files, *a, **k)

    monkeypatch.setattr(clean, "run_files", spy)
    body, headers = _multipart([("app.log", original)], {"kind": "auto", "paths": ""})
    assert page.call("POST", page.t("/run"), body=body, headers=headers)[0] == 303
    copy = lap / "work" / PROJECT / "clean" / "app.clean.log"
    assert copy.is_file() and fx.LOG_USER not in copy.read_text(encoding="utf-8")
    assert seen and not any(p.exists() for p in seen)                   # the uploaded original is gone
    assert not [p for p in (lap / "laptop" / "tmp").iterdir() if p.name.startswith("upload-")]
    text = page.get(page.t())[1]
    assert "| app.clean.log | logs | clean |" in text and fx.LOG_USER not in text
    status, shown = page.get(page.t("/view?f=app.clean.log"))
    assert status == 200 and shown == copy.read_text(encoding="utf-8")
    assert page.get(page.t("/view?f=..%2Fapp.clean.log"))[0] == 404
    assert page.get(page.t("/view?f=other.clean.md"))[0] == 404


def test_a_typed_windows_path_is_run_and_the_original_stays(page, lap, monkeypatch):
    _register()
    laptop.use(PROJECT)
    src = _log(lap / "typed.log")
    monkeypatch.setattr(clean, "windows_path", lambda s: str(src) if s.strip() == "D:\\in\\typed.log" else s)
    body, headers = _multipart([], {"kind": "auto", "paths": "D:\\in\\typed.log\n"})
    assert page.call("POST", page.t("/run"), body=body, headers=headers)[0] == 303
    assert (lap / "work" / PROJECT / "clean" / "typed.clean.log").is_file() and src.is_file()


def test_put_on_the_page_without_the_key_says_so_and_how_to_load_it(page, lap):
    _register()
    laptop.use(PROJECT)
    text = page.get(page.t())[1]
    assert "AWB_OBS_AK and AWB_OBS_SK" in text and "obs.env" in text
    assert page.call("POST", page.t("/put"))[0] == 303
    assert "put needs AWB_OBS_AK and AWB_OBS_SK" in page.state.message


def test_put_on_the_page_sends_the_copies_of_its_run(lap):
    _register()
    laptop.use(PROJECT)
    with FakeOBS("awb", AK, SK) as own:
        own.objects["2026-09/%s/" % PROJECT] = b""
        p = Page(client=obs.Client(own.bucket, obs.Keys(AK, SK), endpoint=own.endpoint))
        try:
            body, headers = _multipart([("app.log", _log(lap / "s.log").read_bytes())], {"kind": "logs"})
            assert p.call("POST", p.t("/run"), body=body, headers=headers)[0] == 303
            assert p.call("POST", p.t("/put"))[0] == 303
        finally:
            p.close()
        assert "2026-09/%s/in/app.clean.log" % PROJECT in own.objects
        assert "1 clean copy into" in p.state.message
