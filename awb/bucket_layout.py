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
OWNER_BUCKET = "awb"
"""The owner bucket when no setting names one: `awb bucket` (AWB_BUCKET, bucket) and keys.conf (owner_bucket)."""
REGION = "eu-de"
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


def folder_of(month: str, code: str) -> str:
    """The folder of a project under a month (<YYYY-MM> without its slash): <YYYY-MM>/<code>/."""
    return "%s/%s/" % (month, code)
