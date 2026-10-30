"""Two defects of the laptop side found on 2026-10-30 (tasks ZS and FA).

ZS: oh-my-zsh replaces ~/.zshrc when it is installed, so the PATH line and the source line of ~/.config/winrelay.sh
were lost. `awb clean shell` writes them into ~/.bashrc once and into awb.zsh of the oh-my-zsh custom folder, never
into ~/.zshrc; `awb clean doctor` names the one command that fixes a missing awb.zsh or an unset proxy. Every test
runs with a temporary HOME.

FA: `awb clean fetch --again NAME` fetches a file of out/ again although fetched.tsv records it and keeps the local
file of the same name."""
from __future__ import annotations

import pytest

from awb import clean, cli, laptop
from tests.test_clean_fetch import BASE, CODE, lap, obs_fake, run   # noqa: F401  (fixtures)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv(laptop.HOME_ENV, str(h / "awb-laptop"))
    monkeypatch.delenv("ZSH_CUSTOM", raising=False)
    for k in clean.PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    return h


def test_shell_writes_bash_once_and_no_zsh_file_without_oh_my_zsh(home, capsys):
    assert clean.main(["shell"]) == 0
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.count(laptop.SHELL_BEGIN) == 1 and all(line in bashrc for line in laptop.SHELL_LINES)
    assert "added to ~/.bashrc" in capsys.readouterr().out
    assert clean.main(["shell"]) == 0
    assert (home / ".bashrc").read_text(encoding="utf-8") == bashrc
    assert "has the PATH and proxy lines" in capsys.readouterr().out
    assert not (home / ".zshrc").exists() and not (home / ".oh-my-zsh").exists()


def test_shell_keeps_bashrc_and_zshrc_and_writes_awb_zsh_with_oh_my_zsh(home):
    (home / ".bashrc").write_text("alias ll='ls -l'", encoding="utf-8")       # no newline at the end
    (home / ".zshrc").write_text("plugins=(git)\n", encoding="utf-8")
    (home / ".oh-my-zsh" / "custom").mkdir(parents=True)
    lines = laptop.shell_setup()
    assert (home / ".bashrc").read_text(encoding="utf-8").startswith("alias ll='ls -l'\n" + laptop.SHELL_BEGIN)
    assert (home / ".zshrc").read_text(encoding="utf-8") == "plugins=(git)\n", "~/.zshrc is never touched"
    zsh = home / ".oh-my-zsh" / "custom" / "awb.zsh"
    first = zsh.read_text(encoding="utf-8")
    assert all(line in first for line in laptop.SHELL_LINES)
    assert lines[-1] == "zsh: the PATH and proxy lines written to ~/.oh-my-zsh/custom/awb.zsh (open a new terminal)"
    assert laptop.shell_setup()[-1] == "zsh: ~/.oh-my-zsh/custom/awb.zsh has the PATH and proxy lines"
    assert zsh.read_text(encoding="utf-8") == first
    assert sorted(p.name for p in zsh.parent.iterdir()) == ["awb.zsh"], "no part file left"


def test_zsh_custom_names_the_folder_and_a_hand_made_bashrc_is_left(home, monkeypatch):
    (home / ".oh-my-zsh").mkdir()
    custom = home / "zsh-custom"
    monkeypatch.setenv("ZSH_CUSTOM", str(custom))
    hand = 'export PATH="$HOME/.local/bin:$PATH"\n. ~/.config/winrelay.sh\n'
    (home / ".bashrc").write_text(hand, encoding="utf-8")
    laptop.shell_setup()
    assert (home / ".bashrc").read_text(encoding="utf-8") == hand
    assert (custom / "awb.zsh").is_file() and not (home / ".oh-my-zsh" / "custom").exists()


def test_init_names_the_shell_command_and_writes_nothing_there(home, capsys):
    (home / ".oh-my-zsh").mkdir()
    assert cli.main(["init", "--laptop"]) == 0
    assert "shell: the PATH and proxy lines are missing; add them with awb clean shell" in capsys.readouterr().out
    assert not (home / ".bashrc").exists() and not (home / ".oh-my-zsh" / "custom").exists()
    laptop.shell_setup()
    assert clean.main(["init"]) == 0
    assert "awb clean shell" not in capsys.readouterr().out


def test_doctor_names_the_fix_of_a_missing_awb_zsh_and_of_an_unset_proxy(home, monkeypatch):
    monkeypatch.setattr(clean, "doctor_targets", lambda: [])               # no request leaves this machine
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    (home / ".oh-my-zsh").mkdir()
    relay = home / ".config" / "winrelay.sh"
    relay.parent.mkdir()
    relay.write_text("export HTTPS_PROXY=http://127.0.0.1:3128\n", encoding="utf-8")
    lines, code = clean.doctor()
    assert code == 0
    assert [x for x in lines if x.startswith("shell:")] == [
        "shell: zsh with oh-my-zsh and no ~/.oh-my-zsh/custom/awb.zsh, so PATH and the proxy are lost; fix: awb "
        "clean shell, then open a new terminal"]
    laptop.shell_setup()
    lines, _ = clean.doctor()
    assert "proxy: HTTPS_PROXY is not set and ~/.config/winrelay.sh is there; fix: . ~/.config/winrelay.sh" in lines
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    lines, _ = clean.doctor()
    assert not [x for x in lines if x.startswith(("shell:", "proxy:"))]
    monkeypatch.delenv("HTTPS_PROXY")
    relay.unlink()
    monkeypatch.setenv("SHELL", "/bin/bash")
    (home / ".oh-my-zsh" / "custom" / "awb.zsh").unlink()
    assert laptop.shell_hint() is None, "bash, no relay: nothing to say"


def test_fetch_again_gets_a_recorded_file_and_keeps_the_local_one(lap, obs_fake, tmp_path):
    dest = tmp_path / "d"
    run(obs_fake, to=dest)
    (dest / "offer.md").write_text("my edits\n", encoding="utf-8")
    failed, lines = run(obs_fake, to=dest, again=["offer.md"])
    assert failed == 0
    assert sorted(lines) == ["offer.md: fetched, 9 bytes as offer (2).md", "reply.md: already fetched"]
    assert (dest / "offer.md").read_text(encoding="utf-8") == "my edits\n"
    assert (dest / "offer (2).md").read_bytes() == b"offer v1\n"
    (dest / "reply.md").unlink()
    failed, lines = run(obs_fake, to=dest, again=["reply.md", "offer.md"])
    assert failed == 0 and "reply.md: fetched, 15 bytes" in lines
    assert "offer.md: fetched, 9 bytes as offer (3).md" in lines


def test_fetch_again_names_a_file_out_lacks(lap, obs_fake, tmp_path, monkeypatch, capsys):
    run(obs_fake, to=tmp_path / "d")
    failed, lines = run(obs_fake, to=tmp_path / "d", again=["nothere.md"])
    assert failed == 1 and "nothere.md: not in out/, nothing fetched again" in lines
    monkeypatch.setattr(clean, "_client", lambda what="--put": obs_fake.client())
    assert clean.main(["fetch", CODE, "--to", str(tmp_path / "d"), "--again", "reply.md", "--again", "offer.md"]) == 0
    out = capsys.readouterr().out
    assert "reply.md: fetched, 15 bytes as reply (2).md" in out and "offer.md: fetched, 9 bytes as offer (2).md" in out
