"""B-112: the SSH host alias of the app line comes from `work_ssh_host` of paths.conf; the docs name no test tenant
number. Invented alias, temporary host file."""
from __future__ import annotations

import re
from pathlib import Path

from awb import config, start

ROOT = Path(__file__).resolve().parent.parent
# the lab tenants of the host: their numbers stay out of the two front docs
TENANT_NUMBERS = ("10491", "10497", "10503", "10498")


def _conf(monkeypatch, tmp_path: Path, text: str) -> None:
    conf = tmp_path / "paths.conf"
    conf.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(config, "HOST_CONF", str(conf))
    monkeypatch.setenv("AWB_CONF", str(conf))


def test_the_app_line_names_the_alias_of_paths_conf(home, monkeypatch, tmp_path):
    _conf(monkeypatch, tmp_path, "work_ssh_host = lab-host\n")
    assert start.app_line(home, "tcp-q7m4").startswith("app: SSH host lab-host, folder ")


def test_without_the_key_the_app_line_names_the_old_alias(home, monkeypatch, tmp_path):
    _conf(monkeypatch, tmp_path, "work_host = other-host\n")
    assert start.app_line(home, "tcp-q7m4").startswith("app: SSH host awb-10491, folder ")


def test_without_a_host_file_the_app_line_names_the_old_alias(home, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setenv("AWB_CONF", str(tmp_path / "none.conf"))
    assert start.app_line(home, "tcp-q7m4").startswith("app: SSH host awb-10491, folder ")


def test_no_test_tenant_number_in_readme_or_commands():
    for name in ("README.md", "COMMANDS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        found = [n for n in TENANT_NUMBERS if re.search(r"(?<!\d)%s(?!\d)" % n, text)]
        assert not found, (name, found)
