"""`awb clean fetch` (task ux2-laptop-fetch): the laptop fetches the finished files of out/ from a fake bucket and a
fake proxy; nothing leaves this machine. The host line after `awb bucket put --reveal` is checked at the end."""
from __future__ import annotations

import os
import stat
import urllib.request

import pytest

from awb import bucket, clean, cleanfetch, laptop
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_bucket import fake as bucket_fake    # noqa: F401  (fixture)
from tests.test_bucket import project                # noqa: F401  (fixture)
from tests.test_clean_proxy import FakeProxy

CODE = "tcp-q7m4"
BASE = "2026-10/%s/" % CODE


@pytest.fixture
def lap(tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    return tmp_path / "laptop"


@pytest.fixture
def obs_fake():
    with FakeOBS() as f:
        f.objects[BASE] = b""
        f.objects[BASE + "in/"] = b""
        f.objects[BASE + "out/"] = b""
        f.objects[BASE + "in/original.docx"] = b"the original"
        f.objects[BASE + "out/.keep"] = b""
        f.objects[BASE + "out/reply.md"] = b"dear colleague\n"
        f.objects[BASE + "out/offer.md"] = b"offer v1\n"
        yield f


def run(obs_fake, project=CODE, to=None, **kw):
    lines = []
    failed = cleanfetch.fetch(project, to, client=obs_fake.client(), say=lines.append, **kw)
    return failed, lines


def test_fetch_downloads_the_files_of_out_and_prints_one_line_each(lap, obs_fake, tmp_path):
    dest = tmp_path / "win" / "Deliveries"
    failed, lines = run(obs_fake, to=dest)
    assert failed == 0
    assert sorted(lines) == ["offer.md: fetched, 9 bytes", "reply.md: fetched, 15 bytes"]
    assert (dest / "reply.md").read_bytes() == b"dear colleague\n"
    assert sorted(x.name for x in dest.iterdir()) == ["offer.md", "reply.md"], "no part file, no .keep, no in/ file"


def test_fetch_never_touches_in(lap, obs_fake, tmp_path):
    run(obs_fake, to=tmp_path / "d")
    assert not [c for c in obs_fake.calls if "/in/" in c[1] or c[1].endswith("/in")]
    assert {m for m, _ in obs_fake.calls} <= {"GET", "HEAD"}, "it reads only"
    assert obs_fake.objects[BASE + "in/original.docx"] == b"the original"


def test_a_second_run_fetches_nothing_again(lap, obs_fake, tmp_path):
    dest = tmp_path / "d"
    run(obs_fake, to=dest)
    failed, lines = run(obs_fake, to=dest)
    assert failed == 0 and sorted(lines) == ["offer.md: already fetched", "reply.md: already fetched"]
    rows = (laptop.vault() / "fetched.tsv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2 and stat.S_IMODE(os.stat(laptop.vault() / "fetched.tsv").st_mode) == 0o600


def test_a_file_put_again_comes_again_next_to_the_first(lap, obs_fake, tmp_path):
    dest = tmp_path / "d"
    run(obs_fake, to=dest)
    obs_fake.objects[BASE + "out/offer.md"] = b"offer v2\n"
    obs_fake.objects[BASE + "out/new.md"] = b"new\n"
    failed, lines = run(obs_fake, to=dest)
    assert sorted(lines) == ["new.md: fetched, 4 bytes", "offer.md: fetched, 9 bytes as offer (2).md",
                             "reply.md: already fetched"]
    assert (dest / "offer.md").read_bytes() == b"offer v1\n" and (dest / "offer (2).md").read_bytes() == b"offer v2\n"


def test_an_empty_out_and_a_missing_folder(lap, obs_fake, tmp_path):
    for k in [k for k in obs_fake.objects if k.startswith(BASE + "out/") and not k.endswith("out/")]:
        del obs_fake.objects[k]
    assert run(obs_fake, to=tmp_path / "d")[1] == ["%s: out/ holds no file yet" % CODE]
    with pytest.raises(cleanfetch.FetchError, match="no folder in the bucket"):
        run(obs_fake, project="tcp-k4n7", to=tmp_path / "d")
    with pytest.raises(laptop.LaptopError):
        run(obs_fake, project="CUST-Q7M4", to=tmp_path / "d")


def test_a_failed_download_is_named_and_not_remembered(lap, obs_fake, tmp_path, monkeypatch):
    from awb import obs
    real = obs.Client.get

    def flaky(self, key, to, if_match=None):
        if key.endswith("offer.md"):
            raise obs.OBSError("HTTP 412 PreconditionFailed", 412, "PreconditionFailed")
        return real(self, key, to, if_match)

    monkeypatch.setattr(obs.Client, "get", flaky)
    dest = tmp_path / "d"
    failed, lines = run(obs_fake, to=dest)
    assert failed == 1 and "offer.md: failed (HTTP 412 PreconditionFailed)" in lines
    assert sorted(x.name for x in dest.iterdir()) == ["reply.md"]
    assert not any("offer.md" in k for k, _ in cleanfetch.fetched())


def test_the_folder_is_named_once_in_the_laptop_config(lap, obs_fake, tmp_path):
    with pytest.raises(cleanfetch.FetchError, match="--to DIR"):
        run(obs_fake)
    dest = tmp_path / "mnt-c" / "Deliveries"
    run(obs_fake, to=dest, save=True)
    assert cleanfetch.saved_dir() == dest
    for f in dest.iterdir():
        f.unlink()
    (laptop.vault() / "fetched.tsv").unlink()
    failed, lines = run(obs_fake)                      # no --to: the saved folder
    assert failed == 0 and (dest / "reply.md").exists() and len(lines) == 2


def test_main_needs_the_key_and_the_arguments(lap, tmp_path, capsys):
    assert clean.main(["fetch", CODE, "--to", str(tmp_path / "d")]) == clean.EXIT_ERROR
    assert "needs his own key" in capsys.readouterr().err
    assert clean.main(["fetch", CODE, "--save"]) == clean.EXIT_ERROR
    assert "--save goes with --to" in capsys.readouterr().err


def test_main_prints_the_lines_and_exits_zero(lap, obs_fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(clean, "_client", lambda what="--put": obs_fake.client())
    assert clean.main(["fetch", CODE, "--to", str(tmp_path / "d")]) == clean.EXIT_OK
    assert capsys.readouterr().out.splitlines().count("reply.md: fetched, 15 bytes") == 1


def test_fetch_goes_through_the_proxy_of_the_environment(lap, tmp_path, monkeypatch):
    proxy = FakeProxy()
    try:
        for k in clean.PROXY_VARS:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", proxy.url())
        monkeypatch.setenv("https_proxy", proxy.url())
        monkeypatch.setattr(urllib.request, "_opener", None)
        monkeypatch.setenv("AWB_OBS_AK", "AKLABFAKE")
        monkeypatch.setenv("AWB_OBS_SK", "sk-lab-fake-secret")
        with pytest.raises(cleanfetch.FetchError, match="the bucket"):
            cleanfetch.fetch(CODE, tmp_path / "d", say=lambda s: None)
        assert proxy.lines and all(x == "CONNECT awb.obs.eu-de.otc.t-systems.com:443 HTTP/1.1" for x in proxy.lines)
    finally:
        proxy.close()


def test_the_host_names_the_laptop_step_after_a_revealed_put(home, bucket_fake, project, tmp_path, monkeypatch,
                                                              capsys):
    monkeypatch.setattr(bucket, "client", lambda: bucket_fake.client())
    f = tmp_path / "offer.md"
    f.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    assert bucket.main(["put", project.code, str(f), "--reveal"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "on the laptop run: awb clean fetch %s" % project.code
    assert fx.CUSTOMER_FORMS[0] not in "\n".join(out)
    f2 = tmp_path / "plain.md"
    f2.write_text("plain\n", encoding="utf-8")
    assert bucket.main(["put", project.code, str(f2)]) == 0
    assert "awb clean fetch" not in capsys.readouterr().out
