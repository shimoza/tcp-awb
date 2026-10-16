"""One owner of the bucket layout, the suffix lists and the upload limits (B-026, step 9 of the F1 plan):
awb/bucket_layout.py spells them once and awb bucket, the key service, the materials service and the exchange build on
it. The gateway imports no module of awb, so its copy of the upload limit is held equal here. An owner bucket in
memory, invented codes."""
import re
from pathlib import Path
from types import SimpleNamespace

from awb import bucket, config, obs
from awb import bucket_layout as layout
from awb.tcp import keys_exchange, xchg
from awb.tcp.web import gateway, materials_api

PACKAGE = Path(__file__).resolve().parents[1] / "awb"
CODE = "tcp-q7m4"
MIB = 1024 * 1024


class Memory:
    """An owner bucket in memory: a listing with a delimiter, head and put_bytes, what the layout calls."""

    def __init__(self, *keys):
        self.objects = {k: b"" for k in keys}

    def list(self, prefix="", delimiter=""):
        objects, prefixes = [], set()
        for key in sorted(self.objects):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            else:
                objects.append((key, len(self.objects[key]), "e" * 32))
        return obs.Listing(objects, sorted(prefixes))

    def head(self, key):
        return {"size": len(self.objects[key])} if key in self.objects else None

    def put_bytes(self, key, data):
        self.objects[key] = data


def context(c, active=(CODE,)):
    return keys_exchange.Context(dict(xchg.DEFAULTS, bucket_tenant="test-1"), lambda alias: object(),
                                 lambda name, keys: c, lambda: None,
                                 lambda code: SimpleNamespace(code=code) if code in active else None,
                                 lambda subject, message: None)


def test_a_new_folder_is_month_and_code_with_in_and_out():
    c = Memory()
    folder, created = bucket.ensure_folder(c, SimpleNamespace(code=CODE, created="2026-10-02"))
    assert created and folder == layout.folder_of("2026-10", CODE) == "2026-10/tcp-q7m4/"
    assert set(c.objects) == {folder, folder + layout.IN, folder + layout.OUT}
    assert (bucket.IN, bucket.OUT, xchg.IN) == (layout.IN, layout.OUT, layout.IN) == ("in/", "out/", "in/")


def test_the_web_mode_reads_the_folder_the_layout_makes():
    c = Memory()
    folder, _ = bucket.ensure_folder(c, SimpleNamespace(code=CODE, created="2026-10-02"))
    answer, _ = keys_exchange.serve_web_read(context(c), {"what": "months", "bucket": "owner"})
    assert answer == {"ok": True, "months": bucket.months(c)} and answer["months"] == ["2026-10/"]
    assert keys_exchange._web_folder(context(c), "owner", folder + layout.IN) == (folder + layout.IN, CODE)
    assert keys_exchange._web_folder(context(c), "owner", folder + layout.OUT) is None
    assert keys_exchange._web_folder(context(c, active=()), "owner", folder + layout.IN) is None
    assert xchg._WEB_FOLDER_RE is keys_exchange._WEB_FOLDER_RE


def test_the_materials_service_lists_the_in_folders_of_the_layout():
    class Months:
        def months(self):
            return ["2026-08/", "2026-10/"]

    class Sources(materials_api.Sources):
        def client(self, source):
            return Months()

    project = SimpleNamespace(code=CODE, created="2026-09-30")
    assert Sources().prefixes(project, "customer") == ["inbox/"] + [
        layout.folder_of(m, CODE) + layout.IN for m in ("2026-08", "2026-09", "2026-10")]
    assert Sources().prefixes(project, "brief") == ["inbox/"]


def test_the_month_spellings_keep_what_they_accepted():
    """The CLI's month takes ASCII digits only; the web mode's \\d also those of other scripts, as before."""
    assert bucket._MONTH_RE.pattern == r"[0-9]{4}-(?:0[1-9]|1[0-2])/"
    assert keys_exchange.WEB_MONTH_RE.pattern == r"^\d{4}-(?:0[1-9]|1[0-2])/$"
    for month in ("2026-01/", "2026-12/"):
        assert bucket._MONTH_RE.fullmatch(month) and keys_exchange.WEB_MONTH_RE.match(month)
    for bad in ("2026-00/", "2026-13/", "26-10/", "2026-1/"):
        assert not bucket._MONTH_RE.fullmatch(bad) and not keys_exchange.WEB_MONTH_RE.match(bad), bad


def test_one_upload_limit_and_one_key_service_limit():
    assert layout.MAX_UPLOAD == materials_api.MAX_FILE == gateway.MAX_UPLOAD == 25 * MIB
    assert layout.MAX_MB == 100 and xchg.DEFAULTS["max_mb"] == "100"
    assert context(Memory()).max_bytes() == 100 * MIB
    for value, limit in (("7", 7 * MIB), ("", 100 * MIB), ("many", 100 * MIB), ("0", MIB)):
        ctx = context(Memory())
        ctx.settings = {"max_mb": value}
        assert ctx.max_bytes() == limit, value


def test_the_suffix_lists_live_in_the_layout_and_keep_their_entries():
    assert materials_api.SUPPORTED is layout.CONSOLE_SUFFIXES
    assert materials_api.SUPPORTED == {".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".pdf", ".docx",
                                       ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
    assert xchg.IMAGE_SUFFIXES is layout.IMAGE_SUFFIXES == (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
    assert xchg.TEXT_SUFFIXES is layout.TEXT_SUFFIXES and len(layout.TEXT_SUFFIXES) == 21


def test_one_default_for_the_owner_bucket_and_its_region(monkeypatch):
    for name in ("AWB_BUCKET", "AWB_BUCKET_REGION", "AWB_BUCKET_KEYS", "AWB_BUCKET_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "host_conf", lambda: {})
    s = bucket.settings()
    assert (s["bucket"], s["region"]) == (layout.OWNER_BUCKET, layout.REGION) == ("awb", "eu-de")
    assert (xchg.DEFAULTS["owner_bucket"], xchg.DEFAULTS["region"]) == (layout.OWNER_BUCKET, layout.REGION)


def test_no_other_module_spells_the_month_or_the_upload_limit():
    """Grep the package: the month of the layout and the 25 MiB of the console are written in bucket_layout.py alone
    (and the gateway's copy of the limit, which this file holds equal)."""
    month = re.compile(re.escape("(?:0[1-9]|1[0-2])"))
    limit = re.compile(r"\b25 ?\* ?1024 ?\* ?1024\b")
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if month.search(line) and rel != "bucket_layout.py":
                found.append("awb/%s:%d" % (rel, n))
            if limit.search(line) and rel not in ("bucket_layout.py", "tcp/web/gateway.py"):
                found.append("awb/%s:%d" % (rel, n))
    assert found == [], "build on awb/bucket_layout.py: " + ", ".join(found)


def test_the_layout_needs_no_module_of_awb():
    """Standard library alone: the bucket module and the services import it without reaching each other."""
    text = (PACKAGE / "bucket_layout.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+awb\b", text, re.M)
