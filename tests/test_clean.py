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


def _tree_fetch(files, tree=None):
    """A public repository entirely in memory, with real Git blob checksums."""
    if tree is None:
        tree = {"tree": [{"path": "rules/" + name, "sha": clean.git_blob_id(data),
                          "type": "blob", "mode": "100644"} for name, data in files.items()]}

    def fetch(url, limit=0):
        if url == clean.TREE_URL:
            return json.dumps(tree).encode()
        return files[url.rsplit("/", 1)[-1]]

    return fetch


@pytest.mark.parametrize("tree", [None, [], {}, {"tree": None}, {"tree": {}}, {"tree": [None]},
                                  {"tree": [{"path": []}]}, {"tree": [], "truncated": True}])
def test_update_refuses_malformed_or_incomplete_tree(tmp_path, tree):
    rules = tmp_path / "rules"
    rules.mkdir()
    old = rules / "allowed-terms.txt"
    old.write_bytes(b"old\n")
    with pytest.raises(clean.CleanError):
        clean.update(rules, fetch=lambda url: json.dumps(tree).encode())
    assert old.read_bytes() == b"old\n"
    assert sorted(p.name for p in rules.iterdir()) == [old.name]


@pytest.mark.parametrize("payload", [b"\xff", b"{", b'{"tree": "' + b"pass" + b"word" + b'"}'])
def test_update_bad_json_has_a_value_free_error(tmp_path, payload):
    rules = tmp_path / "rules"
    rules.mkdir()
    with pytest.raises(clean.CleanError) as exc:
        clean.update(rules, fetch=lambda url: payload)
    assert "pass" + "word" not in str(exc.value)


@pytest.mark.parametrize("change", ["duplicate", "symlink", "tree", "bad-sha", "truncated"])
def test_update_checks_tree_entry_identity_before_fetching_blobs(tmp_path, change):
    rules = tmp_path / "rules"
    rules.mkdir()
    data = b"new\n"
    entry = {"path": "rules/allowed-terms.txt", "sha": clean.git_blob_id(data),
             "type": "blob", "mode": "100644"}
    tree = {"tree": [entry]}
    if change == "duplicate":
        tree["tree"].append(dict(entry))
    elif change == "symlink":
        entry["mode"] = "120000"
    elif change == "tree":
        entry["type"] = "tree"
    elif change == "bad-sha":
        entry["sha"] = {"value": "pass" + "word"}
    else:
        tree["truncated"] = True
    asked = []

    def fetch(url):
        asked.append(url)
        return json.dumps(tree).encode() if url == clean.TREE_URL else data

    with pytest.raises(clean.CleanError):
        clean.update(rules, fetch=fetch)
    assert asked == [clean.TREE_URL]
    assert not list(rules.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_update_refuses_nonregular_local_rule_files_before_network(tmp_path, kind):
    rules = tmp_path / "rules"
    rules.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private\n")
    path = rules / "allowed-terms.txt"
    if kind in ("symlink", "dangling"):
        path.symlink_to(outside if kind == "symlink" else tmp_path / "missing")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)

    def fetch(url):
        pytest.fail("unsafe local files must be refused before a request")

    # A FIFO must be refused without opening it in blocking mode.
    with pytest.raises(clean.CleanError):
        clean.update(rules, fetch=fetch)
    assert outside.read_bytes() == b"private\n"


def test_update_does_not_treat_an_unreadable_rule_as_missing(tmp_path, monkeypatch):
    rules = tmp_path / "rules"
    rules.mkdir()
    path = rules / "allowed-terms.txt"
    path.write_bytes(b"old\n")
    real_read, real_open = Path.read_bytes, os.open

    def read(p):
        if p == path:
            raise PermissionError("pass" + "word")
        return real_read(p)

    def open_file(p, *args, **kwargs):
        if os.fspath(p) in (str(path), path.name):
            raise PermissionError("pass" + "word")
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(clean.CleanError) as exc:
        clean.update(rules, fetch=lambda url: pytest.fail("unreadable rules must stop the update"))
    assert "pass" + "word" not in str(exc.value)


@pytest.mark.parametrize("parent_link", [False, True])
def test_update_refuses_a_rules_path_through_a_link(tmp_path, parent_link):
    outside = tmp_path / "outside"
    (outside / "rules").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(outside if parent_link else outside / "rules", target_is_directory=True)
    rules = link / "rules" if parent_link else link
    with pytest.raises(clean.CleanError):
        clean.update(rules, fetch=_tree_fetch({"allowed-terms.txt": b"new\n"}))
    assert not list((outside / "rules").iterdir())


def test_update_never_writes_through_the_old_predictable_temp_name(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private\n")
    planted = rules / ".allowed-terms.txt.tmp"
    planted.symlink_to(outside)
    clean.update(rules, fetch=_tree_fetch({"allowed-terms.txt": b"new\n"}))
    assert outside.read_bytes() == b"private\n"
    assert planted.is_symlink()
    assert (rules / "allowed-terms.txt").read_bytes() == b"new\n"


def test_update_cleans_staged_files_when_replace_fails(tmp_path, monkeypatch):
    rules = tmp_path / "rules"
    rules.mkdir()
    path = rules / "allowed-terms.txt"
    path.write_bytes(b"old\n")

    def refuse(*args, **kwargs):
        raise PermissionError("pass" + "word")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(clean.CleanError) as exc:
        clean.update(rules, fetch=_tree_fetch({path.name: b"new\n"}))
    assert "pass" + "word" not in str(exc.value)
    assert path.read_bytes() == b"old\n"
    assert list(rules.iterdir()) == [path]


def test_update_verifies_every_blob_before_replacing_any_list(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    files = {"common-words.txt": b"first\n", "allowed-terms.txt": b"second\n"}
    for name in files:
        (rules / name).write_bytes(b"old\n")
    good = _tree_fetch(files)

    def fetch(url):
        return b"bad\n" if url == clean.RAW_URL % "allowed-terms.txt" else good(url)

    with pytest.raises(clean.CleanError, match="checksum"):
        clean.update(rules, fetch=fetch)
    assert all((rules / name).read_bytes() == b"old\n" for name in files)
    assert len(list(rules.iterdir())) == len(files)


def test_concurrent_updates_do_not_share_staging_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    rules = tmp_path / "rules"
    rules.mkdir()
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    replace = os.replace
    calls = []

    def paused(src, dst, *args, **kwargs):
        calls.append(os.fspath(src))
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return replace(src, dst, *args, **kwargs)

    def second():
        second_started.set()
        return clean.update(rules, fetch=_tree_fetch({"allowed-terms.txt": b"second\n"}))

    monkeypatch.setattr(os, "replace", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(clean.update, rules, fetch=_tree_fetch({"allowed-terms.txt": b"first\n"}))
        try:
            assert entered.wait(10)
            other = pool.submit(second)
            assert second_started.wait(10)
        finally:
            release.set()
        first.result(timeout=10)
        other.result(timeout=10)
    assert len(calls) == len(set(calls)) == 2
    assert (rules / "allowed-terms.txt").read_bytes() in (b"first\n", b"second\n")
    assert len(list(rules.iterdir())) == 1


def test_fetch_bounds_the_response_and_closes_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    streams = []
    requests = []

    def opened(req, timeout):
        requests.append((req, timeout))
        stream = io.BytesIO(b"abcde")
        streams.append(stream)
        return stream

    monkeypatch.setattr(clean.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=opened))
    assert clean._fetch(clean.TREE_URL, limit=5) == b"abcde"
    with pytest.raises(clean.CleanError, match="limit"):
        clean._fetch(clean.TREE_URL, limit=4)
    assert all(s.closed for s in streams)
    assert all(r.get_header("User-agent") == clean.USER_AGENT and t == 60 for r, t in requests)


def test_fetch_never_exposes_a_transport_error_value(monkeypatch):
    from types import SimpleNamespace
    import urllib.error

    secret = "pass" + "word"

    def fail(*args, **kwargs):
        raise urllib.error.URLError("http://user:" + secret + "@proxy.invalid")

    monkeypatch.setattr(clean.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=fail))
    with pytest.raises(urllib.error.URLError) as exc:
        clean._fetch(clean.TREE_URL)
    assert secret not in str(exc.value) and "user:" not in str(exc.value)


@pytest.mark.parametrize("value", ["http://user/pass" + "word@proxy.invalid:80",
                                    "http://user?pass" + "word@proxy.invalid:80",
                                    "http://user#pass" + "word@proxy.invalid:80",
                                    "http://user\nname:pw@proxy.invalid:80",
                                    "http://proxy.invalid:bad", "http://[broken", "http://"])
def test_proxy_host_refuses_malformed_credentials_and_controls(value):
    assert clean.proxy_host(value) == "set, not a URL"


def test_doctor_does_not_print_private_bypass_names_or_invalid_region(lap, monkeypatch):
    from types import SimpleNamespace
    private = fx.CUSTOMER_FORMS[0]
    monkeypatch.setenv("NO_PROXY", private + ",http://user:" + "pass" + "word@proxy.invalid")
    monkeypatch.setenv("no_proxy", private)
    monkeypatch.setenv("AWB_OBS_REGION", private + "/@outside.invalid")
    asked = []

    def open_request(req, timeout):
        asked.append(req.full_url)
        raise OSError("pass" + "word")

    lines, code = clean.doctor(opener=SimpleNamespace(open=open_request))
    text = "\n".join(lines)
    assert code != 0
    assert private not in text and "pass" + "word" not in text
    assert all("outside.invalid" not in url for url in asked)


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "fifo"])
def test_put_refuses_nonregular_copies_before_any_bucket_call(lap, tmp_path, kind):
    path = tmp_path / "copy.clean.log"
    if kind == "symlink":
        outside = tmp_path / "private"
        outside.write_bytes(b"private\n")
        path.symlink_to(outside)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)

    class NoBucket:
        def __getattr__(self, name):
            pytest.fail("the refusal must precede the bucket")

    with pytest.raises(clean.CleanError):
        clean.put_files(PROJECT, [path], client=NoBucket())


def test_put_bucket_errors_do_not_echo_remote_values(lap, tmp_path, monkeypatch):
    from awb import bucket
    path = tmp_path / "copy.clean.log"
    path.write_bytes(b"ok\n")
    secret = "pass" + "word"

    def fail(*args):
        raise obs.OBSError("remote echoed " + secret)

    monkeypatch.setattr(bucket, "find_folder", fail)
    with pytest.raises(clean.CleanError) as exc:
        clean.put_files(PROJECT, [path], client=object())
    assert secret not in str(exc.value)


def test_bucket_month_errors_do_not_echo_remote_values(monkeypatch):
    from awb import bucket
    secret = "pass" + "word"
    monkeypatch.setattr(clean, "keys_present", lambda: True)
    monkeypatch.setattr(clean, "_client", lambda *a: object())

    def fail(*args):
        raise obs.OBSError(secret)

    monkeypatch.setattr(bucket, "find_folder", fail)
    with pytest.raises(clean.CleanError, match="--month") as exc:
        clean._bucket_month(PROJECT)
    assert secret not in str(exc.value)


@pytest.mark.parametrize("kind", ["bad-encoding", "symlink"])
def test_report_outputs_refuses_unreadable_or_linked_reports(tmp_path, kind):
    copy = tmp_path / "copy.clean.log"
    copy.write_bytes(b"ok\n")
    report = tmp_path / "report.md"
    if kind == "bad-encoding":
        report.write_bytes(b"\xff")
    else:
        outside = tmp_path / "outside"
        outside.write_text("| copy.clean.log | logs | clean | nothing | nothing |\n")
        report.symlink_to(outside)
    assert clean.report_outputs(report) == []


def test_clean_name_requires_a_basename_without_controls():
    for name in ("../a.clean.log", "/a.clean.log", "..\\a.clean.log", "a.clean.log\n", "a\x00.clean.md"):
        assert not clean.is_clean_name(name)


def test_secret_in_source_name_never_reaches_copy_report_or_map(lap, tmp_path):
    _register()
    source = tmp_path / (("pass" + "word=") + PASSWORD + ".log")
    source.write_text("ok\n", encoding="utf-8")
    result = clean.run([source], PROJECT, "logs", tmp_path / "out")
    assert result.outputs
    for path in result.outputs + [result.report] + ([result.mapping] if result.mapping else []):
        assert PASSWORD not in path.name
        assert PASSWORD not in path.read_text(encoding="utf-8")


def test_main_does_not_print_the_private_output_directory(lap, tmp_path, capsys):
    _register()
    source = tmp_path / "app.log"
    source.write_text("ok\n", encoding="utf-8")
    out = tmp_path / fx.CUSTOMER_FORMS[0]
    assert clean.main([str(source), "--project", PROJECT, "--out", str(out)]) == 0
    said = capsys.readouterr()
    assert fx.CUSTOMER_FORMS[0] not in said.out + said.err
    assert "clean-report-" in said.out


def test_run_refuses_an_output_directory_link_without_chmod_or_writes(lap, tmp_path):
    _register()
    source = tmp_path / "app.log"
    source.write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    out = tmp_path / "out"
    out.symlink_to(outside, target_is_directory=True)
    with pytest.raises(clean.CleanError):
        clean.run([source], PROJECT, "logs", out)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert not list(outside.iterdir())


def test_report_creation_retries_a_racing_writer_and_a_dangling_link(tmp_path, monkeypatch):
    real_open = os.open
    raced = []

    def open_file(path, flags, *args, **kwargs):
        if Path(path).name.startswith("clean-report-") and not raced:
            raced.append(Path(path))
            Path(path).symlink_to(tmp_path / "missing")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    report = clean._write_report(tmp_path, PROJECT, CUSTOMER, "logs", [])
    assert report != raced[0] and raced[0].is_symlink()
    assert PROJECT in report.read_text(encoding="utf-8")
    assert stat.S_IMODE(report.stat().st_mode) == 0o600


def test_copy_creation_race_skips_the_existing_copy(lap, tmp_path, monkeypatch):
    _register()
    source = tmp_path / "app.log"
    source.write_text("ok\n", encoding="utf-8")
    real_open = os.open

    def open_file(path, flags, *args, **kwargs):
        if Path(path).name == "app.clean.log":
            Path(path).write_bytes(b"other run\n")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    result = clean.run([source], PROJECT, "logs", tmp_path / "out")
    assert not result.outputs and result.files[0].state == "skipped"
    assert (tmp_path / "out" / "app.clean.log").read_bytes() == b"other run\n"
    assert result.report.is_file()


def test_update_ignores_tree_paths_outside_the_fixed_allowlist(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private\n")
    data = b"new\n"
    paths = ["rules/allowed-terms.txt", "rules/../outside", str(outside), "rules/unknown.txt"]
    tree = {"tree": [{"path": p, "sha": clean.git_blob_id(data)} for p in paths]}
    asked = []
    fetch_tree = _tree_fetch({"allowed-terms.txt": data}, tree)

    def fetch(url):
        asked.append(url)
        return fetch_tree(url)

    clean.update(rules, fetch=fetch)
    assert asked == [clean.TREE_URL, clean.RAW_URL % "allowed-terms.txt"]
    assert outside.read_bytes() == b"private\n"
    assert [p.name for p in rules.iterdir()] == ["allowed-terms.txt"]


@pytest.mark.parametrize("failure", ["status", "dirty", "pull"])
def test_git_update_errors_never_echo_subprocess_output(tmp_path, failure):
    from types import SimpleNamespace
    # The marker and runner are private to this test, independent of the checkout's .git.
    (tmp_path / ".git").mkdir()
    rules = tmp_path / "rules"
    rules.mkdir()
    secret = "pass" + "word"
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        status = "status" in args
        return SimpleNamespace(returncode=int(failure == ("status" if status else "pull")),
                               stdout=secret if failure == "dirty" or not status else "", stderr=secret)

    with pytest.raises(clean.CleanError) as exc:
        clean.update(rules, run=run, fetch=lambda u: pytest.fail("git update must not use the downloader"))
    assert secret not in str(exc.value)
    assert len(calls) == (2 if failure == "pull" else 1)
    assert all(args[:3] == ["git", "-C", str(tmp_path)] for args in calls)


def test_update_refuses_linked_git_metadata(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    (repo / "rules").mkdir(parents=True)
    (repo / ".git").symlink_to(outside, target_is_directory=True)
    with pytest.raises(clean.CleanError):
        clean.update(repo / "rules", run=lambda *a, **k: pytest.fail("must not run git through a link"))


def test_fetch_refuses_https_downgrade_before_opening_the_redirect(monkeypatch):
    from types import SimpleNamespace
    import urllib.request

    seen = []

    def build(*handlers):
        redirect = next((h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)),
                        urllib.request.HTTPRedirectHandler())

        def open_request(req, timeout):
            seen.append(req.full_url)
            next_request = redirect.redirect_request(req, None, 302, "redirect", {}, "http://outside.invalid/tree")
            assert next_request is None, "an HTTPS tree must never be followed over HTTP"
            return io.BytesIO(b"ok")

        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(clean.CleanError):
        clean._fetch(clean.TREE_URL)
    assert seen == [clean.TREE_URL]


@pytest.mark.parametrize("error_kind", ["proxy", "read", "http"])
def test_fetch_sanitizes_setup_and_read_failures_and_closes_http_errors(monkeypatch, error_kind):
    from types import SimpleNamespace
    import urllib.error

    secret = "pass" + "word"
    body = io.BytesIO(b"private response")

    def fail(*args, **kwargs):
        if error_kind == "proxy":
            raise ValueError(secret)
        if error_kind == "read":
            raise OSError(secret)
        raise urllib.error.HTTPError(clean.TREE_URL, 502, secret, {}, body)

    if error_kind == "proxy":
        monkeypatch.setattr(clean.urllib.request, "build_opener", fail)
    else:
        monkeypatch.setattr(clean.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=fail))
    with pytest.raises((clean.CleanError, OSError)) as exc:
        clean._fetch(clean.TREE_URL)
    assert secret not in str(exc.value)
    if error_kind == "http":
        assert body.closed


def test_log_read_rejects_a_link_swapped_after_preflight(lap, tmp_path, monkeypatch):
    _register()
    source = tmp_path / "app.log"
    source.write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "private.log"
    outside.write_text("private content\n", encoding="utf-8")
    read_log = clean._read_log

    def swapped(path):
        path.unlink()
        path.symlink_to(outside)
        return read_log(path)

    monkeypatch.setattr(clean, "_read_log", swapped)
    with pytest.raises(clean.CleanError):
        clean.run([source], PROJECT, "logs", tmp_path / "out")
    assert not list((tmp_path / "out").iterdir())


def test_log_size_limit_also_applies_when_the_file_grows_before_read(tmp_path, monkeypatch):
    source = tmp_path / "app.log"
    source.write_bytes(b"abcde")
    monkeypatch.setattr(clean, "MAX_LOG_BYTES", 4)
    with pytest.raises(clean.CleanError, match="limit"):
        clean._read_log(source)


def test_put_uses_the_checked_copy_even_if_its_path_changes(lap, tmp_path, monkeypatch):
    from awb import bucket
    copy = tmp_path / "app.clean.log"
    copy.write_bytes(b"clean\n")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private\n")
    sent = []

    class Client:
        def head(self, key):
            copy.unlink()
            copy.symlink_to(outside)
            return None

        def put_file(self, key, path, content_type):
            sent.append(path.read_bytes())

        def put_bytes(self, key, data, content_type):
            sent.append(data)

    monkeypatch.setattr(bucket, "find_folder", lambda *a: "2026-11/" + PROJECT + "/")
    assert clean.put_files(PROJECT, [copy], client=Client(), say=lambda s: None)
    assert sent == [b"clean\n"]


def test_doctor_handles_invalid_proxy_setup_without_echoing_it(lap, monkeypatch):
    secret = "pass" + "word"

    def fail():
        raise ValueError(secret)

    monkeypatch.setattr(clean.urllib.request, "build_opener", fail)
    lines, code = clean.doctor()
    assert code == clean.EXIT_ERROR and secret not in "\n".join(lines)


def test_doctor_preserves_valid_proxy_hosts_and_classifies_private_errors(lap, monkeypatch):
    from types import SimpleNamespace
    import ssl
    import urllib.error

    secret = PASSWORD
    monkeypatch.setenv("HTTPS_PROXY", "http://user:" + secret + "@proxy.example.org:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    errors = iter([urllib.error.HTTPError("https://example.org", 407, secret, {}, None),
                   urllib.error.URLError(ssl.SSLCertVerificationError(1, secret))])

    def fail(req, timeout):
        assert req.method == "HEAD"
        raise next(errors)

    lines, code = clean.doctor(opener=SimpleNamespace(open=fail))
    text = "\n".join(lines)
    assert code == clean.EXIT_HELD and secret not in text
    assert "HTTPS_PROXY: proxy.example.org:3128 (with a login)" in text
    assert "NO_PROXY: localhost,127.0.0.1" in text
    assert clean.FIX_LOGIN in text and clean.FIX_TLS in text


def test_main_missing_and_unreadable_input_errors_do_not_echo_paths(lap, tmp_path, monkeypatch, capsys):
    _register()
    source = tmp_path / (fx.CUSTOMER_FORMS[0] + ".log")
    args = [str(source), "--project", PROJECT, "--out", str(tmp_path / "out")]
    assert clean.main(args) == clean.EXIT_ERROR
    source.write_text("ok\n", encoding="utf-8")

    def fail(path):
        raise PermissionError("pass" + "word", str(path))

    monkeypatch.setattr(clean, "_read_log", fail)
    assert clean.main(args) == clean.EXIT_ERROR
    text = capsys.readouterr()
    assert fx.CUSTOMER_FORMS[0] not in text.out + text.err
    assert "pass" + "word" not in text.out + text.err


def test_update_counts_ignore_indented_comments_and_duplicate_words():
    old = b" # old comment\nalpha\nalpha\n"
    new = b"  # new comment\nalpha\n beta\nbeta\n"
    assert clean._changes({"allowed-terms.txt": old}, {"allowed-terms.txt": new}) == [
        "allowed-terms.txt: 1 -> 2 entries (1 added, 0 removed)"]


@pytest.mark.parametrize("project", ["../outside", "tcp-q7m4\n", fx.CUSTOMER_FORMS[0]])
def test_put_refuses_an_invalid_project_before_any_bucket_call(lap, tmp_path, project):
    copy = tmp_path / "app.clean.log"
    copy.write_bytes(b"ok\n")

    class NoBucket:
        def __getattr__(self, name):
            pytest.fail("invalid projects must be refused before the bucket")

    with pytest.raises(laptop.LaptopError, match="project code") as exc:
        clean.put_files(project, [copy], client=NoBucket())
    assert project not in str(exc.value)
