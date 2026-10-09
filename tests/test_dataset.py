"""awb/tcp/dataset.py: the dataset TCP Facts built from a throw-away knowledge base and price snapshots, its
how-to PDF, and the put into a stand-in bucket."""
from __future__ import annotations

import json
import re
import subprocess
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from types import SimpleNamespace

from awb import cli, kb
from awb.tcp import dataset, price
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

TODAY = date(2026, 10, 8)
S_ECS = "ECS flavor s3.large.2 is offered in eu-de"
S_OBS = "OBS buckets in eu-de keep object versions when versioning is on"
S_IAM = "The IAM user quota is 500 per tenant in eu-de, read with GET on the OS-QUOTA endpoint"
RAW = {"id": "OTC_ECS_S3L2", "productIdParameter": "ecs", "productId": "Elastic Cloud Server", "opiFlavour": "s3.large.2",
       "productName": "s3.large.2 linux", "osUnit": "Linux", "vCpu": "2", "ram": "4 GiB", "unit": "h", "currency": "EUR",
       "priceAmount": "0.049000 EUR", "R12": "0.040000 EUR", "R24": "0.038000 EUR", "R36": "0.036000 EUR",
       "RU12": "0.030000 EUR", "RU24": "0.028000 EUR", "RU36": "0.026000 EUR", "region": "eu-de"}


def new(statement: str, **kw) -> kb.Entry:
    args = dict(scope="tcp", tags=["ecs"], grade="docs", cls="api", source="docs mirror, user guide", today=TODAY,
                checked=TODAY)
    args.update(kw)
    return kb.add(statement, **args)


def snapshot(home, region: str, day: str, records: list[dict]) -> Path:
    folder = price.snapshot_dir(home) / region
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (day + ".json")
    path.write_text(json.dumps({"source": "price API", "region": region, "fetched_at": day + " 06:00 UTC",
                                "cached_at": "", "count": len(records), "records": records}), encoding="utf-8")
    return path


@pytest.fixture
def live(monkeypatch):
    """The live price API answers with the records of the snapshot of 2026-10-07 (the same values)."""
    def fetch(service, region):
        rows = [dict(RAW, priceAmount="0.051000 EUR")]
        return SimpleNamespace(records=[price.Record.from_raw(r) for r in rows if r["productIdParameter"] == service])
    monkeypatch.setattr(price, "fetch", fetch)
    return fetch


@pytest.fixture
def facts(home, live):
    live = new(S_ECS, grade="live", source="live API call on test-10491, eu-de")
    new(S_OBS, grade="docs", tags=["obs", "storage"])
    new(S_IAM, grade="live", tags=["iam"], source="awb cloud call with the read key of test tenant test-10497 in eu-de")
    new("GPU flavor p2 needs a quota increase in eu-nl", grade="said", tags=["gpu"])
    snapshot(home, "eu-de", "2026-10-01", [RAW])
    bms = dict(RAW, id="OTC_BMS_EL_BLST_SAS", productIdParameter="bms", productId="BARE METAL", productName="EVS High I/O",
               opiFlavour="vss.sas", priceAmount="0.066000 EUR")
    snapshot(home, "eu-de", "2026-10-07", [dict(RAW, priceAmount="0.051000 EUR"), bms])
    return live


def test_the_rewrite_of_a_tenant_alias_is_proven_on_a_plant_and_covers_both_forms():
    assert dataset.tenant_free("a call on test-10491 in eu-de") == "a call on a TCP test tenant in eu-de"
    assert dataset.tenant_free("the key of test tenant test-10497 got 403") == "the key of a TCP test tenant got 403"
    assert dataset.tenant_free("the key of the test tenant test-10497") == "the key of a TCP test tenant"
    assert "test-1049" not in dataset.tenant_free(dataset._PLANT)
    assert dataset.tenant_free("awb cloud call against test-10491, a DELETE through the key service") == \
        "a scripted call against a TCP test tenant, a DELETE through a signed call"
    assert not dataset._TOOL_LEFT_RE.search(dataset.tenant_free(dataset._PLANT_TOOL))


def test_build_writes_every_file_with_the_rules_and_no_tenant_alias(home, facts, tmp_path, capsys):
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08"]) == 0
    out = capsys.readouterr().out
    assert "3 facts (docs 1, live 2)" in out and "3 topics" in out and "prices eu-de 1" in out
    assert "best before 2026-11-07" in out and "left out: grade 1" in out
    assert "live price check eu-de: 1 rows of 1 services match the API" in out
    assert "operator named in prose: none" in out
    assert dataset.operator_named([{"id": "KB-X", "statement": "the host iam.eu-de.otc.t-systems.com answers", "source": "", "tried": []}]) == []
    assert dataset.operator_named([{"id": "KB-Y", "statement": "fine", "source": "a page of T-Systems", "tried": []}]) == ["KB-Y"]
    root = tmp_path / "ds" / "tcp-facts-2026-10-08"
    names = {str(x.relative_to(root)) for x in root.rglob("*") if x.is_file()}
    assert {"README.md", "PROMPT.md", "HOW-TO.pdf", "facts.md", "facts.jsonl", "services.md", "MANIFEST.json",
            "prices/eu-de.csv", "topics/ecs.md", "topics/obs.md", "topics/iam.md"} <= names
    facts_md = (root / "facts.md").read_text()
    assert facts_md.startswith("<!-- TCP Facts, dataset of 2026-10-08.")
    assert "Best before 2026-11-07" in facts_md and "taken 2026-10-07 06:00 UTC" in facts_md
    assert "- %s (%s, live, checked 2026-10-08)" % (S_ECS, facts.id) in facts_md
    assert "## ecs (1)" in facts_md and "## obs (1)" in facts_md and "GPU flavor p2" not in facts_md
    for name in names:
        if name.endswith((".md", ".jsonl", ".csv")):
            assert "test-1049" not in (root / name).read_text(), name
    rows = [json.loads(l) for l in (root / "facts.jsonl").read_text().splitlines()]
    assert rows[0]["dataset"] == "TCP Facts" and rows[0]["count"] == 3
    assert next(r for r in rows[1:] if r["id"] == facts.id)["source"] == "live API call on a TCP test tenant, eu-de"
    assert not any(re.search(r"\bawb\b|key service", r["source"]) for r in rows[1:])
    topic = (root / "topics" / "iam.md").read_text()
    assert topic.startswith("<!-- TCP Facts") and S_IAM in topic and S_OBS not in topic
    csv_lines = (root / "prices" / "eu-de.csv").read_text().splitlines()
    assert csv_lines[0].startswith("# TCP price list eu-de") and "fetched 2026-10-07 06:00 UTC" in csv_lines[0]
    assert csv_lines[1].startswith("id,service,product,flavor,os,vcpu,ram,unit,currency,payg,reserved_12m")
    assert csv_lines[2] == "OTC_ECS_S3L2,Elastic Cloud Server,s3.large.2 linux,s3.large.2,Linux,2,4 GiB,h,EUR,0.051000," \
                           "0.040000,0.038000,0.036000,0.030000,0.028000,0.026000"
    # a service the service description does not offer stays out of the price list, named in the header
    assert len(csv_lines) == 3 and "OTC_BMS" not in (root / "prices" / "eu-de.csv").read_text()
    assert "does not offer are left out" in csv_lines[0] and "BARE METAL (1)" in csv_lines[0]
    assert dataset.not_offered(dataset.offered.load(), "2026-10-08", "bms", "BARE METAL")
    assert dataset.not_offered(dataset.offered.load(), "2026-10-08", "csbs", "CLOUD SERVER BACKUP SERVICE")
    assert not dataset.not_offered(dataset.offered.load(), "2026-10-08", "ecsflex", "ELASTIC CLOUD SERVER (Flexible)")
    services = (root / "services.md").read_text()
    assert "Elastic Cloud Server (ECS)" in services and "revision" in services
    manifest = json.loads((root / "MANIFEST.json").read_text())
    assert manifest["compiled_by"] == "TCP Facts, github.com/shimoza/tcp-facts" and "author" not in manifest
    readme = (root / "README.md").read_text()
    assert ("Compiled from public sources and live checks. Licence CC BY 4.0: name the dataset and its address when "
            "you reuse it. No warranty: the service description and the price list of the provider are the binding "
            "documents.") in readme and "Dataset: TCP Facts, github.com/shimoza/tcp-facts." in readme
    assert manifest["facts"] == 3 and manifest["grades"] == {"docs": 1, "live": 2} and manifest["best_before"] == "2026-11-07"
    assert manifest["built_at"].endswith(" UTC") and "Build of %s" % manifest["built_at"] in (root / "README.md").read_text()
    assert "built %s" % manifest["built_at"] in out
    assert manifest["prices"]["records"] == {"eu-de": 1} and manifest["prices"]["left_out"] == {"eu-de": {"BARE METAL": 1}}
    assert manifest["files"]["facts.md"]["bytes"] == (root / "facts.md").stat().st_size
    assert len(manifest["files"]["HOW-TO.pdf"]["sha256"]) == 64 and "MANIFEST.json" not in manifest["files"]
    with zipfile.ZipFile(tmp_path / "ds" / "tcp-facts-2026-10-08.zip") as z:
        assert "tcp-facts-2026-10-08/facts.md" in z.namelist() and "tcp-facts-2026-10-08/MANIFEST.json" in z.namelist()
        assert z.read("tcp-facts-2026-10-08/PROMPT.md").decode() == (root / "PROMPT.md").read_text()
    prompt = (root / "PROMPT.md").read_text()
    assert dataset.ONE_LINER in prompt and dataset.LONG_PROMPT in prompt and "CC BY 4.0" in prompt


def test_the_how_to_pdf_is_a_pdf_the_poppler_tools_read(home, facts, tmp_path):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    pdf = built.folder / "HOW-TO.pdf"
    assert pdf.read_bytes().startswith(b"%PDF-1.4")
    text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    assert "TCP Facts 2026-10-08: how to use it" in text
    assert "Use the attached TCP Facts. Answer: <your question>" in text
    assert "Best before 2026-11-07" in text and "CC BY 4.0" in text
    assert "Compiled from public sources and live checks. Licence CC BY 4.0" in text and "Author" not in text
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    assert re.search(r"^Author:\s+TCP Facts, github.com/shimoza/tcp-facts$", info, re.M)
    # a long how-to runs over several pages and stays readable
    long = dataset.pdf_bytes([("p", "a line of the how-to that is long enough to wrap %d" % i) for i in range(200)], "t")
    assert long.count(b"/Type /Page ") >= 3
    assert "wrap 199" in subprocess.run(["pdftotext", "-", "-"], input=long, capture_output=True, check=True).stdout.decode()


def test_no_setting_puts_a_person_into_the_dataset(home, facts, tmp_path, monkeypatch):
    """The old author setting is gone: a planted AWB_DATASET_AUTHOR and dataset_author reach no file of the build."""
    monkeypatch.setenv("AWB_DATASET_AUTHOR", fx.PLANTED_PERSON)
    monkeypatch.setattr(dataset.config, "host_conf", lambda: {"dataset_author": fx.PLANTED_PERSON})
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    files = [x for x in (tmp_path / "ds").rglob("*") if x.is_file()]
    assert any(x.name == "HOW-TO.pdf" for x in files) and any(x.suffix == ".zip" for x in files)
    planted = fx.PLANTED_PERSON.encode()
    for x in files:
        assert planted not in x.read_bytes(), x.name
    with zipfile.ZipFile(built.folder.parent / (built.folder.name + ".zip")) as z:
        for n in z.namelist():
            assert planted not in z.read(n), n
    text = subprocess.run(["pdftotext", str(built.folder / "HOW-TO.pdf"), "-"], capture_output=True, text=True,
                          check=True).stdout
    assert fx.PLANTED_PERSON not in text and not hasattr(dataset, "author")


def test_build_refuses_without_facts_over_an_existing_folder_and_with_an_alias_left(home, facts, tmp_path, capsys,
                                                                                    monkeypatch):
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08"]) == 0
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08"]) == 1
    assert "exists; give --force" in capsys.readouterr().err
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08", "--force"]) == 0
    assert cli.main(["dataset", "build", "--date", "2026-10-8"]) == 1
    # a broken rewrite is caught before anything is written
    monkeypatch.setattr(dataset, "_TENANT_RE", dataset.re.compile(r"never-matches"))
    with pytest.raises(dataset.DatasetError, match="failed its own test"):
        dataset.build(home, out=tmp_path / "ds2", today=TODAY)
    assert not (tmp_path / "ds2").exists()
    # a rewrite that passes the plant but misses a form in the files: the folder is removed again
    monkeypatch.setattr(dataset, "_TENANT_RE", dataset.re.compile(r"test-1049[17]\b(?= and| in eu-de$)"))
    monkeypatch.setattr(dataset, "_PLANT", "test-10491 and test-10497 in eu-de")
    with pytest.raises(dataset.DatasetError, match="tenant alias was left"):
        dataset.build(home, out=tmp_path / "ds3", today=TODAY)
    assert not (tmp_path / "ds3" / "tcp-facts-2026-10-08").exists()


def test_build_refuses_an_empty_knowledge_base(home, tmp_path, capsys):
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds")]) == 1
    assert "nothing to build" in capsys.readouterr().err


def test_put_sends_the_zip_the_pdf_and_the_manifest_and_marks_the_latest(home, facts, tmp_path, capsys, monkeypatch):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    with FakeOBS() as fake:
        keys = dataset.put(fake.client(), built.folder)
        assert keys == ["datasets/tcp-facts/2026-10-08/tcp-facts-2026-10-08.zip",
                        "datasets/tcp-facts/2026-10-08/HOW-TO.pdf", "datasets/tcp-facts/2026-10-08/MANIFEST.json"]
        assert fake.objects["datasets/tcp-facts/LATEST"] == b"2026-10-08\n"
        assert fake.objects[keys[1]].startswith(b"%PDF")
        with pytest.raises(dataset.DatasetError, match="already; give --replace"):
            dataset.put(fake.client(), built.folder)
        assert dataset.put(fake.client(), built.folder, replace=True) == keys
        # the command finds the newest build and needs the owner's key setting
        monkeypatch.setenv("AWB_BUCKET_KEYS", "")
        assert cli.main(["dataset", "put", "--out", str(tmp_path / "ds")]) == 1
        assert "no key setting" in capsys.readouterr().err
        assert cli.main(["dataset", "put", "--out", str(tmp_path / "none")]) == 1
        assert "no built dataset" in capsys.readouterr().err
    (built.folder / "HOW-TO.pdf").unlink()
    with FakeOBS() as fake:
        with pytest.raises(dataset.DatasetError, match="not a complete build"):
            dataset.put(fake.client(), built.folder)


def test_the_refresh_builds_the_dataset_after_a_clean_run_and_not_after_an_error(home, facts, monkeypatch):
    from awb.tcp import refresh

    parts, _ = refresh.run(home, parts=("knowledge", "dataset"), update=False, today=TODAY)
    ds = next(p for p in parts if p.name == "dataset")
    assert not ds.error and ds.lines[0].startswith("tcp-facts-2026-10-08: 3 facts")
    assert (dataset.datasets_dir(home) / "tcp-facts-2026-10-08.zip").exists()
    # the same day again: built over, not refused
    parts, _ = refresh.run(home, parts=("knowledge", "dataset"), update=False, today=TODAY)
    assert not next(p for p in parts if p.name == "dataset").error
    monkeypatch.setattr(refresh, "part_knowledge", lambda *a, **k: (_ for _ in ()).throw(kb.KBError("broken")))
    parts, _ = refresh.run(home, parts=("knowledge", "dataset"), update=False, today=TODAY + timedelta(days=1))
    ds = next(p for p in parts if p.name == "dataset")
    assert ds.lines == ["not built: knowledge ended in an error"]
    assert not (dataset.datasets_dir(home) / "tcp-facts-2026-10-09.zip").exists()


def test_a_fact_that_presents_a_service_not_offered_stays_out_and_a_negative_about_it_stays_in(home, facts, tmp_path, capsys):
    offered_bms = new("Bare Metal Server (BMS) is offered in eu-de with three flavors", grade="live", tags=["bms"],
                      source="live call on a test tenant, eu-de")
    gone_bms = new("Bare Metal Server (BMS) is ramped down on TCP and no longer orderable", grade="live", tags=["bms"],
                   source="live call on a test tenant, eu-de", tried=("flavor listing, eu-de", "console order form, eu-de"))
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08"]) == 0
    out = capsys.readouterr().out
    assert "facts naming a service not offered: %s (Bare Metal Server)" % offered_bms.id in out
    facts_md = (tmp_path / "ds" / "tcp-facts-2026-10-08" / "facts.md").read_text()
    assert offered_bms.id not in facts_md and gone_bms.id in facts_md
    manifest = json.loads((tmp_path / "ds" / "tcp-facts-2026-10-08" / "MANIFEST.json").read_text())
    assert manifest["facts_left_out"] == [{"id": offered_bms.id, "names": ["Bare Metal Server"]}]
    assert manifest["facts"] == 4


def test_the_checks_of_a_build_prove_themselves_first(home, facts, tmp_path, monkeypatch):
    with pytest.MonkeyPatch.context() as m:
        m.setattr(dataset.offered, "mentions", lambda *a, **k: [])
        with pytest.raises(dataset.DatasetError, match="offered check over the facts failed its own test"):
            dataset.build(home, out=tmp_path / "a", today=TODAY)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(dataset, "not_offered", lambda *a, **k: False)
        with pytest.raises(dataset.DatasetError, match="price filter failed its own test"):
            dataset.build(home, out=tmp_path / "b", today=TODAY)
    with pytest.MonkeyPatch.context() as m:
        # the comparison that cannot tell a planted wrong price is refused
        m.setattr(price.Record, "price", lambda self, term: None)
        with pytest.raises(dataset.DatasetError, match="live price check failed its own test"):
            dataset.build(home, out=tmp_path / "c", today=TODAY)
    for name in ("a", "b", "c"):
        assert not (tmp_path / name / "tcp-facts-2026-10-08").exists()


def test_a_live_price_that_differs_from_the_snapshot_refuses_the_build(home, facts, tmp_path, monkeypatch, capsys):
    def fetch(service, region):
        return SimpleNamespace(records=[price.Record.from_raw(dict(RAW, priceAmount="0.052000 EUR"))])
    monkeypatch.setattr(price, "fetch", fetch)
    with pytest.raises(dataset.DatasetError, match="live price check of eu-de: OTC_ECS_S3L2 snapshot 0.051000 live 0.052000"):
        dataset.build(home, out=tmp_path / "ds", today=TODAY)
    monkeypatch.setattr(price, "fetch", lambda service, region: SimpleNamespace(records=[]))
    with pytest.raises(dataset.DatasetError, match="OTC_ECS_S3L2 not served live"):
        dataset.build(home, out=tmp_path / "ds", today=TODAY)
    # offline: the check is skipped and says so
    assert cli.main(["dataset", "build", "--out", str(tmp_path / "ds"), "--date", "2026-10-08", "--no-live"]) == 0
    assert "live price check skipped" in capsys.readouterr().out


FAKE_GH = '''#!%s
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
remote = os.environ["FAKE_GH_REMOTE"]
if args[:2] == ["api", "user"]:
    print({".login": "shimoza", ".id": "42"}[args[3]])
    sys.exit(0)
if args[:2] == ["repo", "clone"]:
    sys.exit(subprocess.run(["git", "clone", "-q", remote, args[3]]).returncode)
if args[:2] == ["release", "create"]:
    tagged = subprocess.run(["git", "ls-remote", "--tags", remote, args[2]], capture_output=True, text=True).stdout
    sys.exit(0 if tagged.strip() and all(os.path.isfile(a) for a in args[3:6]) else 1)
sys.exit(0)
'''


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A fake gh on the PATH whose clone comes from a bare repository with the hand-written README and LICENSE,
    and a commit identity that is a person: the scan must keep it out of every file."""
    import sys
    gitconf = tmp_path / "gitconfig"
    gitconf.write_text("[user]\n\tname = %s\n\temail = xqarv@example.invalid\n[init]\n\tdefaultBranch = main\n"
                       % fx.PLANTED_PERSON)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconf))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote, seed = tmp_path / "remote.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    (seed / "README.md").write_text("# TCP Facts\n")
    (seed / "LICENSE").write_text("Attribution 4.0 International\n")
    for cmd in (["add", "-A"], ["commit", "-q", "-m", "start"], ["push", "-q", str(remote), "HEAD:main"]):
        subprocess.run(["git", *cmd], cwd=seed, check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH % sys.executable)
    (bindir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bindir, __import__("os").environ["PATH"]))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_GH_REMOTE", str(remote))
    return SimpleNamespace(remote=remote, clone=tmp_path / "clone.git",
                           log=lambda: [json.loads(l) for l in (tmp_path / "gh.log").read_text().splitlines()])


def _remote_files(remote: Path, ref: str) -> list[str]:
    return subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=remote, capture_output=True, text=True,
                          check=True).stdout.split()


def test_github_put_lands_the_files_in_the_root_tags_the_date_and_releases_the_assets(home, facts, tmp_path, github,
                                                                                       monkeypatch):
    monkeypatch.setenv("AWB_DATASET_AUTHOR", fx.PLANTED_PERSON)
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    lines = dataset.github_put(home, built.folder, clone=github.clone)
    assert lines[-1].startswith("github.com/shimoza/tcp-facts/releases/tag/2026-10-08 with tcp-facts-2026-10-08.zip")
    files = _remote_files(github.remote, "refs/tags/2026-10-08")
    assert {"README.md", "LICENSE", "facts.md", "facts.jsonl", "services.md", "prices/eu-de.csv",
            "topics/ecs.md"} <= set(files)
    assert subprocess.run(["git", "show", "main:facts.md"], cwd=github.remote, capture_output=True,
                          check=True).stdout == (built.folder / "facts.md").read_bytes()
    assert subprocess.run(["git", "show", "main:README.md"], cwd=github.remote, capture_output=True,
                          check=True).stdout == b"# TCP Facts\n"        # the hand-written README stays
    subject = subprocess.run(["git", "log", "-1", "--format=%s", "main"], cwd=github.remote, capture_output=True,
                             text=True, check=True).stdout.strip()
    assert subject == "TCP Facts 2026-10-08"
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>", "main"], cwd=github.remote, capture_output=True,
                            text=True, check=True).stdout.strip()
    assert author == "shimoza <42+shimoza@users.noreply.github.com>"     # the account, never the person of the identity
    assert fx.PLANTED_PERSON in dataset.person_values()
    for x in github.clone.rglob("*"):
        if x.is_file() and ".git" not in x.relative_to(github.clone).parts:
            assert fx.PLANTED_PERSON.encode() not in x.read_bytes(), x.name
    create = [a for a in github.log() if a[:2] == ["release", "create"]]
    assert len(create) == 1 and create[0][2] == "2026-10-08"
    assert [Path(a).name for a in create[0][3:6]] == ["tcp-facts-2026-10-08.zip", "HOW-TO.pdf", "MANIFEST.json"]
    assert create[0][create[0].index("--title") + 1] == "TCP Facts 2026-10-08"
    notes = create[0][create[0].index("--notes") + 1]
    assert notes.startswith("TCP Facts 2026-10-08: 3 facts (live 2, docs 1, contract 0), 0 left out") and "CC BY 4.0" in notes
    assert fx.PLANTED_PERSON not in notes
    # the same date again needs --replace, which moves the tag and makes the release again
    with pytest.raises(dataset.DatasetError, match="already; give --replace"):
        dataset.github_put(home, built.folder, clone=github.clone)
    dataset.github_put(home, built.folder, replace=True, clone=github.clone)
    log = github.log()
    assert ["release", "delete", "2026-10-08", "--repo", "shimoza/tcp-facts", "--yes"] in log
    assert len([a for a in log if a[:2] == ["release", "create"]]) == 2
    assert [a[:2] for a in log].count(["repo", "clone"]) == 1


@pytest.mark.parametrize("plant,why", [(fx.PLANTED_PERSON, "a person"), ("xqarv@example.invalid", "a person"),
                                       (fx.PERSON_FORMS[0], "facts.md: name")])
def test_github_put_refuses_a_planted_name_and_publishes_nothing(home, facts, tmp_path, github, plant, why):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    facts_md = built.folder / "facts.md"
    facts_md.write_text(facts_md.read_text() + "- a note by %s (KB-0000, docs, checked 2026-10-08)\n" % plant)
    with pytest.raises(dataset.DatasetError, match="refused, nothing was published: .*%s" % why) as err:
        dataset.github_put(home, built.folder, clone=github.clone)
    assert plant not in str(err.value)
    assert not subprocess.run(["git", "tag", "--list"], cwd=github.remote, capture_output=True, text=True,
                              check=True).stdout.strip()
    assert _remote_files(github.remote, "main") == ["LICENSE", "README.md"]
    assert not any(a[:2] == ["release", "create"] for a in github.log())
    assert not subprocess.run(["git", "status", "--porcelain"], cwd=github.clone, capture_output=True, text=True,
                              check=True).stdout.strip()
