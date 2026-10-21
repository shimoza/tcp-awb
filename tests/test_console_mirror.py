"""awb/tcp/console_mirror.py: a small synthetic mirror under tmp, invented services and text, no real screen."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import config
from awb.tcp import console_mirror, mirror

HEADER = "service\tfile\tmenu\turl\ttitle\tdate\tnote"


def make(tmp: Path, day="2026-10-20", extra_walk=False) -> Path:
    base = tmp / "mirrors"
    walk = base / "console" / day
    rows = [
        ("zorbal", "01-widget-list.jpg", "Zorbal > Widgets", "/zorbal/widgets", "Widget list", day,
         "fields: widget name, flavor; buttons: create widget, delete"),
        ("zorbal", "02-widget-create.png", "Zorbal > Widgets > Create", "/zorbal/widgets/create", "Create widget", day,
         "fields: widget name, size; tabs: basic"),
        ("zorbal", "03-leaked.none", "Zorbal > Secrets", "/zorbal/secrets/{id}", "Secret view", day, "fields: token"),
        ("quimble", "01-gadget-list.jpg", "Quimble > Gadgets", "/quimble/gadgets", "Gadget list", day,
         "fields: gadget name; buttons: create gadget"),
    ]
    for svc, fname, *_ in rows:
        (walk / svc).mkdir(parents=True, exist_ok=True)
    (walk / "INDEX.tsv").write_text(HEADER + "\n" + "\n".join("\t".join(r) for r in rows) + "\nshort\tline\n",
                                    encoding="utf-8")
    (walk / "zorbal" / "01-widget-list.jpg").write_bytes(b"x")
    (walk / "zorbal" / "01-widget-list.txt").write_text("Widget list\nName  Status\nfrobnicate widget wizard\n")
    (walk / "zorbal" / "02-widget-create.png").write_bytes(b"x")
    (walk / "zorbal" / "02-widget-create.txt").write_text("Create widget\nPick the size of the widget\n")
    (walk / "zorbal" / "03-leaked.txt").write_text("Secret view\n")
    (walk / "quimble" / "01-gadget-list.txt").write_text("Gadget list\nsprocket gadget table\n")
    if extra_walk:
        old = base / "console" / "2026-09-01"
        old.mkdir(parents=True)
        (old / "INDEX.tsv").write_text("")
    return base


def test_find_ranks_by_overlap_and_prints_the_matching_line(tmp_path, capsys):
    base = make(tmp_path)
    day, hits = console_mirror.find(base, "create widget")
    assert day == "2026-10-20"
    assert [h[1].ref for h in hits][0] == "zorbal/02-widget-create"
    assert hits[0][0] > hits[-1][0]
    assert [h[0] for h in hits] == sorted((h[0] for h in hits), reverse=True)
    assert hits[0][2].startswith("Create widget")


def test_find_reaches_the_text_and_filters_by_service(tmp_path):
    base = make(tmp_path)
    assert [h[1].ref for h in console_mirror.find(base, "frobnicate")[1]] == ["zorbal/01-widget-list"]
    assert [h[1].ref for h in console_mirror.find(base, "list", service="quimble")[1]] == ["quimble/01-gadget-list"]
    assert console_mirror.find(base, "list", limit=1)[1].__len__() == 1
    assert console_mirror.find(base, "nothingmatches")[1] == []


def test_show_prints_the_index_line_the_text_and_the_image_path(tmp_path):
    base = make(tmp_path)
    out = "\n".join(console_mirror.show(base, "zorbal/01-widget-list"))
    assert "menu path: Zorbal > Widgets" in out and "url path: /zorbal/widgets" in out
    assert "frobnicate widget wizard" in out
    assert str(base / "console" / "2026-10-20" / "zorbal" / "01-widget-list.jpg") in out
    assert "image: none" in "\n".join(console_mirror.show(base, "zorbal/03-leaked"))
    assert str(base) in "\n".join(console_mirror.show(base, "zorbal/02-widget-create"))


@pytest.mark.parametrize("ref", ["../etc/passwd", "zorbal/../x", "zorbal", "zorbal/99-none", "/zorbal/01-widget-list"])
def test_show_refuses_a_bad_or_unknown_reference(tmp_path, ref):
    with pytest.raises(console_mirror.ConsoleError):
        console_mirror.show(make(tmp_path), ref)


def test_the_newest_walk_is_the_default_and_an_unknown_one_is_refused(tmp_path):
    base = make(tmp_path, extra_walk=True)
    assert console_mirror.walks(base) == ["2026-09-01", "2026-10-20"]
    assert console_mirror.find(base, "gadget")[0] == "2026-10-20"
    with pytest.raises(console_mirror.ConsoleError):
        console_mirror.find(base, "gadget", walk="2026-01-01")
    with pytest.raises(console_mirror.ConsoleError):
        console_mirror.find(tmp_path / "empty", "gadget")


def test_status_lists_the_console_mirror(tmp_path, monkeypatch, capsys):
    base = make(tmp_path, extra_walk=True)
    assert console_mirror.status(base) == "console screens: 2 walk(s), newest 2026-10-20, 4 screens of 2 services, in %s" % (
        base / "console")
    assert console_mirror.status(tmp_path / "none") == "console screens: none"
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    assert mirror.main(["status"]) == 0
    assert "console screens: 2 walk(s), newest 2026-10-20" in capsys.readouterr().out


def test_the_command_line_finds_and_shows_and_the_work_user_may(tmp_path, monkeypatch, capsys):
    base = make(tmp_path)
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    assert mirror.main(["console", "find", "create", "widget", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "zorbal\t02-widget-create.png\tZorbal > Widgets > Create\t/zorbal/widgets/create\t" in out
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(config, "host_conf", lambda: {"mirrors": str(base)})
    assert mirror.main(["console", "show", "quimble/01-gadget-list"]) == 0
    assert "sprocket gadget table" in capsys.readouterr().out
    assert mirror.main(["console", "show", "nope/01-x"]) == 2
    assert mirror.main(["console"]) == 2
    assert mirror.main(["sd"]) == 2          # updating stays refused for the work user
