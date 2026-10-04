"""The switch of the web side (seal/web/): the templates name no host, every placeholder is documented, every module
they start exists, and the private names the adapters still deployed outside the repository call stay until the
switch."""
import importlib
import re
from pathlib import Path

from awb import obs, projects, register
from awb.tcp import sweep

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "seal" / "web"
PLACEHOLDERS = {"@OWNER@", "@DOMAIN@", "@VAULT@", "@SHARED@", "@WORK_HOME@"}


def templates():
    return sorted(p for p in WEB.rglob("*") if p.is_file() and p.suffix in (".service", ".socket", ".conf"))


def test_the_templates_name_no_host_and_only_documented_placeholders():
    readme = (WEB / "README.md").read_text(encoding="utf-8")
    assert len(templates()) == 11
    for path in templates():
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text and "/opt/awb-web" not in text, path.name
        assert set(re.findall(r"@[A-Z_]+@", text)) <= PLACEHOLDERS, path.name
        assert path.name in readme or path.parent.name in readme, path.name
    for placeholder in PLACEHOLDERS:
        assert "`%s`" % placeholder in readme


def test_every_module_a_template_starts_exists():
    started = []
    for path in templates():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart=") and line != "ExecStart=":
                started += re.findall(r"-m (awb\.[\w.]+)", line)
                started += [m.replace("/opt/tcp-awb/src/", "") for m in re.findall(r"/opt/tcp-awb/src/\S+\.py", line)]
    assert len(started) == 7
    for name in started:
        if name.endswith(".py"):
            assert (REPO / name).is_file(), name
        else:
            importlib.import_module(name)


def test_the_private_names_of_the_deployed_adapters_stay_until_the_switch():
    assert projects._known_tags is projects.known_tags
    assert projects._locked is projects.locked
    assert (obs._text, obs._child, obs._local) == (obs.xml_text, obs.xml_child, obs.xml_local)
    assert obs.Client._sign is obs.Client.sign and obs.Client._url is obs.Client.url
    assert sweep._tags is sweep.tags
    assert callable(register._check)
