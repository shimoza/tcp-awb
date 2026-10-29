"""The layout and the limits of the owner bucket, in one place for every side (F1 step 9): `awb bucket`, the key
service (`awb/tcp/keys_exchange.py`), the exchange (`awb/tcp/xchg.py`) and the materials service of the console.

One working folder per project, `<YYYY-MM>/<project-code>/` with `in/` for the owner's originals and `out/` for
results. The gateway imports no module of awb and keeps its own copy of `MAX_UPLOAD`; tests/test_bucket_layout.py
holds the two equal. Standard library only, no setting and no call: `awb/bucket.py` reads the settings and talks to
the bucket.
"""
from __future__ import annotations

IN = "in/"
OUT = "out/"
KEEP = ".keep"
KEEP_TEXT = b"keeps this folder visible in S3 clients; ignored by awb\n"
"""The small file in in/ and out/ of every project folder: some S3 clients cannot open an empty folder. Every
reader of in/ and out/ ignores it (`is_keep`)."""
OWNER_BUCKET = "awb"
"""The owner bucket when no setting names one: `awb bucket` (`settings()`) and keys.conf (owner_bucket)."""
REGION = "eu-de"
DATASET_PREFIX = "datasets/tcp-facts/"
"""Where `awb dataset put` writes in the owner bucket: <date>/<file> and LATEST."""
REPORTS_PREFIX = "reports/"
"""Where `awb report --codes --put` writes in the owner bucket: owner calls only, a session's calls never reach it."""
REPORT_KEYS = frozenset(REPORTS_PREFIX + n for n in ("projects.csv", "projects.meta.json"))
"""The files of reports/ the owner's CLI may put: the codes file of his report workbook and its sidecar."""
INVENTORY_PREFIX = REPORTS_PREFIX + "inventory/"
"""Where the coded inventories of the move plan lie (awb/inventory.py): the laptop's of `awb clean inventory --put`
with his own key, the host's and the buckets' of `awb files inventory`."""
MONTH = r"[0-9]{4}-(?:0[1-9]|1[0-2])"
"""A month folder of the top of the owner bucket, <YYYY-MM>, without its slash (`awb bucket`)."""
WEB_MONTH = r"\d{4}-(?:0[1-9]|1[0-2])"
"""The month as the web mode of the key service spells it (`keys_exchange.WEB_MONTH_RE`, `_WEB_FOLDER_RE`): its
\\d also takes the digits of other scripts. Kept as it was (F1 step 9 changes no behaviour)."""
MAX_UPLOAD = 25 * 1024 * 1024
"""One file of the owner's console into a project's in/ and the most the materials service reads of one object."""
MAX_MB = 100
"""The default of max_mb in keys.conf: the most the key service moves in one object."""
CONSOLE_SUFFIXES = frozenset((".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".pdf", ".docx", ".xlsx",
                              ".pptx", ".odt", ".ods", ".odp"))
"""The files the owner's console puts into in/ and imports from a bucket (the materials service)."""
TEXT_SUFFIXES = (".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".tf", ".py", ".sh", ".xml",
                 ".html", ".htm", ".ini", ".conf", ".cfg", ".toml", ".sql", ".ps1", ".rst")
"""Text files of the exchange (`xchg.TEXT_SUFFIXES`)."""
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
"""Pictures of the exchange: one crosses into from-session/ only with --image and a reason."""


def is_keep(key: str) -> bool:
    """True for the .keep file of a layout folder (by its last segment)."""
    return key.rsplit("/", 1)[-1] == KEEP


def folder_of(month: str, code: str) -> str:
    """The folder of a project under a month (<YYYY-MM> without its slash): <YYYY-MM>/<code>/."""
    return "%s/%s/" % (month, code)
