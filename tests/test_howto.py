"""awb howto and awb mirror console publish: a small synthetic mirror under tmp, invented services and text, tiny
generated PNGs, no real screen."""
from __future__ import annotations

import base64
import os
import stat
import struct
import zlib
from pathlib import Path

import pytest

from awb import config
from awb.tcp import howto
from awb.tcp import console_mirror, mirror, xchg

HEADER = "service\tfile\tmenu\turl\ttitle\tdate\tnote"
DAY = "2026-10-20"


def png(color: int) -> bytes:
    """A 2x2 grey PNG, built by hand."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes([color]) * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_mirror(tmp: Path, day: str = DAY) -> Path:
    """zorbal: 01 list, 02 create (child path), 03 create dialog (dropped image), 04 unrelated settings;
    quimble: 01 gadget list. The INDEX lists them out of order on purpose."""
    base = tmp / "mirrors"
    walk = base / "console" / day
    rows = [
        ("zorbal", "02-widget-create.png", "Zorbal > Widgets > Create", "/zorbal/widgets/create", "Create widget", day,
         "fields: Widget name, Size; buttons: Create now, Cancel"),
        ("quimble", "01-gadget-list.png", "Quimble > Gadgets", "/quimble/gadgets", "Gadget list", day,
         "buttons: Create gadget"),
        ("zorbal", "04-settings.png", "Zorbal > Settings", "/zorbal/settings", "Settings", day, "tabs: general"),
        ("zorbal", "03-widget-confirm.none", "Zorbal > Widgets > Create", "/zorbal/widgets/create", "Confirm widget",
         day, "buttons: Confirm"),
        ("zorbal", "01-widget-list.png", "Zorbal > Widgets", "/zorbal/widgets", "Widget list", day,
         "fields: Widget name; buttons: Create widget, Delete"),
    ]
    (walk / "zorbal").mkdir(parents=True)
    (walk / "quimble").mkdir()
    (walk / "INDEX.tsv").write_text(HEADER + "\n" + "\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")
    for n, (svc, fname, *_rest) in enumerate(rows):
        stem = fname.rsplit(".", 1)[0]
        if not fname.endswith(".none"):
            (walk / svc / fname).write_bytes(png(40 + n * 30))
        (walk / svc / (stem + ".txt")).write_text("%s\nwidget wizard text\n" % _rest[3], encoding="utf-8")
    return base


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "tcp-q7m4"
    p.mkdir()
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    return p


def test_the_draft_orders_the_steps_by_menu_path_and_number(tmp_path, project):
    base = make_mirror(tmp_path)
    path, n, lacking = howto.draft("create widget", base=base, project=project, service="zorbal")
    text = path.read_text(encoding="utf-8")
    assert path == project / "deliverables" / "howto-create-widget.md"
    assert text.startswith("# How to: create widget\n")
    assert "## Before you start" in text and "walk of %s" % DAY in text
    order = [text.index(x) for x in ("zorbal/01-widget-list, console mirror", "zorbal/02-widget-create, console mirror",
                                     "zorbal/03-widget-confirm, console mirror")]
    assert order == sorted(order)
    assert text.index("1. **Widget list**") < text.index("2. **Create widget**") < text.index("3. **Confirm widget**")
    assert "Menu path: Zorbal > Widgets > Create" in text
    assert (n, lacking) == (3, 1)


def test_the_draft_keeps_the_labels_as_the_screen_spells_them(tmp_path, project):
    base = make_mirror(tmp_path)
    text = howto.draft("create widget", base=base, project=project, service="zorbal")[0].read_text(encoding="utf-8")
    assert "Fields: `Widget name`, `Size`" in text
    assert "Buttons: `Create now`, `Cancel`" in text
    assert "Buttons: `Create widget`, `Delete`" in text


def test_the_draft_copies_only_the_cited_images(tmp_path, project):
    base = make_mirror(tmp_path)
    path = howto.draft("create widget", base=base, project=project, service="zorbal")[0]
    copied = sorted(p.name for p in (project / "deliverables" / "images").iterdir())
    assert copied == ["zorbal-01-widget-list.png", "zorbal-02-widget-create.png"]
    text = path.read_text(encoding="utf-8")
    assert "![Widget list](images/zorbal-01-widget-list.png)" in text
    assert "zorbal-04-settings" not in text and "quimble" not in text
    src = base / "console" / DAY / "zorbal" / "01-widget-list.png"
    assert (project / "deliverables" / "images" / "zorbal-01-widget-list.png").read_bytes() == src.read_bytes()


def test_a_step_the_mirror_does_not_hold_is_marked(tmp_path, project):
    base = make_mirror(tmp_path)
    path, n, lacking = howto.draft("widgets", base=base, project=project, steps=["widget list", "teleport sprocket"])
    text = path.read_text(encoding="utf-8")
    assert (n, lacking) == (2, 1)
    assert "2. **teleport sprocket**\n   - Screen: not in the mirror yet: check on screen" in text
    # a screen whose image was dropped keeps its labels and marks only the screenshot
    path2 = howto.draft("confirm", base=base, project=project, steps=["confirm widget"], name="two")[0]
    text2 = path2.read_text(encoding="utf-8")
    assert "Buttons: `Confirm`" in text2 and "Screenshot: not in the mirror yet: check on screen" in text2
    assert "![" not in text2
    # a topic the mirror knows nothing about gives one marked step
    none = howto.draft("nothingmatches", base=base, project=project)
    assert none[1:] == (1, 1)


def test_a_draft_is_never_overwritten_without_force_and_needs_a_project(tmp_path, project):
    base = make_mirror(tmp_path)
    path = howto.draft("create widget", base=base, project=project)[0]
    path.write_text("my edited text\n", encoding="utf-8")
    with pytest.raises(howto.HowtoError):
        howto.draft("create widget", base=base, project=project)
    assert path.read_text(encoding="utf-8") == "my edited text\n"
    howto.draft("create widget", base=base, project=project, force=True)
    assert path.read_text(encoding="utf-8").startswith("# How to")
    with pytest.raises(howto.HowtoError):
        howto.draft("create widget", base=base, project=tmp_path / "mirrors")
    with pytest.raises(howto.HowtoError):
        howto.draft("   ", base=base, project=project)
    with pytest.raises(console_mirror.ConsoleError):
        howto.draft("create widget", base=base, project=project, walk="2020-01-01", name="x")


def test_render_makes_one_self_contained_html_with_the_images_inline(tmp_path, project):
    base = make_mirror(tmp_path)
    path = howto.draft("create widget", base=base, project=project, service="zorbal")[0]
    out, _, lacking = howto.render(path)
    page = out.read_text(encoding="utf-8")
    assert out == path.with_suffix(".html") and lacking == 1
    assert page.count("data:image/png;base64,") == 2 and "images/" not in page
    inline = page.split("data:image/png;base64,")[1].split('"')[0]
    assert base64.b64decode(inline) == (project / "deliverables" / "images" / "zorbal-01-widget-list.png").read_bytes()
    assert "<h1>How to: create widget</h1>" in page and "<ol>" in page and "<code>Create now</code>" in page
    assert "src=\"http" not in page and "<script" not in page


def test_render_refuses_a_missing_or_escaping_image_and_escapes_text(tmp_path, project):
    d = project / "deliverables"
    d.mkdir()
    (d / "a.md").write_text("# T\n\n![x](images/none.png)\n", encoding="utf-8")
    (d / "b.md").write_text("# T\n\n![x](../SCOPE.md)\n\n![y](/etc/passwd)\n![z](https://host.example/a.png)\n",
                            encoding="utf-8")
    (d / "c.md").write_text("# T <b>\n\n1. **a <script>alert(1)</script>**\n", encoding="utf-8")
    for name in ("a", "b"):
        with pytest.raises(howto.HowtoError):
            howto.render(d / (name + ".md"))
        assert not (d / (name + ".html")).exists()
    page = howto.render(d / "c.md")[0].read_text(encoding="utf-8")
    assert "<script>" not in page and "&lt;script&gt;" in page
    with pytest.raises(howto.HowtoError):
        howto.render(d / "missing.md")


def test_the_command_line_drafts_renders_and_pdf_waits(tmp_path, project, monkeypatch, capsys):
    base = make_mirror(tmp_path)
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    monkeypatch.chdir(project)
    assert howto.main(["draft", "create widget", "--service", "zorbal", "--step", "widget list", "--name", "w"]) == 0
    assert "1 step(s), 0 not in the mirror yet" in capsys.readouterr().out
    assert howto.main(["draft", "create widget", "--name", "w"]) == 2          # exists
    md = project / "deliverables" / "howto-w.md"
    assert howto.main(["render", str(md)]) == 0
    out = capsys.readouterr().out
    assert "howto-w.html" in out and "xchg put" in out
    assert howto.main(["render", str(md), "--pdf"]) == 1
    assert "no PDF yet" in capsys.readouterr().err and md.with_suffix(".html").is_file()
    assert howto.main([]) == 2


def test_publish_copies_the_newest_walk_read_only_and_only_what_is_cited(tmp_path):
    src = make_mirror(tmp_path)
    old = src / "console" / "2026-09-01"
    old.mkdir()
    (old / "INDEX.tsv").write_text("")
    walk = src / "console" / DAY
    (walk / "zorbal" / "notes.md").write_text("private\n")
    os.symlink("/etc/hostname", walk / "zorbal" / "link.txt")
    dest = tmp_path / "published"
    (dest / "console").mkdir(parents=True)
    day, n = console_mirror.publish(src, dest)
    assert day == DAY and n == 1 + 4 + 5          # INDEX, four images (one was dropped) and five texts
    got = dest / "console" / DAY
    assert console_mirror.walks(dest) == [DAY]
    names = sorted(p.relative_to(got).as_posix() for p in got.rglob("*") if p.is_file())
    assert "zorbal/notes.md" not in names and "zorbal/link.txt" not in names and "INDEX.tsv" in names
    for p in got.rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o027 == 0 and mode & 0o002 == 0
        assert (mode & 0o777) in ((0o640,) if p.is_file() else (0o750, 0o2750))
    # a second run replaces the walk and leaves no staging folder
    day, n2 = console_mirror.publish(src, dest)
    assert n2 == n and sorted(x.name for x in (dest / "console").iterdir()) == [DAY]
    # what the work user reads from there is what find and show print
    assert console_mirror.find(dest, "create widget")[1][0][1].ref == "zorbal/02-widget-create"
    with pytest.raises(console_mirror.ConsoleError):
        console_mirror.publish(src, tmp_path / "nowhere")
    with pytest.raises(console_mirror.ConsoleError):
        console_mirror.publish(tmp_path / "empty", dest)


def test_the_work_user_reads_the_published_copy_and_cannot_publish(tmp_path, monkeypatch, capsys):
    src = make_mirror(tmp_path)
    dest = tmp_path / "srv"
    (dest / "console").mkdir(parents=True)
    monkeypatch.setenv("AWB_MIRRORS", str(src))
    monkeypatch.setenv("AWB_MIRRORS_PUBLISH", str(dest))
    assert mirror.main(["console", "publish"]) == 0
    assert "published the walk %s" % DAY in capsys.readouterr().out
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(config, "host_conf", lambda: {"mirrors": str(dest)})
    assert mirror.main(["console", "find", "gadget"]) == 0
    assert "quimble\t01-gadget-list.png" in capsys.readouterr().out
    assert mirror.main(["console", "publish"]) == 2
    assert "never updates" in capsys.readouterr().err
    assert not os.access(dest / "console" / DAY / "INDEX.tsv", os.W_OK) or os.geteuid() == 0
    assert mirror.root() == dest


def test_a_page_with_embedded_pictures_crosses_only_as_an_image(tmp_path):
    page = tmp_path / "howto.html"
    page.write_text('<html><img src="data:image/png;base64,AAAA"></html>', encoding="utf-8")
    plain = tmp_path / "plain.html"
    plain.write_text("<html><p>no picture</p></html>", encoding="utf-8")
    reg = tmp_path / "register.tsv"
    reg.write_text("code\tkind\tform\tadded\tstatus\n", encoding="utf-8")
    assert xchg.check_problems(page, register=reg) == ["a page with embedded pictures crosses only with --image and a reason"]
    assert xchg.check_problems(page, image=True, register=reg) == []
    assert xchg.check_problems(plain, register=reg) == []
