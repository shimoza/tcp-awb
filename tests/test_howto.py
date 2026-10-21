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



def test_the_draft_keeps_the_labels_as_the_screen_spells_them(tmp_path, project):
    base = make_mirror(tmp_path)
    text = howto.draft("create widget", base=base, project=project, service="zorbal")[0].read_text(encoding="utf-8")
    assert "Fields: `Widget name`, `Size`" in text
    assert "Buttons: `Create now`, `Cancel`" in text
    assert "Buttons: `Create widget`, `Delete`" in text





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
    # read only by the mode bits, whoever runs the test: nobody but the owner has a write bit
    for f in (dest / "console" / DAY).rglob("*"):
        assert stat.S_IMODE(f.stat().st_mode) & 0o022 == 0
    assert mirror.root() == dest


# Task ST: console screenshots stay internal. The default draft is the outward text; --internal is the image version.

def test_the_default_draft_has_no_image_link_and_no_mirror_citation(tmp_path, project):
    base = make_mirror(tmp_path)
    path, n, lacking = howto.draft("create widget", base=base, project=project, service="zorbal")
    text = path.read_text(encoding="utf-8")
    assert path == project / "deliverables" / "howto-create-widget.md"
    assert text.startswith("# How to: create widget\n") and "## Before you start" in text
    assert "![" not in text and "images/" not in text and not (project / "deliverables" / "images").exists()
    for word in ("mirror", "screenshot", "walk", "zorbal/01-widget-list", DAY):
        assert word not in text.lower()
    # the steps keep the order, the menu path and the labels as the screen spells them
    assert text.index("1. **Widget list**") < text.index("2. **Create widget**") < text.index("3. **Confirm widget**")
    assert "Menu path: Zorbal > Widgets > Create" in text
    assert "Fields: `Widget name`, `Size`" in text and "Buttons: `Create now`, `Cancel`" in text
    assert "Buttons: `Create widget`, `Delete`" in text and "zorbal-04-settings" not in text
    assert (n, lacking) == (3, 0)


def test_the_default_draft_puts_the_citations_and_the_walk_date_into_sources(tmp_path, project):
    base = make_mirror(tmp_path)
    howto.draft("create widget", base=base, project=project, service="zorbal")
    note = (project / "reviews" / "howto-create-widget" / "sources.md").read_text(encoding="utf-8")
    assert "internal, never publish" in note and "Console walk: %s" % DAY in note
    order = [note.index(x) for x in ("zorbal/01-widget-list, console mirror %s" % DAY,
                                     "zorbal/02-widget-create, console mirror %s" % DAY,
                                     "zorbal/03-widget-confirm, console mirror %s" % DAY)]
    assert order == sorted(order)
    assert "quimble" not in note and "04-settings" not in note


def test_the_internal_draft_has_the_citations_and_the_image_links(tmp_path, project):
    base = make_mirror(tmp_path)
    path, n, lacking = howto.draft("create widget", base=base, project=project, service="zorbal", internal=True)
    text = path.read_text(encoding="utf-8")
    assert "walk of %s" % DAY in text
    order = [text.index(x) for x in ("zorbal/01-widget-list, console mirror", "zorbal/02-widget-create, console mirror",
                                     "zorbal/03-widget-confirm, console mirror")]
    assert order == sorted(order)
    assert "![Widget list](images/zorbal-01-widget-list.png)" in text
    assert "![Create widget](images/zorbal-02-widget-create.png)" in text
    assert "Screenshot: not in the mirror yet: check on screen" in text       # the dropped image
    copied = sorted(p.name for p in (project / "deliverables" / "images").iterdir())
    assert copied == ["zorbal-01-widget-list.png", "zorbal-02-widget-create.png"]
    src = base / "console" / DAY / "zorbal" / "01-widget-list.png"
    assert (project / "deliverables" / "images" / "zorbal-01-widget-list.png").read_bytes() == src.read_bytes()
    assert (n, lacking) == (3, 1) and not (project / "reviews").exists()


def test_a_step_the_reference_does_not_hold_is_marked_in_both_versions(tmp_path, project):
    base = make_mirror(tmp_path)
    path, n, lacking = howto.draft("widgets", base=base, project=project, steps=["widget list", "teleport sprocket"])
    text = path.read_text(encoding="utf-8")
    assert (n, lacking) == (2, 1)
    assert "2. **teleport sprocket**\n   - Not confirmed yet: check on screen" in text and "mirror" not in text.lower()
    note = (project / "reviews" / "howto-widgets" / "sources.md").read_text(encoding="utf-8")
    assert "2. teleport sprocket: not in the mirror yet" in note
    inner = howto.draft("widgets", base=base, project=project, steps=["widget list", "teleport sprocket"],
                        name="inner", internal=True)[0].read_text(encoding="utf-8")
    assert "2. **teleport sprocket**\n   - Screen: not in the mirror yet: check on screen" in inner
    # a screen whose image was dropped keeps its labels
    text2 = howto.draft("confirm", base=base, project=project, steps=["confirm widget"], name="two")[0].read_text(
        encoding="utf-8")
    assert "Buttons: `Confirm`" in text2 and "![" not in text2
    none = howto.draft("nothingmatches", base=base, project=project)
    assert none[1:] == (1, 1)


def test_render_refuses_a_draft_with_images_unless_internal(tmp_path, project):
    base = make_mirror(tmp_path)
    inner = howto.draft("create widget", base=base, project=project, service="zorbal", internal=True)[0]
    with pytest.raises(howto.HowtoError, match="--internal"):
        howto.render(inner)
    assert not inner.with_suffix(".html").exists()
    out, _, lacking = howto.render(inner, internal=True)
    page = out.read_text(encoding="utf-8")
    assert out == inner.with_suffix(".html") and lacking == 1
    assert page.index("internal, never publish") < page.index("<h1>")
    assert page.count("data:image/png;base64,") == 2 and "images/" not in page
    inline = page.split("data:image/png;base64,")[1].split('"')[0]
    assert base64.b64decode(inline) == (project / "deliverables" / "images" / "zorbal-01-widget-list.png").read_bytes()
    assert "<ol>" in page and "<code>Create now</code>" in page and "src=\"http" not in page and "<script" not in page


def test_render_of_the_outward_draft_has_no_image_and_no_banner(tmp_path, project):
    base = make_mirror(tmp_path)
    path = howto.draft("create widget", base=base, project=project, service="zorbal")[0]
    out, _, lacking = howto.render(path)
    page = out.read_text(encoding="utf-8")
    assert lacking == 0 and "<img" not in page and "data:image" not in page and "internal" not in page
    assert "<h1>How to: create widget</h1>" in page and "<code>Create now</code>" in page


def test_the_command_line_drafts_renders_and_pdf_waits(tmp_path, project, monkeypatch, capsys):
    base = make_mirror(tmp_path)
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    monkeypatch.chdir(project)
    assert howto.main(["draft", "create widget", "--service", "zorbal", "--step", "widget list", "--name", "w"]) == 0
    assert "1 step(s), 0 not confirmed yet" in capsys.readouterr().out
    assert howto.main(["draft", "create widget", "--name", "w"]) == 2          # exists
    md = project / "deliverables" / "howto-w.md"
    assert "![" not in md.read_text(encoding="utf-8")
    assert howto.main(["render", str(md)]) == 0
    out = capsys.readouterr().out
    assert "howto-w.html" in out and "xchg put" in out and "--image" not in out
    assert howto.main(["render", str(md), "--pdf"]) == 1
    assert "no PDF yet" in capsys.readouterr().err and md.with_suffix(".html").is_file()
    # the image version: --internal to draft and to render, and the render says it never leaves
    assert howto.main(["draft", "create widget", "--service", "zorbal", "--name", "i", "--internal"]) == 0
    capsys.readouterr()
    inner = project / "deliverables" / "howto-i.md"
    assert howto.main(["render", str(inner)]) == 2
    assert "--internal" in capsys.readouterr().err
    assert howto.main(["render", str(inner), "--internal"]) == 0
    out = capsys.readouterr().out
    assert "internal, never publish" in out and "xchg put" not in out
    assert howto.main([]) == 2


def test_xchg_put_refuses_a_file_that_embeds_or_links_a_mirror_image(tmp_path):
    reg = tmp_path / "register.tsv"
    reg.write_text("code\tkind\tform\tadded\tstatus\n", encoding="utf-8")
    files = {
        "page.html": '<html><img src="data:image/png;base64,AAAA"></html>',
        "link.html": '<html><img src="images/zorbal-01-widget-list.png"></html>',
        "draft.md": "# T\n\n![Widget list](images/zorbal-01-widget-list.png)\n",
        "plain.html": "<html><p>no picture</p></html>",
        "plain.md": "# T\n\n1. **Widget list**\n   - Buttons: `Delete`\n",
    }
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    for name in ("page.html", "link.html", "draft.md"):
        for image in (False, True):          # --image and a reason do not open it
            got = xchg.check_problems(tmp_path / name, image=image, register=reg)
            assert len(got) == 1 and "never crosses" in got[0], name
        assert xchg.check_problems(tmp_path / name, register=reg, mirror_image=True) == [], name
    for name in ("plain.html", "plain.md"):
        assert xchg.check_problems(tmp_path / name, register=reg) == []


def test_the_internal_image_flag_is_the_owners_alone(tmp_path, monkeypatch):
    page = tmp_path / "howto.html"
    page.write_text('<html><img src="data:image/png;base64,AAAA"></html>', encoding="utf-8")
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(xchg.XchgError, match="owner"):
        xchg.put(page, mirror_image=True)
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(xchg.XchgError, match="owner"):
        xchg.put(page, mirror_image=True)


def test_the_generated_docs_do_not_say_the_console_reference_holds_screenshots():
    root = Path(__file__).resolve().parent.parent
    for name in ("README.md", "COMMANDS.md", "docs/commands-guide.md"):
        for n, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), start=1):
            low = line.lower()
            if "howto" in low or "mirror console" in low or "console reference" in low:
                for word in ("screenshot", "console mirror", "mirror walk"):
                    assert word not in low, "%s:%d says %r" % (name, n, word)
    for name in ("plugin/skills/drafting/SKILL.md", "seal/work-claude/skills/drafting/SKILL.md",
                 "seal/work-claude/CLAUDE.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "screenshot" not in text.lower().replace("no mention of screenshots, images, a mirror or a walk", "")
