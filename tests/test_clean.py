"""`awb clean` on the laptop (task lc-laptop-clean): docs and logs offline, the laptop register and its mapping, the
report without values, --put into the project's in/ and `awb clean update`. Invented names only (tests/fixtures.py);
the secrets of the logs are built from pieces so that the commit gate finds none in this file."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from awb import clean, cli, laptop, obs, register
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

PROJECT = "tcp-q7m4"
CUSTOMER = fx.CUSTOMER_CODE
AK, SK = "AKLABFAKE", "sk-lab-fake-secret"
PAIR = " ".join(("Outcome", "Scorecard"))
PASSWORD = "Sup3r" + "S3cret" + "Value99x"
TOKEN = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def lap(tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    return tmp_path / "laptop"


def _reader(lines):
    it = iter(lines)
    return lambda: next(it, "")


def _register(forms=None):
    out = io.StringIO()
    n = laptop.ask_forms(CUSTOMER, read=_reader([f + "\n" for f in (forms or fx.CUSTOMER_FORMS)] + ["\n"]),
                         tty=True, say=lambda s: out.write(s + "\n"))
    return n, out.getvalue()


def _docx(path: Path) -> Path:
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.core_properties.author = fx.PERSON_FORMS[0]
    d.core_properties.title = "Offer for %s" % fx.CUSTOMER_FORMS[0]
    d.sections[0].header.paragraphs[0].text = "%s confidential, %s" % (fx.CUSTOMER_FORMS[0], fx.CONTROL_UNREGISTERED)
    d.sections[0].footer.paragraphs[0].text = "footer of %s" % fx.SECOND_COMPANY
    d.add_heading("Migration concept for %s" % fx.CUSTOMER_FORMS[1], 1)
    d.add_paragraph("Contact: %s, %s, +49 170 1234567." % (fx.PERSON_FORMS[0], "tobias.b@" + fx.CUSTOMER_DOMAIN))
    d.add_paragraph("the Kafka cluster runs on ECS with Terraform 1.9 and PostgreSQL 16. Herr %s from %s signs off."
                    % (fx.SECOND_PERSON, fx.PLANTED_CANDIDATE))
    d.add_paragraph("Admin pass" + "word: " + PASSWORD)
    d.save(path)
    return path


def _log_text() -> str:
    return "\n".join([
        "Oct 22 10:01:02 %s sshd[1234]: Failed password for invalid user %s from %s port 22 ssh2"
        % (fx.LOG_HOST, fx.LOG_USER, fx.LOG_PUBLIC_IP),
        "2026-10-22T10:02:00Z %s java[99]: ERROR org.apache.kafka.clients.NetworkClient Connection to node 3 "
        "(%s/%s:9092) could not be established. Broker may not be available." % (fx.LOG_INTERNAL_HOST,
                                                                                  fx.LOG_INTERNAL_HOST,
                                                                                  fx.LOG_PRIVATE_IP),
        "user=%s host=%s mail=%s" % (fx.LOG_USER, fx.LOG_HOST, "tobias.b@" + fx.CUSTOMER_DOMAIN),
        "DB_PASS" + "WORD=" + PASSWORD + " api_key: \"" + TOKEN + "\"",
        "NullPointerException at com.example.app.Main.run(Main.java:42) version 1.2.3.4 on CCE 1.29 with KVM, "
        "%s %s %s" % (fx.SECOND_LAST, PAIR, fx.CUSTOMER_FORMS[1]),
        "dns 100.125.1.250 netmask 255.255.255.0 on 127.0.0.1 via https://github.com/org/repo",
        "",
    ])


def test_register_asks_the_forms_once_echoes_none_and_keeps_them_on_the_laptop(lap):
    n, said = _register()
    assert n == len(fx.CUSTOMER_FORMS)
    fx.assert_no_fixture_name(said, "the prompt")
    path = laptop.register_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert {e.form for e in register.load(path)} == set(fx.CUSTOMER_FORMS)
    assert laptop.known(CUSTOMER)
    with pytest.raises(laptop.LaptopError, match="own terminal"):
        laptop.ask_forms(CUSTOMER, read=_reader([]), tty=False)


def test_a_customer_without_a_form_on_the_laptop_is_refused_with_the_register_line(lap, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("ok\n", encoding="utf-8")
    with pytest.raises(clean.CleanError, match="awb clean register %s" % CUSTOMER):
        clean.run([f], PROJECT, "logs", tmp_path / "out")
    laptop.ask_forms(CUSTOMER, read=_reader(["\n"]), tty=True, say=lambda s: None)
    assert laptop.known(CUSTOMER)          # registered without a form
    assert clean.run([f], PROJECT, "logs", tmp_path / "out").outputs


def test_docs_keep_the_body_and_drop_logos_properties_headers_footers_and_names(lap, tmp_path):
    _register()
    src = _docx(tmp_path / ("Angebot %s.docx" % fx.CUSTOMER_FORMS[1]))
    before = src.read_bytes()
    res = clean.run([src], PROJECT, "docs", tmp_path / "out")
    assert src.read_bytes() == before                      # the original is never changed
    (out,) = res.outputs
    assert out.name.endswith(".clean.md") and CUSTOMER in out.name
    text = out.read_text(encoding="utf-8")
    fx.assert_no_fixture_name(text, "the clean copy")
    for value in (fx.SECOND_PERSON, fx.SECOND_LAST, fx.PLANTED_CANDIDATE, fx.CONTROL_UNREGISTERED, "confidential",
                  "footer of", "Offer for", "1234567", PASSWORD, fx.CUSTOMER_DOMAIN):
        assert value not in text, value
    assert CUSTOMER in text and "[person" in text and "[secret]" in text
    for word in ("Kafka", "ECS", "Terraform 1.9", "PostgreSQL 16", "Migration concept"):
        assert word in text, word
    fr = res.files[0]
    assert fr.counts["person"] >= 1 and fr.counts["mail"] == 1 and fr.counts["phone"] == 1
    assert fr.removed["header and footer parts"] >= 1 and fr.removed["document properties"] >= 1
    report = res.report.read_text(encoding="utf-8")
    fx.assert_no_fixture_name(report, "the report")
    assert fx.SECOND_LAST not in report and PASSWORD not in report and "| %s | docx | clean |" % out.name in report
    mapping = res.mapping
    assert mapping.is_relative_to(laptop.vault()) and stat.S_IMODE(mapping.stat().st_mode) == 0o600
    assert fx.SECOND_LAST in mapping.read_text(encoding="utf-8")
    assert PASSWORD not in mapping.read_text(encoding="utf-8")   # a secret is never kept, not even in the vault


def test_logs_wipe_users_hosts_addresses_mail_and_secrets_and_keep_the_technical_words(lap, tmp_path):
    _register()
    src = tmp_path / "app.log"
    src.write_text(_log_text(), encoding="utf-8")
    res = clean.run([src], PROJECT, "logs", tmp_path / "out")
    (out,) = res.outputs
    assert out.name == "app.clean.log"
    text = out.read_text(encoding="utf-8")
    for value in (fx.LOG_USER, fx.LOG_HOST, fx.LOG_INTERNAL_HOST, fx.LOG_PUBLIC_IP, fx.LOG_PRIVATE_IP, PASSWORD,
                  TOKEN, fx.CUSTOMER_DOMAIN, fx.CUSTOMER_FORMS[1]):
        assert value not in text, value
    for token in ("[user 1]", "[host 1]", "[ip 1]", "[secret]", "[mail 1]", CUSTOMER):
        assert token in text, token
    for word in ("sshd[1234]: Failed password for invalid user", "org.apache.kafka.clients.NetworkClient",
                 "com.example.app.Main.run(Main.java:42)", "version 1.2.3.4", "CCE 1.29", "KVM", PAIR,
                 fx.SECOND_LAST, "100.125.1.250", "255.255.255.0", "127.0.0.1", "https://github.com/org/repo",
                 "DB_PASS" + "WORD=", "api_key:"):
        assert word in text, word
    # one token per value: the host of two lines is one host
    assert text.count("[host 2]") >= 2
    fr = res.files[0]
    assert fr.counts["user"] >= 2 and fr.counts["ip"] >= 2 and fr.counts["secret"] >= 2
    report = res.report.read_text(encoding="utf-8")
    for value in (fx.LOG_USER, fx.LOG_HOST, fx.LOG_PUBLIC_IP, PASSWORD):
        assert value not in report


def test_logs_refuse_a_binary_and_a_document_and_say_why_in_the_report(lap, tmp_path):
    _register()
    b = tmp_path / "dump.log"
    b.write_bytes(b"\x00\x01binary")
    d = _docx(tmp_path / "x.docx")
    res = clean.run([b, d], PROJECT, "logs", tmp_path / "out")
    assert not res.outputs and [f.state for f in res.files] == ["skipped", "skipped"]
    assert "not text" in res.files[0].why and "--kind docs" in res.files[1].why


def test_nothing_in_the_laptop_path_opens_a_network_connection(lap, tmp_path, monkeypatch):
    """Socket creation raises: register, init, docs and logs still run to the end."""
    def refuse(*a, **k):
        raise AssertionError("a network connection was opened")

    src_doc = _docx(tmp_path / "d.docx")
    src_log = tmp_path / "a.log"
    src_log.write_text(_log_text(), encoding="utf-8")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    laptop.init()
    _register()
    assert clean.run([src_doc], PROJECT, "docs", tmp_path / "o1").outputs
    assert clean.run([src_log], PROJECT, "logs", tmp_path / "o2").outputs
    assert cli.main(["clean", str(src_log), "--project", PROJECT, "--kind", "logs", "--out", str(tmp_path / "o3")]) == 0


def test_pdf_is_read_as_a_document(lap, tmp_path):
    pytest.importorskip("reportlab")
    if not shutil.which("pdftotext"):
        pytest.skip("the poppler tools are not installed")
    from reportlab.pdfgen import canvas

    _register()
    f = tmp_path / "brief.pdf"
    c = canvas.Canvas(str(f))
    c.setAuthor(fx.PERSON_FORMS[0])
    c.drawString(72, 720, "Brief for %s on Kubernetes" % fx.CUSTOMER_FORMS[0])
    c.save()
    (out,) = clean.run([f], PROJECT, "docs", tmp_path / "out").outputs
    text = out.read_text(encoding="utf-8")
    fx.assert_no_fixture_name(text, "the pdf copy")
    assert CUSTOMER in text and "Kubernetes" in text


def test_the_clean_name_is_what_the_host_takes_with_trusted():
    for name in ("a.clean.md", "a.clean.log", "a.clean (2).md", "x-y.clean.yaml"):
        assert clean.is_clean_name(name), name
    for name in ("a.md", "a.clean.docx", "clean.md", "a.cleaner.md"):
        assert not clean.is_clean_name(name), name


@pytest.fixture
def own():
    with FakeOBS("awb", AK, SK) as o:
        yield o


def _bucket(own):
    return obs.Client(own.bucket, obs.Keys(AK, SK), endpoint=own.endpoint)


def test_put_finds_the_folder_under_its_month_and_puts_only_clean_copies(lap, tmp_path, own):
    _register()
    src = tmp_path / "a.log"
    src.write_text(_log_text(), encoding="utf-8")
    own.objects["2026-09/%s/" % PROJECT] = b""
    own.objects["2026-09/%s/in/" % PROJECT] = b""
    res = clean.run([src], PROJECT, "logs", tmp_path / "out")
    keys = clean.put_files(PROJECT, res.outputs, client=_bucket(own), say=lambda s: None)
    assert keys == ["2026-09/%s/in/a.clean.log" % PROJECT]
    assert own.objects[keys[0]] == res.outputs[0].read_bytes()
    assert not [k for k in own.objects if k.endswith("a.log")]
    said = []
    assert clean.put_files(PROJECT, res.outputs, client=_bucket(own), say=said.append) == []
    assert any("not replaced" in s for s in said)
    with pytest.raises(clean.CleanError, match="clean copies"):
        clean.put_files(PROJECT, [src], client=_bucket(own))


def test_put_creates_the_folder_under_this_month_and_needs_his_key_from_the_environment(lap, tmp_path, own,
                                                                                         monkeypatch):
    import datetime

    _register()
    src = tmp_path / "a.log"
    src.write_text("ok\n", encoding="utf-8")
    (out,) = clean.run([src], PROJECT, "logs", tmp_path / "out").outputs
    with pytest.raises(clean.CleanError, match="AWB_OBS_AK and AWB_OBS_SK"):
        clean.put_files(PROJECT, [out])
    keys = clean.put_files(PROJECT, [out], client=_bucket(own), today=datetime.date(2026, 10, 22),
                           say=lambda s: None)
    base = "2026-10/%s/" % PROJECT
    assert {base, base + "in/", base + "out/"} <= set(own.objects) and keys == [base + "in/a.clean.log"]
    monkeypatch.setenv("AWB_OBS_AK", AK)
    monkeypatch.setenv("AWB_OBS_SK", SK)
    c = clean._client()
    assert c.bucket == "awb" and c.region == "eu-de"


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org"))


def test_update_pulls_the_clone_and_prints_the_counts_that_changed(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("no git")
    upstream = tmp_path / "upstream"
    (upstream / "rules").mkdir(parents=True)
    (upstream / "rules" / "common-words.txt").write_text("# words\nalpha\nbeta\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", str(upstream))
    _git("add", "-A", cwd=upstream)
    _git("commit", "-q", "-m", "one", cwd=upstream)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(upstream), str(clone))
    (upstream / "rules" / "common-words.txt").write_text("# words\nalpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    (upstream / "rules" / "tech-terms.txt").write_text("kvm\n", encoding="utf-8")
    _git("add", "-A", cwd=upstream)
    _git("commit", "-q", "-m", "two", cwd=upstream)
    monkeypatch.setattr(clean, "PUBLIC_REPO", str(upstream))
    lines = clean.update(clone / "rules")
    assert "common-words.txt: 2 -> 4 entries (2 added, 0 removed)" in lines
    assert "tech-terms.txt: 0 -> 1 entries (1 added, 0 removed)" in lines
    assert clean.update(clone / "rules")[-1].endswith("nothing changed")
    (clone / "rules" / "common-words.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(clean.CleanError, match="local changes"):
        clean.update(clone / "rules")


def test_update_without_git_downloads_the_lists_and_checks_them_against_the_tree(tmp_path):
    rules = tmp_path / "pkg" / "rules"
    rules.mkdir(parents=True)
    (rules / "allowed-terms.txt").write_text("old\n", encoding="utf-8")
    fresh = b"old\nnew\n"
    asked = []

    def fetch(url, limit=0):
        asked.append(url)
        if url == clean.TREE_URL:
            return json.dumps({"tree": [{"path": "rules/allowed-terms.txt", "sha": clean.git_blob_id(fresh)}]}).encode()
        return fresh

    lines = clean.update(rules, fetch=fetch)
    assert "allowed-terms.txt: 1 -> 2 entries (1 added, 0 removed)" in lines
    assert (rules / "allowed-terms.txt").read_bytes() == fresh
    assert asked == [clean.TREE_URL, clean.RAW_URL % "allowed-terms.txt"]   # fixed addresses, no word of a file

    def tampered(url, limit=0):
        if url == clean.TREE_URL:
            return json.dumps({"tree": [{"path": "rules/allowed-terms.txt", "sha": "0" * 40}]}).encode()
        return b"evil\n"

    with pytest.raises(clean.CleanError, match="checksum"):
        clean.update(rules, fetch=tampered)
    assert (rules / "allowed-terms.txt").read_bytes() == fresh


def test_the_commands_are_wired(lap, tmp_path, capsys):
    assert cli.main(["init", "--laptop"]) == 0
    assert "laptop: %s" % lap in capsys.readouterr().out
    assert cli.main(["clean", "register", CUSTOMER]) == 2        # no terminal here
    assert "own terminal" in capsys.readouterr().err
    f = tmp_path / "a.log"
    f.write_text("ok\n", encoding="utf-8")
    assert cli.main(["clean", str(f), "--project", PROJECT, "--kind", "logs"]) == 2
    assert "awb clean register" in capsys.readouterr().err
    assert cli.main(["clean", str(f), "--project", PROJECT]) == 2


def test_logs_keep_code_names_and_stack_traces_and_wipe_the_people_in_them(lap, tmp_path):
    """The false positives of a first sample (2026-10-22): a Java class after a host-shaped head, a Terraform
    address, a Kafka logger, an HTTP verb after a colon; and what that sample still let through: a DN, an
    environment user, the flags of a mysql call and the display name in front of a mail address."""
    _register()
    pw = "S3cr3t" + "Passw0rdX"
    src = tmp_path / "trace.log"
    src.write_text("\n".join([
        "java.net.ConnectException: Connection refused (Connection refused)",
        "\tat com.zaxxer.hikari.pool.HikariPool.createPoolEntry(HikariPool.java:476)",
        "Error: creating ECS instance: Get \"https://ecs.eu-de.otc.t-systems.com/v1/cloudservers\": i/o timeout",
        "  with opentelekomcloud_compute_instance_v2.web[0],",
        "WARN kafka.server.ReplicaFetcherThread: Error sending fetch request to node 2",
        "ldap_bind: Invalid credentials (49) for DN cn=%s,ou=People,dc=example,dc=local" % fx.LOG_USER,
        "ticket opened by %s <%s>, priority P2" % (fx.SECOND_PERSON, "u.q@" + fx.CUSTOMER_DOMAIN),
        "mysql -h %s -u %s -p%s app" % (fx.LOG_INTERNAL_HOST, fx.LOG_USER, pw),
        "export OS_USERNAME=%s OS_PROJECT_NAME=eu-de_demo" % fx.LOG_USER,
        "Oct 21 08:15:02 %s systemd[1]: new session 42 of user %s." % (fx.LOG_HOST, fx.LOG_USER),
        "Oct 21 08:15:03 %s CRON[2201]: pam_unix(cron:session): session opened for user root(uid=0) by (uid=0)"
        % fx.LOG_HOST,
        ""]), encoding="utf-8")
    (out,) = clean.run([src], PROJECT, "logs", tmp_path / "out").outputs
    text = out.read_text(encoding="utf-8")
    for kept in ("java.net.ConnectException", "com.zaxxer.hikari.pool.HikariPool.createPoolEntry(HikariPool.java:476)",
                 "instance: Get \"https://ecs.eu-de.otc.t-systems.com/v1/cloudservers\"",
                 "opentelekomcloud_compute_instance_v2.web[0]", "kafka.server.ReplicaFetcherThread",
                 "session opened for user root(uid=0)", "OS_PROJECT_NAME=eu-de_demo", "session 42 of user [user 1]."):
        assert kept in text, kept
    for value in (fx.LOG_USER, fx.SECOND_PERSON, fx.LOG_INTERNAL_HOST, fx.LOG_HOST, pw):
        assert value not in text, value
    assert "cn=[user 1]" in text and "-u [user 1] -p[secret]" in text and "[person 1] <[mail 1]>" in text


def test_a_secret_field_with_a_plain_word_is_no_secret(lap, tmp_path):
    _register()
    pw = "Hk38" + "skd9LmQp2"
    src = tmp_path / "rules.log"
    src.write_text("Pass" + "word: must have 12 characters\n" + "tok" + "en: expired\n" + "OS_PASS" + "WORD='" + pw + "'\n",
                   encoding="utf-8")
    (out,) = clean.run([src], PROJECT, "logs", tmp_path / "out").outputs
    text = out.read_text(encoding="utf-8")
    assert "must have 12 characters" in text and "token: expired" in text
    assert pw not in text and "[secret]" in text
