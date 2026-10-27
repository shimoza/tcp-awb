"""Task DN (2026-10-27): a deliverable put to the bucket goes out as md and HTML, the laptop fetches both and names
the Windows path of the HTML, a note from development reaches the next prompt of a project session once, and the
project CLAUDE.md carries his two rules (once, also after `awb projects sync`)."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from awb import bucket, cleanfetch, clean, cli, devnote, projects, rulesync
from awb.tcp import xchg
from tests import fixtures as fx
from tests.test_bucket import fake as bucket_fake    # noqa: F401  (fixture)
from tests.test_bucket import project                # noqa: F401  (fixture)
from tests.test_clean_fetch import BASE, CODE, lap, obs_fake    # noqa: F401  (fixtures)
from tests.test_lifecycle import run_hook, spawn
from tests.test_xchg import buckets, lab_project, quiet, svc    # noqa: F401  (fixtures)

OFFER = """# Offer for the lab

Two **flavors** and one *note*, see [the docs](https://docs.example.org/x).

- first point
- second point
  - nested point

1. one step
2. two steps

| Flavor | Hours | Price |
|---|---:|---|
| s3.large.2 | 730 | `0.10` |
| c7.xlarge.4 | 730 | 0.40 |

```
awb bucket put tcp-q7m4 offer.md
```
"""


# --------------------------------------------------------------------------- the renderer


def test_the_renderer_writes_tables_lists_and_code_into_one_page_without_assets():
    page = bucket.md_to_html(OFFER)
    assert page.startswith("<!doctype html>") and "<title>Offer for the lab</title>" in page
    assert page.count("<style>") == 1 and "<script" not in page and "<link" not in page and "<img" not in page
    assert "<h1>Offer for the lab</h1>" in page
    assert "<strong>flavors</strong>" in page and "<em>note</em>" in page
    assert "the docs (https://docs.example.org/x)" in page and "<a " not in page
    assert "<ul>\n<li>first point" in page and "<li>nested point" in page and page.count("<ul>") == 2
    assert "<ol>\n<li>one step" in page and "<li>two steps" in page
    assert "<th>Flavor</th><th>Hours</th><th>Price</th>" in page
    assert "<tr><td>s3.large.2</td><td>730</td><td><code>0.10</code></td></tr>" in page
    assert "border:1px solid" in page and 'border="1"' in page
    assert "<pre><code>awb bucket put tcp-q7m4 offer.md</code></pre>" in page
    assert "|---" not in page and "**" not in page


def test_the_renderer_escapes_markup_of_the_text():
    page = bucket.md_body("a <b>x</b> & `<i>`\n\n| a | b |\n|---|---|\n| <x> | y |\n")
    assert "&lt;b&gt;x&lt;/b&gt; &amp; <code>&lt;i&gt;</code>" in page and "<td>&lt;x&gt;</td>" in page


# --------------------------------------------------------------------------- awb bucket put


def test_put_writes_the_md_and_its_html_page_into_out(home, bucket_fake, project, tmp_path):
    f = tmp_path / ("%s-offer-en.md" % project.code)
    f.write_text(OFFER, encoding="utf-8")
    key, stats = bucket.put(home, bucket_fake.client(), project.code, f)
    assert key.endswith("/out/%s-offer-en.md" % project.code) and stats is None
    page_key = bucket.html_key(key)
    assert page_key == key[:-3] + ".html"
    assert bucket_fake.objects[key] == f.read_bytes()
    page = bucket_fake.objects[page_key].decode("utf-8")
    assert "<th>Flavor</th>" in page and "<li>first point" in page and "<ol>" in page
    with pytest.raises(bucket.BucketError, match="--replace"):
        bucket.put(home, bucket_fake.client(), project.code, f)
    bucket.put(home, bucket_fake.client(), project.code, f, replace=True)
    other = tmp_path / "sheet.csv"
    other.write_text("a,b\n", encoding="utf-8")
    key2, _ = bucket.put(home, bucket_fake.client(), project.code, other)
    assert bucket.html_key(key2) is None and key2[:-4] + ".html" not in bucket_fake.objects


def test_a_revealed_put_renders_the_page_from_the_named_text(home, bucket_fake, project, tmp_path):
    f = tmp_path / "offer.md"
    f.write_text("# Offer\n\n| for | x |\n|---|---|\n| %s | 1 |\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    key, stats = bucket.put(home, bucket_fake.client(), project.code, f, reveal=True)
    page = bucket_fake.objects[bucket.html_key(key)].decode("utf-8")
    assert "<td>%s</td>" % fx.CUSTOMER_FORMS[0] in page and fx.CUSTOMER_CODE not in page
    assert not any((home.vault / "tmp").iterdir())


def test_the_put_output_names_both_objects_and_the_get_it_line(home, bucket_fake, project, tmp_path, monkeypatch,
                                                               capsys):
    monkeypatch.setattr(bucket, "client", lambda: bucket_fake.client())
    f = tmp_path / "reply.md"
    f.write_text("# Reply\n\nplain\n", encoding="utf-8")
    assert bucket.main(["put", project.code, str(f)]) == 0
    out = capsys.readouterr().out.splitlines()
    folder = "%s/%s/out/" % (project.created[:7], project.code)
    assert out[0] == "%s: %sreply.md" % (project.code, folder)
    assert out[1].startswith("%s: %sreply.html  (" % (project.code, folder))
    assert out[-1] == bucket.get_it_line(folder + "reply.md", project.code)
    assert out[-1].startswith("get it: Total") and out[-1].endswith("or on the laptop: awb clean fetch %s" % project.code)
    assert "bucket awb, folder %s," % folder in out[-1]
    assert len(out) == 3 and "/home/" not in "\n".join(out)


# --------------------------------------------------------------------------- awb xchg put


def test_xchg_put_of_a_markdown_file_writes_the_html_beside_it_and_names_both(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    f = Path(lab_project.path) / "evidence" / ("%s-offer-en.md" % lab_project.code)
    f.write_text(OFFER, encoding="utf-8")
    assert cli.main(["xchg", "put", str(f)]) == 0
    out = capsys.readouterr().out.splitlines()
    put = sorted(k for k in lab.objects if k.startswith("%s/from-session/" % lab_project.code))
    assert len(put) == 2 and all(xchg.put_key_ok(k, lab_project.code) for k in put)
    page, md = put                                   # put-<hex>.html sorts before put-<hex>.md
    assert page.endswith(".html") and md.endswith(".md") and page[:-5] == md[:-3]
    assert lab.objects[md] == f.read_bytes()
    html = lab.objects[page].decode("utf-8")
    assert "<th>Flavor</th>" in html and "<li>first point" in html and "<title>Offer for the lab</title>" in html
    folder = md.rsplit("/", 1)[0] + "/"
    assert out[0].startswith("put: %s-offer-en.md as %s (" % (lab_project.code, md))
    assert out[1] == "put: the same text as HTML as %s (open it, select all, paste into the mail)" % page
    assert out[2] == xchg.get_it_line("awb-lab-eu-de", md)
    assert out[2].startswith("get it: Total") and ("bucket awb-lab-eu-de, folder %s " % folder) in out[2]
    assert "only out/ of the owner bucket" in out[2] and len(out) == 3
    assert len(svc.notes) == 1 and md.rsplit("/", 1)[-1] in svc.notes[-1][1], "one mail, by the id of the md"


def test_xchg_put_of_another_file_writes_no_page(svc, buckets, lab_project, capsys):
    lab, _ = buckets
    f = Path(lab_project.path) / "evidence" / "sizes.csv"
    f.write_text("flavor,count\ns3.large.2,4\n", encoding="utf-8")
    assert cli.main(["xchg", "put", str(f)]) == 0
    out = capsys.readouterr().out.splitlines()
    put = [k for k in lab.objects if k.startswith("%s/from-session/" % lab_project.code)]
    assert len(put) == 1 and put[0].endswith(".csv")
    assert len(out) == 2 and out[1] == xchg.get_it_line("awb-lab-eu-de", put[0])


# --------------------------------------------------------------------------- awb clean fetch


def test_fetch_brings_the_md_and_the_html_and_names_the_windows_path(lap, obs_fake, tmp_path, monkeypatch):
    obs_fake.objects[BASE + "out/offer.html"] = b"<!doctype html>\n<p>offer v1</p>\n"
    monkeypatch.setattr(clean, "_client", lambda what="--put": obs_fake.client())
    drive = tmp_path / "mnt" / "d"
    real = clean.windows_path

    def mapped(text):
        out = real(text)
        return str(tmp_path) + out if out.startswith("/mnt/") else out

    monkeypatch.setattr(clean, "windows_path", mapped)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert clean.main(["fetch", CODE, "--to", r"D:\Deliveries\offers"]) == clean.EXIT_OK
    dest = drive / "Deliveries" / "offers"
    assert (dest / "offer.md").is_file() and (dest / "offer.html").read_bytes().startswith(b"<!doctype html>")
    lines = out.getvalue().splitlines()
    assert "offer.html: fetched, 32 bytes" in lines
    opened = [x for x in lines if x.startswith("open in the browser")]
    assert len(opened) == 1 and opened[0].endswith(str(dest / "offer.html"))


def test_the_windows_path_of_a_folder_under_mnt():
    assert cleanfetch.windows_of(Path("/mnt/d/Deliveries/offers/offer.html")) == "D:\\Deliveries\\offers\\offer.html"
    assert cleanfetch.windows_of(Path("/srv/x/offer.html")) == "/srv/x/offer.html"
    assert clean.windows_path(r"D:\Deliveries\offers") == "/mnt/d/Deliveries/offers"


# --------------------------------------------------------------------------- awb projects note


def test_a_note_lands_in_the_project_and_the_prompt_shows_it_once(home, register_path, monkeypatch, capsys,
                                                                  tmp_path):
    pr = spawn(home, register_path)
    note = tmp_path / "note.md"
    note.write_text("Put deliverables with awb bucket put; they go as HTML and md.\n", encoding="utf-8")
    assert cli.main(["projects", "note", pr.code, "--title", "deliverables as HTML", str(note)]) == 0
    out = capsys.readouterr().out
    files = sorted((Path(pr.path) / devnote.FOLDER).iterdir())
    assert [f.name for f in files] == ["%s-1.md" % files[0].name[:10]]
    assert out.strip() == "%s: notes/from-dev/%s written; its next session prompt shows it" % (pr.code, files[0].name)
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o640
    assert files[0].read_text(encoding="utf-8").startswith("# deliverables as HTML\n\nPut deliverables")
    payload = {"cwd": pr.path, "session_id": "dn-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    code, said, _ = run_hook("prompt", dict(payload, prompt="go on"), monkeypatch, capsys)
    line = "note from development: deliverables as HTML (notes/from-dev/%s): read it first" % files[0].name
    assert code == 0 and json.loads(said)["hookSpecificOutput"]["additionalContext"] == line
    assert run_hook("prompt", dict(payload, prompt="go on"), monkeypatch, capsys) == (0, "", "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("a second note\n"))
    assert cli.main(["projects", "note", pr.code, "--title", "second", "-"]) == 0
    capsys.readouterr()
    assert (Path(pr.path) / devnote.FOLDER / ("%s-2.md" % files[0].name[:10])).is_file()
    said = rulesync.notice(home, "dn-session", Path(pr.path))
    assert said == "note from development: second (notes/from-dev/%s-2.md): read it first" % files[0].name[:10]
    assert devnote.notices(home, "another-project-session", Path(pr.path).parent / "tcp-zzzz") == []


def test_a_note_with_a_secret_or_without_a_title_is_refused(home, register_path, capsys):
    pr = spawn(home, register_path)
    with pytest.raises(devnote.NoteError, match="secret"):
        devnote.write(home, pr.code, "keys", "the key is " + "-----BEGIN" + " RSA PRIVATE KEY-----\nabc\n")
    with pytest.raises(devnote.NoteError, match="--title"):
        devnote.write(home, pr.code, "  ", "text")
    with pytest.raises(devnote.NoteError, match="not a registered project"):
        devnote.write(home, "tcp-zzzz", "t", "text")
    assert not (Path(pr.path) / devnote.FOLDER).exists() or not any((Path(pr.path) / devnote.FOLDER).iterdir())


# --------------------------------------------------------------------------- the work rules


def test_the_template_and_the_sync_carry_the_two_rules_once(home, register_path, tmp_path):
    pr = spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.count(devnote.FILE_RULE) == 1 and lines.count(devnote.NOTE_RULE) == 1
    assert "Commander, or awb clean fetch" in devnote.FILE_RULE and "HTML and md" in devnote.FILE_RULE
    assert "no tables or bullets" in devnote.FILE_RULE and "never overrides his own word" in devnote.NOTE_RULE
    assert len(lines) <= projects.MAX_CLAUDE_LINES
    assert rulesync.sync_project(Path(pr.path)) == []
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    older = text.replace(devnote.FILE_RULE + "\n", "").replace(devnote.NOTE_RULE + "\n", "")
    (root / "CLAUDE.md").write_text(older, encoding="utf-8")
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    synced = (root / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    at = synced.index(devnote.RESOURCES_LINE)
    assert synced[at + 1:at + 3] == [devnote.FILE_RULE, devnote.NOTE_RULE]
    assert rulesync.sync_project(root) == [] and rulesync.current(root)
