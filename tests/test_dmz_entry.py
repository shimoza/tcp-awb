"""DMZ 1 (B-085, ask 2026-11-25-tcp-x7k2-1): the entry keeps registered names and real secrets only.

The doors of customer material (awb import, awb import --paste, awb paste, awb material add, the intake of the console)
keep an address, a host name, a web address (the platform's own endpoints included), a hash, a UUID and a tenant or
project number as written: no finding, no token. Registered names become their codes, mail addresses stay a finding,
a real secret becomes [secret]. A technical word (VMs, IPs, pre-shared) is never a person or a place. The guard of the
tool calls reads the pattern of a grep as text. Every name is an invented fixture name; the secrets are put together
at run time.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from awb import bucket, check, cli, config, hooks, importcmd, intake, paste, pastein, projects, start, wipe
from awb import toolguard as tg
from awb.intake.engine import _Engine
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"
TENANT = "0f1e2d3c" "4b5a6978" "8796a5b4" "c3d2e1f0"       # put together: the gate reads a whole identifier as a finding
UUID = "6f7d0a52-3b1c" "-4e8a-9c1d-2a5b7e9f1c34"
MD5 = "9e107d9d372bb682" "6bd81d3542a419d6"
SHA256 = "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b"
PUBLIC_IP, PUBLIC_IP_2, PRIVATE_IP, IPV6 = "203.0.113.45", "198.51.100.17", "172.16.4.9", "2001:db8:abcd::17"
ENDPOINT = "https://ecs.eu-de.otc.t-systems.com/v1/%s/cloudservers/detail?limit=10" % TENANT
GATEWAY = "https://gw.qvelmar-demo.net/api/v2/tokens"
INTRANET = "http://intranet.qvelmar-demo.net:8080/admin"
HOSTS = ("srv-app01.corp.qvelmar-demo.net", "db-02.internal.qvelmar-demo.net", "web-01")
NUMBERS = ("123456", "54321", "987654")
SCRIPT = ('pm.environment.set("token", pm.response.json().access_token);',
          'pm.environment.set("password", pm.variables.get("pwd"));',
          "const key = $env:TOKEN;")
HEADERS = ("X-Auth-Token: {{token}}", "Authorization: Bearer {{access_token}}", "export TOKEN=$(get-token --quiet)")
TECHNICAL = ("VMs", "IPs", "ECSs", "VPCs", "SGs", "pre-shared", "pre-shared-key")

TERRAFORM = '''\
# Two VMs, three IPs and a pre-shared key for the VPN. The VPCs and SGs come from the base module.
provider "opentelekomcloud" {
  auth_url    = "https://iam.eu-de.otc.t-systems.com/v3"
  domain_name = "OTC-EU-DE-00000000"
  tenant_id   = "%(tenant)s"
}

resource "opentelekomcloud_compute_instance_v2" "web" {
  name      = "web-01"
  image_id  = "%(uuid)s"
  flavor_id = "s3.large.2"
  network {
    fixed_ip_v4 = "%(private)s"
  }
}

resource "opentelekomcloud_vpnaas_ike_policy_v2" "ike" {
  pre_shared_key = var.pre-shared-key
  peer_address   = "%(public)s"
  peer_v6        = "%(v6)s"
}

output "ecs_endpoint" {
  value = "%(endpoint)s"
}
''' % {"tenant": TENANT, "uuid": UUID, "private": PRIVATE_IP, "public": PUBLIC_IP, "v6": IPV6, "endpoint": ENDPOINT}

COLLECTION = {
    "info": {"name": "Handover ECS", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "item": [{
        "name": "List servers",
        "event": [{"listen": "test", "script": {"exec": list(SCRIPT)}}],
        "request": {
            "method": "GET",
            "header": [{"key": "X-Auth-Token", "value": "{{token}}"}],
            "url": {"raw": "https://ecs.eu-de.otc.t-systems.com/v1/{{project_id}}/cloudservers/detail?limit=10"},
            "description": "The VMs of the tenant %s, checksum %s on %s." % (NUMBERS[0], MD5, PUBLIC_IP),
        },
    }],
}

COLLECTION["item"] += [
    {"name": name,
     "request": {"method": "POST", "header": [{"key": "X-Auth-Token", "value": "{{token}}"}],
                 "url": {"raw": "https://ecs.eu-de.otc.t-systems.com/v1/{{project_id}}/cloudservers",
                         "host": ["ecs", "eu-de", "otc", "t-systems", "com"], "path": ["v1", "{{project_id}}"]}},
     "event": [{"listen": "test", "script": {"exec": ['pm.environment.set("server_id", pm.response.json().server.id);']}}]}
    for name in ("Create vm", "List VMs", "Delete eip", "Bind elastic IP", "Get VPCs and subnets", "Pre-shared key rotation",
                 "Add NIC to ECS", "IAM token (project scoped)", "NAT gateway SNAT rule", "Create vpn connection")]

NOTES = """\
# Handover notes

The three VMs and the IPs behind them sit in two VPCs with four SGs. The ECSs use a pre-shared key and a
pre-shared-key rotation of 90 days.

Owner: IPs
Contact: pre-shared-key

- Gateway: %(gateway)s (public) and %(intranet)s
- Endpoints: %(endpoint)s and https://iam.eu-de.otc.t-systems.com/v3
- Hosts: %(h0)s, %(h1)s, %(h2)s
- Public IPs %(public)s and %(public2)s, private %(private)s, IPv6 %(v6)s
- Tenant %(n0)s, domain %(n1)s, project %(n2)s
- sha256 %(sha)s, md5 %(md5)s
- uuid %(uuid)s and ecs.eu-de.otc.t-systems.com

```javascript
%(script)s
%(headers)s
```
""" % {"gateway": GATEWAY, "intranet": INTRANET, "h0": HOSTS[0], "h1": HOSTS[1], "h2": HOSTS[2], "public": PUBLIC_IP,
       "public2": PUBLIC_IP_2, "private": PRIVATE_IP, "v6": IPV6, "n0": NUMBERS[0], "n1": NUMBERS[1], "n2": NUMBERS[2],
       "sha": SHA256, "md5": MD5, "uuid": UUID, "endpoint": ENDPOINT, "script": "\n".join(SCRIPT), "headers": "\n".join(HEADERS)}

KEPT = ([TENANT, UUID, MD5, SHA256, PUBLIC_IP, PUBLIC_IP_2, PRIVATE_IP, IPV6, GATEWAY, INTRANET,
         "https://ecs.eu-de.otc.t-systems.com/v1/", "https://iam.eu-de.otc.t-systems.com/v3", "ecs.eu-de.otc.t-systems.com"]
        + list(HOSTS) + list(NUMBERS) + list(SCRIPT) + list(HEADERS) + list(TECHNICAL))


def private_key() -> str:
    return "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEAu3xq\nb7Zk9Qe1\n-----END RSA " + "PRIVATE KEY-----\n"


def password() -> str:
    return "Hunt" + "er2-Zx9!qq"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _reader(lines):
    it = iter(lines)
    return lambda: next(it, "")


@pytest.fixture
def project(home, capsys):
    assert start.start(home, "project", GOAL, "new", read=_reader(["\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    capsys.readouterr()
    return pr


def _copy(home, cust) -> str:
    files = [x for x in (home.outbox / cust).rglob("F-*") if x.is_file()]
    assert files, sorted(x.name for x in (home.outbox / cust).rglob("*"))
    return "\n".join(x.read_text(encoding="utf-8", errors="replace") for x in files)


def _import(home, pr, tmp_path, files: dict[str, str], **kwargs) -> str:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    paths = []
    for name, text in files.items():
        (src / name).write_text(text, encoding="utf-8")
        paths.append(src / name)
    assert importcmd.run(home, pr.code, paths, tty=True, **kwargs) == 0
    return _copy(home, pr.customer)


def _assert_no_token(copy: str) -> None:
    for token in ("[person", "[place", "[company", "[name", "[secret", "URL-", "IP-", "PHONE-", "TAX-"):
        assert token not in copy, token


def _kit(tmp_path_files=None) -> dict[str, str]:
    return {"main.tf": TERRAFORM, "handover.postman_collection.json": json.dumps(COLLECTION, indent=2),
            "notes.md": NOTES}


# --------------------------------------------------------------------------- the entry wipe


def test_a_handover_kit_arrives_unchanged(home, project, tmp_path, capsys):
    copy = _import(home, project, tmp_path, _kit())
    assert "wiped: nothing" in capsys.readouterr().out
    for value in KEPT:
        assert value in copy.replace("\\", ""), value
    _assert_no_token(copy)


def test_trusted_keeps_the_kit_as_written_too(home, project, tmp_path, capsys):
    copy = _import(home, project, tmp_path, _kit(), trusted=True)
    for value in KEPT:
        assert value in copy.replace("\\", ""), value
    _assert_no_token(copy)


def test_the_same_kit_with_a_registered_name_gets_the_code_for_the_name_only(home, project, tmp_path):
    named = NOTES + "\nContact: %s of %s, see https://%s/portal and %s\n" % (
        fx.PERSON_FORMS[0], fx.CUSTOMER_FORMS[0], fx.CUSTOMER_DOMAIN, fx.ORG_FORMS[0])
    copy = _import(home, project, tmp_path, {"main.tf": TERRAFORM, "notes.md": named})
    for form in fx.PERSON_FORMS + fx.CUSTOMER_FORMS + fx.ORG_FORMS + [fx.CUSTOMER_DOMAIN]:
        assert form not in copy, "a registered form is replaced"
    assert fx.PERSON_CODE in copy and fx.CUSTOMER_CODE in copy
    for value in KEPT:
        if value != SCRIPT[0] and value not in HEADERS:
            assert value in copy.replace("\\", ""), value
    _assert_no_token(copy)


def test_a_mail_address_is_still_a_finding(home, project, tmp_path):
    copy = _import(home, project, tmp_path, {"notes.md": NOTES + "\nMail: ops.team@qvelmar-demo.net\n"})
    assert "ops.team@qvelmar-demo.net" not in copy and "MAIL-" in copy
    assert GATEWAY in copy and PUBLIC_IP in copy


def test_a_literal_password_and_a_private_key_are_still_secret(home, project, tmp_path):
    text = NOTES + '\npassword = "%s"\n%s\npm.environment.set("password", "%s");\n' % (password(), private_key(),
                                                                                      password())
    copy = _import(home, project, tmp_path, {"notes.md": text})
    assert password() not in copy and "MIIEowIBAAKCAQEAu3xq" not in copy and "PRIVATE KEY" not in copy.replace(
        "[private key]", "")
    assert copy.count("[secret]") >= 2 and "[private key]" in copy
    for value in KEPT:
        assert value in copy, value


def test_the_script_lines_that_only_read_or_set_a_variable_hold_no_secret(home, project, tmp_path):
    escaped = json.dumps({"event": [{"script": {"exec": list(SCRIPT) + list(HEADERS)}}]}, indent=1)
    copy = _import(home, project, tmp_path, {"collection.json": escaped})
    assert "[secret" not in copy
    for line in SCRIPT:
        assert line.replace('"', '\\"') in copy, line


def test_a_literal_value_in_a_script_line_is_a_secret(home, project, tmp_path):
    lines = ['pm.environment.set("password", "%s");' % password(), 'pm.globals.set("api_key", "%s")' % (
        "Ab3dEf6hIj9lMn" + "2pQr5tUv8xYz1BcDeFg")]
    copy = _import(home, project, tmp_path, {"collection.json": json.dumps({"exec": lines}, indent=1)})
    assert password() not in copy and "Ab3dEf6hIj9lMn" not in copy and copy.count("[secret]") == 2


def test_a_pasted_text_keeps_the_technical_values(home, project, capsys, monkeypatch):
    res = pastein.run(home, project.customer, NOTES, "mail")
    out = res.outputs[0].read_text(encoding="utf-8")
    for value in KEPT:
        assert value in out, value
    _assert_no_token(out)


def test_one_text_of_the_console_keeps_the_technical_values(home):
    clean = intake.wipe_text(NOTES, home)
    for value in KEPT:
        assert value in clean, value
    _assert_no_token(clean)


def test_the_entry_engine_keeps_what_the_laptop_clean_still_tokens():
    text = "gateway %s on %s, %s" % (GATEWAY, PUBLIC_IP, IPV6)
    entry = _Engine([], entry=True)
    entry.detect("t", text)
    assert entry.sanitize(text, "t")[0] == text
    assert entry.leaks(text) == []
    plain = _Engine([])
    plain.detect("t", text)
    clean, counts = plain.sanitize(text, "t")
    assert counts["url"] == 1 and counts["ip"] == 2 and "URL-" in clean and "IP-" in clean


def test_the_self_test_of_the_intake_runs_the_entry_cases():
    assert intake.selftest() == []
    assert all("https://" in c or "srv-app01" in c for c in intake.ENTRY_KEPT_CASES)


# --------------------------------------------------------------------------- the name rules


@pytest.mark.parametrize("text", [
    "Owner: VMs\nContact: IPs\nName: pre-shared-key\nUser: ECSs",
    "Dear VMs,\nregards IPs",
    "Prepared by: VPCs SGs",
    "Ansprechpartner: pre-shared",
])
def test_a_technical_word_is_never_taken_for_a_person_or_a_place(text):
    assert intake.unknown_candidates(text, [], rules=None) == []
    assert intake.unknown_candidates(text, []) == []


def test_a_listed_word_and_the_general_shapes_are_technical():
    for word in TECHNICAL + ("vCPUs", "NICs", "ACLs", "ECS", "web-01", "Pre-shared", "read-only"):
        assert wipe._technical_word(word), word
    for word in ("Tobias", "Beispielmann", "Anna-Lena", "anna-lena", "XQARVS", "Zyxwo", "Hans-Peter"):
        assert not wipe._technical_word(word), word
    assert intake.unknown_candidates("Owner: %s" % fx.PLANTED_PERSON, [], rules=None) == [fx.PLANTED_PERSON]


# --------------------------------------------------------------------------- the other doors of the entry


@pytest.fixture
def folder(home, monkeypatch):
    root = home.projects_root / "tcp-q7m4"
    (root / "input").mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope of tcp-q7m4\n\n- code: tcp-q7m4\n- customer: none\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8"))


def test_awb_material_add_keeps_the_technical_values(home, folder, monkeypatch, capsys):
    _stdin(monkeypatch, NOTES)
    assert cli.main(["material", "add"]) == 0
    (written,) = (folder / "input").iterdir()
    assert written.read_text(encoding="utf-8") == NOTES


def test_awb_material_add_still_refuses_a_name_a_mail_and_a_secret(home, folder, monkeypatch, capsys):
    for text in (NOTES + fx.PERSON_FORMS[0] + "\n", NOTES + "ops.team@qvelmar-demo.net\n",
                 NOTES + 'password = "%s"\n' % password()):
        _stdin(monkeypatch, text)
        assert cli.main(["material", "add"]) != 0
    assert list((folder / "input").iterdir()) == []


def test_awb_paste_keeps_addresses_and_web_addresses_as_written(home, folder, monkeypatch):
    path, counts, outside = paste.paste(NOTES, folder)
    assert path.read_text(encoding="utf-8").rstrip("\n") == NOTES.rstrip("\n")
    assert "ip" not in counts and "url" not in counts


def test_awb_paste_still_masks_a_phone_number(home, folder):
    path, counts, _ = paste.paste("call +49 170 1234567 about " + GATEWAY, folder)
    text = path.read_text(encoding="utf-8")
    assert GATEWAY in text and "1234567" not in text and counts.get("phone") == 1


def test_the_entry_check_leaves_out_addresses_and_urls_only(home):
    text = "%s %s ops.team@qvelmar-demo.net %s" % (GATEWAY, PUBLIC_IP, fx.PERSON_FORMS[0])
    every = {h["cls"] for h in check.check_text(text, home.register)}
    entry = {h["cls"] for h in check.check_text(text, home.register, entry=True)}
    assert {"url", "ip", "mail", "name"} <= every and entry == {"mail", "name"}
    assert {h["cls"] for h in hooks.name_hits(text, home)} == every
    assert {h["cls"] for h in hooks.name_hits(text, home, entry=True)} == entry
    assert check.screen(text, check.checker(home.register), allow=("url", "ip")).found == ("mail", "name")


def test_the_prompt_and_the_other_checks_read_every_class_as_before(home):
    assert {h["cls"] for h in check.check_text(GATEWAY + " " + PUBLIC_IP, home.register)} == {"url", "ip"}
    assert check.screen(GATEWAY, check.checker(home.register)).found == ("url",)


# --------------------------------------------------------------------------- the tool guard

OWN = "tcp-q7m4"
OTHER = "tcp-z3k5"


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    user = tmp_path / "userhome"
    for name in (".ssh", ".config", ".claude"):
        (user / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    own, other = home.projects_root / OWN, home.projects_root / OTHER
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "SCOPE.md").write_text("# Scope of %s\n" % root.name, encoding="utf-8")
        (root / "input").mkdir()
    return {"p": home, "own": own, "other": other, "user": user}


def _guarded(world, command):
    data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(world["own"]), "session_id": "s-1"}
    return tg.guard(data, world["p"], world["user"], sealed=True)


@pytest.mark.parametrize("command", [
    "grep -rn 'https://ecs.eu-de.otc.t-systems.com/v1' input/",
    "grep -rn https://ecs.eu-de.otc.t-systems.com/v1/ input/",
    "rg 'https?://[a-z.]+/v1/[a-z]+' input/",
    "grep -rEn '(https?://)?[a-z-]+\\.eu-de\\.otc/' input",
    "grep -rn '/v1/cloudservers' .",
    "grep -rn -e /v1/servers -e https://x.qvelmar-demo.net/a input/ notes",
    "rg --glob '*/v1/*' -e '^https?://[a-z]+/' input/",
    "sed -n 's/https:\\/\\/a\\/b/x/p' input/notes.md",
    "sed -n '/v1\\/servers/p' input/a.txt",
    "grep -c -- https://a.qvelmar-demo.net/v1 input/a.txt",
])
def test_the_pattern_of_a_search_is_no_path_to_another_project(world, command):
    assert _guarded(world, command) is None


def test_a_real_path_outside_the_project_is_still_refused(world):
    other = world["other"]
    for command in ("grep -rn /v1/x %s" % (other / "input"), "grep -rn pattern ../%s/input" % OTHER,
                    "grep -rn /v1/x ../%s" % OTHER, "rg -e /v1/x input/ %s" % other,
                    "grep -rn -f %s/SCOPE.md input/" % other, "sed -n '/v1/p' %s/SCOPE.md" % other,
                    "grep -rn 'https://a.qvelmar-demo.net/v1' input/ ../%s/input" % OTHER):
        assert _guarded(world, command) == tg.ANOTHER_PROJECT, command
