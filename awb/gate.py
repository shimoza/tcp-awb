"""The commit gate: refuse files that carry a name, a secret, a home path or anything the owner blocked.

Classes of findings:

    name         a registered form, found with the Matcher over the normalised text (only with a register)
    secret       a key-like assignment (ak, sk, secret, password, pass, pw, passphrase, token, api_key, access_key,
                 private_key, auth, credentials, the German words and similar, followed by ':' or '=', also after
                 a -- or -D flag, inside a longer value, as an XML element or a key/value attribute pair, or with
                 the value on the next line as a YAML block scalar, a heredoc or a JSON value) whose value has 16
                 or more characters, an entropy of at least 3.5 bits per character and is neither a placeholder
                 nor a reference; also a password in the userinfo of a URL, after curl -u, in a .pgpass line,
                 after the word password (.netrc), in a credential table, in an Authorization header, a password
                 hash of .htpasswd and a bare access key pair (a key id of 20 capitals and digits within two
                 lines of a secret of 40 characters)
    private-key  the armour line that opens a private key block (also the ssh.com form with four dashes and
                 spaces), a PuTTY key file, a JWK with its private part, an age secret key and the base64 of an
                 armour line
    token        a credential with a known prefix (GitHub, GitLab, Slack, Google, SendGrid, npm, PyPI, Hugging
                 Face, Stripe, Telegram, Discord, Twilio, a JWT, cloud access key ids)
    homepath     an absolute path into a home folder (the Linux, macOS and Windows spellings, the home of the
                 super user, a tilde with a user name, a tar listing line) or the same path written with dashes
                 (followed by a dash, a slash or the end of the path)
    blocklist    anything a line of the owner's local blocklist matches (BLOCKLIST_FILES, one regular expression
                 per line): names of other workspaces on the host, a naming scheme that must not spread. The list
                 lives outside the repository, so what it holds stays the owner's
    identifier   a 32 hex id (also after 0x), a UUID (also glued to a resource name with a dash) or a 5 or 6
                 digit number right after 'tenant', 'domain', 'project', 'account' or 'Mandant'
    opaque       a file that is not text (a NUL byte in the first 8 KB, the magic of a PDF, a ZIP or a gzip
                 file, the suffix of a document, an archive, an image or a key store) or cannot be read; never
                 silently clean

Every detector runs over the text as given and, when it differs, over its normalised form (awb.normalize:
escapes, entities, percent encoding, invisible characters, terminal colour sequences) with the hits mapped
back, and over what the base64 and hex blocks of the text decode to (such a finding is on the line of the
block). A commit message is scanned the same way (`awb gate --message FILE`, the commit-msg hook) and so are
the author, the committer, the message and the ref names of every pushed commit.

A finding is a file, a line and a class. Line 0 means the file as a whole (opaque), its path (the staged
path of a file is checked like its content; a path given on the command line is checked for names), a pushed
commit ("commit <id>": author, committer, message) or a pushed ref ("ref <name>": its name, for an annotated
tag also its tagger and message). Nothing here prints, logs or raises with the matched value: a printed path
has every registered form in it replaced by [name]. Without a register the gate exits 2 unless --no-register
asks for a run without the name class.

The name class takes the same path as `awb check`: the register when this user can read it, else the vault
daemon over the check socket (`check.RemoteCheck`). When neither answers or the vault is locked, the gate
exits 2 with "name check unavailable: ..." and reports nothing: it never calls a file clean whose names it
could not check.

Limits, kept on purpose: a submodule's content (its gitlink is left out, .gitmodules is scanned), Git LFS
content (the commit carries the pointer text only), the path of an older pushed commit whose blob comes back
in a newer pushed commit (content is scanned once per blob, that older path is not), a key body without any
armour line, an armour line in lower case or wrapped over two lines, a key id and its secret three or more
lines apart, a placeholder word inside a real value.

The patterns below are written so that this file does not match them itself (a character class or a split
string where a literal would match), so the gate can check its own source.
"""
from __future__ import annotations

import bisect
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from awb import check, normalize, register
from awb.extract import text as extract_text
from awb.matcher import Matcher, Span

CLASSES = ("name", "secret", "private-key", "token", "homepath", "blocklist", "identifier", "opaque", "attribution")
BINARY_PROBE = 8192
"""A NUL byte in this many first bytes makes a file opaque."""
_OPAQUE_MAGICS = (b"%PDF-", b"PK\x03\x04", b"\x1f\x8b")
"""The first bytes of a PDF, a ZIP (also docx, xlsx, pptx, jar) and a gzip file: opaque whatever follows."""
_OPAQUE_SUFFIXES = frozenset((
    ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip", ".jar", ".gz", ".tgz", ".bz2", ".xz",
    ".zst", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".p12", ".pfx", ".jks", ".kdbx",
    ".sqlite", ".pyc",
))
"""Documents, archives, images and key stores are opaque by suffix, even when their bytes carry no NUL early."""


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    cls: str


class GateError(Exception):
    """The gate could not run (git failed). The message carries no content of any file."""


# --------------------------------------------------------------------------- detectors
# Each detector takes the decoded text of one file and returns the character offsets of its hits.

_MIN_SECRET_LENGTH = 16
_MIN_SECRET_ENTROPY = 3.5
_MIN_PASSWORD_LENGTH = 8
_MIN_PASSWORD_ENTROPY = 3.0
"""A password in a URL, after curl -u, in a .pgpass line or a credential table is often shorter than a key."""
_NAME_SHAPE_ENTROPY = 4.0
"""A value in the shape of a name (snake, env, dotted) is a reference below this entropy or without a digit."""

_ASSIGN_RE = re.compile(
    r"""(?<![A-Za-z0-9_.-])(?:--?D?)?(?P<key>[A-Za-z0-9_][A-Za-z0-9_.-]*)["']?[ \t]*(?::=|=>|=|:(?![/\\]))[ \t]*"""
    r"""(?:(?:&|!!?)[A-Za-z0-9_:.-]+[ \t]+)?"""
    r"""(?P<value>"[^"\n]*"|'[^'\n]*'|[^\s"'`,;&?{}]+|)"""
)
"""key = value, key: value, also --key=value and -Dkey=value; a YAML anchor or tag before the value is skipped;
an unquoted value stops at &, ?, { and } so that an inner assignment (url=...&password=v) is its own match;
an empty value at the end of the line means the value may be on the next line."""
_BLOCK_INDICATOR_RE = re.compile(r"[|>][+-]?[0-9]?|<<-?[A-Za-z_]*[A-Za-z0-9_]*")
"""A YAML block scalar indicator or a heredoc opener: the value is on the next line."""
_NEXT_LINES = 3
_SECRET_PARTS = ("ak", "sk", "pwd", "passwd", "pass", "pw", "auth")
_SECRET_WORDS = (
    "secret", "password", "passwd", "passphrase", "passwort", "kennwort", "geheimnis", "motdepasse", "token",
    "api_key", "apikey", "access_key", "accesskey", "client_key", "key_data", "private_key", "privatekey",
    "account_key", "master_key", "signing_key", "encryption_key", "credential",
)
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_PLACEHOLDER_RE = re.compile(
    r"(?i)x{4,}|\*{3,}|\.{3}|<[^>]*>|\{\{|\}\}|\$\{|\$\(|%\(|%[a-z_]+%|example|placeholder|change[_-]?me|"
    r"your[_-]?|redacted|dummy|sample|fixture|todo|fixme|replace[_-]?me|not[_-]?set"
)
_REFERENCE_PREFIX_RE = re.compile(
    r"(?i)^(?:arn|env|vault|ref|file|secretsmanager|ssm|kms|op|keyvault|azurekeyvault|gcpkms|awskms):"
)
_SIGIL_RE = re.compile(r"[$@!]\{?[A-Za-z_][A-Za-z0-9_]*\}?")
_DOTTED_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_SNAKE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_KEBAB_RE = re.compile(r"[a-z]+(?:-[a-z]+)+")
_NESTED_ASSIGN_RE = re.compile(r"[A-Za-z0-9_.-]+[ \t]*(?::[ \t]|=[^=\s])")
"""A line that is itself an assignment is not the value of the key on the line before it."""

# secrets written without a key, or with the key and value apart: (pattern, minimum length, minimum entropy)
_USERINFO_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+.-]*://[^/\s@:\"'`]*:(?P<value>[^/\s@\"'`]{8,})@")
_HEADER_RE = re.compile(
    r"""(?i)(?<![A-Za-z0-9_-])(?:proxy-)?authorization["']?[ \t]*[:=][ \t]*["']?"""
    r"""(?:(?:basic|bearer|token|digest|oauth|negotiate|ntlm|apikey|api-key)[ \t]+)?(?P<value>[^\s"'`,;]{8,})"""
)
_WORD_SPACE_RE = re.compile(
    r"""(?i)(?<![A-Za-z0-9_.-])(?:password|passwd|passwort|kennwort|passphrase|secret|token|identified[ \t]+by)"""
    r"""[ \t]+(?P<value>"[^"\n]*"|'[^'\n]*'|[^\s"'`,;=:]+)"""
)
_FLAG_SPACE_RE = re.compile(
    r"""(?<![A-Za-z0-9_-])(?:-p|--password|--passwd|--pass|--secret|--token|--api-key)[ \t]+"""
    r"""(?P<value>"[^"\n]*"|'[^'\n]*'|[^\s"'`,;]+)"""
)
_FLAG_GLUED_RE = re.compile(r"(?<![A-Za-z0-9_-])-p(?P<value>[A-Za-z0-9][^\s\"'`,;]{15,})")
_USER_PW_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:-u|--user|--username|--creds)[ \t]+[^\s:@\"'`]+:(?P<value>[^\s@\"'`]{8,})"
)
_PGPASS_RE = re.compile(r"(?m)^[^:\s]+:(?:[0-9]+|\*):[^:\s]+:[^:\s]+:(?P<value>[^\s:]{8,})$")
_SECRET_SHAPES = (
    (_USERINFO_RE, _MIN_PASSWORD_LENGTH, _MIN_PASSWORD_ENTROPY),
    (_HEADER_RE, _MIN_SECRET_LENGTH, _MIN_SECRET_ENTROPY),
    (_WORD_SPACE_RE, _MIN_SECRET_LENGTH, _MIN_SECRET_ENTROPY),
    (_FLAG_SPACE_RE, _MIN_SECRET_LENGTH, _MIN_SECRET_ENTROPY),
    (_FLAG_GLUED_RE, _MIN_SECRET_LENGTH, _MIN_SECRET_ENTROPY),
    (_USER_PW_RE, _MIN_PASSWORD_LENGTH, _MIN_PASSWORD_ENTROPY),
    (_PGPASS_RE, _MIN_PASSWORD_LENGTH, _MIN_PASSWORD_ENTROPY),
)
_HASH_LINE_RE = re.compile(
    r"(?m)^[A-Za-z0-9._@-]+:(?P<value>\$(?:apr1|2[abxy]|1|5|6|y|7|argon2(?:id|i|d)|pbkdf2[a-z0-9-]*|sha1|md5)\$"
    r"[^\s]{10,}|\{(?:S?SHA(?:256|512)?|MD5|CRYPT|PLAIN)\}[^\s]{10,})"
)
"""user:hash lines of .htpasswd and shadow: a password hash is a secret whatever its entropy."""
_XML_ELEMENT_RE = re.compile(
    r"<(?P<key>[A-Za-z_][A-Za-z0-9_.:-]*)(?:[ \t][^<>\n]*)?>(?P<value>[^<>\n]+)</(?P=key)[ \t]*>"
)
_XML_PAIR_RE = re.compile(
    r"""(?i)(?<![A-Za-z0-9_-])(?:key|name)[ \t]*=[ \t]*(?P<q>["'])(?P<key>[^"'\n]+)(?P=q)[^<>\n]*?"""
    r"""(?<![A-Za-z0-9_-])value[ \t]*=[ \t]*(?P<q2>["'])(?P<value>[^"'\n]*)(?P=q2)"""
)
_TABLE_DELIMITERS = ("|", "\t", ";", ",")
_TABLE_RULE_RE = re.compile(r"[-: ]*")
_TABLE_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_FUNCTION_WORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "in", "into", "is", "it", "its", "no",
    "not", "of", "on", "or", "so", "the", "then", "this", "that", "to", "with", "und", "oder", "der", "die", "das",
))

_PRIVATE_KEY_RE = re.compile(
    r"-{4,5} ?BEGIN (?:[A-Z0-9]+[ \t]+)*PRIVATE[ \t]+KEY(?: BLOCK)? ?-{4,5}"
    r"|PuTTY-User-Key-File-[0-9]"
    r"|(?m:^Private-Lines: [0-9]+)"
    r"|AGE-SECRET-KEY-1[A-Z0-9]{50,}"
    r"|LS0tLS1CRUdJTiB(?:SU0Eg|QUklW|FQyBQ|PUEVO|FTkNS|EU0Eg|QR1Ag|TU0gy)[A-Za-z0-9+/]{10,}"
)
"""The armour line, the PuTTY key file, an age secret key and the base64 of the armour lines of the private key
kinds (RSA, PKCS8, EC, OpenSSH, encrypted, DSA, PGP, SSH2), which differs from the base64 of a certificate."""
_JWK_D_RE = re.compile(r"\"d\"[ \t]*:[ \t]*\"[A-Za-z0-9_-]{16,}\"")
_JWK_KTY = '"kty"'

# prefix and body; the body must also hold a digit (a real key does, a long kebab-case word does not)
_TOKEN_PATTERNS = tuple(
    re.compile(r"(?<![A-Za-z0-9_])" + p)
    for p in (
        r"gh[oprsu]_([A-Za-z0-9]{20,})",
        r"github_pat_([A-Za-z0-9_]{20,})",
        r"sk-([A-Za-z0-9_-]{20,})",
        r"[sr]k_(?:live|test)_([A-Za-z0-9]{16,})",
        r"xox[abeprs]-([A-Za-z0-9-]{10,})",
        r"xapp-[0-9]-([A-Za-z0-9-]{10,})",
        r"hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/([A-Za-z0-9]{20,})",
        r"A(?:KIA|SIA)([A-Z0-9]{16,})",
        r"HPUA([A-Z0-9]{16,})",
        r"glpat-([A-Za-z0-9_-]{20,})",
        r"AIza([A-Za-z0-9_-]{30,})",
        r"ya29\.([A-Za-z0-9_-]{30,})",
        r"SG\.([A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})",
        r"npm_([A-Za-z0-9]{30,})",
        r"pypi-([A-Za-z0-9_-]{40,})",
        r"hf_([A-Za-z0-9]{30,})",
        r"[0-9]{8,10}:AA([A-Za-z0-9_-]{30,})",
        r"[MN][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.([A-Za-z0-9_-]{27,})",
        r"(?:AC|SK)([0-9a-f]{32})(?![A-Za-z0-9])",
        r"ey([J][A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})",
    )
)

_GENERIC_USERS = frozenset(("user", "username", "you", "yourname", "name", "me", "someone", "example"))
_HOMEPATH_RE = re.compile(
    r"(?<![\w.-])(?:/export|/data)?(?:/|\\/)+home(?:/|\\/)+(?P<a>\w[\w.-]*)"
    r"|(?<![\w.-])/(?:mnt/[a-z]/)?Users/(?P<c>[A-Za-z_][\w.-]*)"
    r"|(?<![\w.-])[A-Za-z]:\\+Users\\+(?P<d>[A-Za-z_][\w.-]*)"
    r"|\\home\\(?P<e>[A-Za-z_][\w.-]*)"
    r"|(?<![\w.-])/r[o]ot(?=/)"
    r"|(?<![\w.-])~(?P<f>[A-Za-z_][\w.-]*)(?=/)"
    r"|(?m:^home/(?P<g>\w[\w.-]*)(?=/|$))"
    r"|(?<![A-Za-z0-9])-home-(?P<b>[A-Za-z0-9_][A-Za-z0-9_]*)(?=[-/.]|[^\w.-]|$)"
)

BLOCKLIST_FILES = (Path("/etc/awb/blocklist.txt"), Path("~/.config/awb/blocklist.txt"))
"""The owner's local blocklists: one regular expression per line, a line starting with # is a comment. The host
file is root owned; the one in the home folder serves the owner before the seal. AWB_BLOCKLIST names more files,
separated by a colon (tests). The work user of a sealed host reads the host file only."""
_BLOCKLIST_CACHE: dict = {}


def blocklist_files() -> list[Path]:
    from awb import config

    if config.is_work_user():
        return [BLOCKLIST_FILES[0]]
    extra = [Path(x) for x in os.environ.get("AWB_BLOCKLIST", "").split(":") if x]
    return [BLOCKLIST_FILES[0], BLOCKLIST_FILES[1].expanduser(), *extra]


def blocklist_re() -> "re.Pattern[str] | None":
    """One pattern of every line of the blocklists, None when there is none. A line that does not compile is a
    GateError that names the file and the line number, never the line."""
    key = []
    for f in blocklist_files():
        try:
            st = f.stat()
            key.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    key = tuple(key)
    if key in _BLOCKLIST_CACHE:
        return _BLOCKLIST_CACHE[key]
    parts: list[str] = []
    for name, _, _ in key:
        try:
            lines = Path(name).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            raise GateError("the blocklist %s cannot be read" % name) from None
        for n, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                re.compile(line)
            except re.error:
                raise GateError("line %d of the blocklist %s is not a regular expression" % (n, name)) from None
            parts.append("(?:%s)" % line)
    rx = re.compile("|".join(parts)) if parts else None
    _BLOCKLIST_CACHE.clear()
    _BLOCKLIST_CACHE[key] = rx
    return rx


_HEX32_RE = re.compile(r"(?<![0-9A-Za-z])(?:0[xX])?([0-9a-fA-F]{32})(?![0-9A-Za-z])")
_UUID_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])"
)
_HEX_GROUP_RE = re.compile(r"[0-9a-fA-F]+")
_TENANT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:tenants?|domain|project|account|mandant)(?:[ _-]?(?:id|no|nr|number))?"
    r"[ \t:=#\"'./-]{0,12}(?P<n>[0-9]{5,6})(?![0-9])(?![.,][0-9])"
)


def _entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _key_is_secret(key: str) -> bool:
    parts = [p for p in re.split(r"[_.:\s-]+", _CAMEL_RE.sub(r"\1_\2", key).lower()) if p]
    if any(p in _SECRET_PARTS for p in parts):
        return True
    joined = "_".join(parts)
    glued = "".join(parts)
    return any(w in joined or w.replace("_", "") in glued for w in _SECRET_WORDS)


def _is_name_shape(v: str) -> bool:
    """A snake, env, dotted or kebab name that is a reference to a setting, not a value: low entropy or no digit."""
    if not any(rx.fullmatch(v) for rx in (_DOTTED_RE, _ENV_NAME_RE, _SNAKE_RE, _KEBAB_RE)):
        return False
    return _entropy(v) < _NAME_SHAPE_ENTROPY or not any(c.isdigit() for c in v)


def _is_placeholder_or_reference(v: str, quoted: bool = False) -> bool:
    if _PLACEHOLDER_RE.search(v) or _REFERENCE_PREFIX_RE.match(v) or "://" in v:
        return True
    if not quoted and (v[0] in "&*%/~.<{[(\\" or "(" in v or ")" in v):
        return True
    if v[0] in "$@!":
        m = _SIGIL_RE.match(v)
        if m is None:
            return not quoted
        if m.end() < len(v):
            return True         # a variable with a path or more text after it
        v = v[1:]               # a bare $NAME: judged by its shape
    return _is_name_shape(v)


def _secret_value(v: str, quoted: bool = False, min_len: int = _MIN_SECRET_LENGTH,
                  min_ent: float = _MIN_SECRET_ENTROPY) -> bool:
    v = v.strip()
    if len(v) < min_len or _entropy(v) < min_ent:
        return False
    if _is_placeholder_or_reference(v, quoted):
        return False
    return not (_detect_token(v) or _PRIVATE_KEY_RE.search(v))   # reported by the more specific class


def _unquote(raw: str) -> tuple[str, bool]:
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1], True
    return raw, False


def _next_line_value(text: str, end: int, heredoc: bool, key_column: int) -> tuple[str, int, bool] | None:
    """The value on the line after a block indicator, a heredoc opener or an empty value: the first non-blank of
    the next lines as (value, offset, quoted), without quotes and a trailing comma. None when that line is not
    one token, is an assignment or a list item of its own or, unless a heredoc, is not indented past the key."""
    pos = text.find("\n", end)
    if pos < 0:
        return None
    pos += 1
    for _ in range(_NEXT_LINES):
        nl = text.find("\n", pos)
        line = text[pos:] if nl < 0 else text[pos:nl]
        stripped = line.strip()
        if stripped:
            if not heredoc and len(line) - len(line.lstrip(" \t")) <= key_column:
                return None
            if stripped.startswith(("- ", "#")) or _NESTED_ASSIGN_RE.match(stripped):
                return None
            value = stripped.rstrip(",").rstrip()
            value, quoted = _unquote(value)
            if not value or re.search(r"\s", value):
                return None
            return value, pos + line.find(value), quoted
        if nl < 0:
            return None
        pos = nl + 1
    return None


def _assignments(text: str) -> Iterator[tuple[str, str, int, bool]]:
    """(key, value, offset of the value, quoted) of every assignment in `text`, also inside a longer value
    (url=...&password=v, Cookie: token=v) and with the value on the line after the key."""
    todo = [(text, 0)]
    while todo:
        chunk, base = todo.pop()
        for m in _ASSIGN_RE.finditer(chunk):
            value, quoted = _unquote(m.group("value"))
            start = m.start("value") + (1 if quoted else 0)
            if not quoted and (value == "" or _BLOCK_INDICATOR_RE.fullmatch(value)):
                line_start = chunk.rfind("\n", 0, m.start()) + 1
                found = _next_line_value(chunk, m.end(), value.startswith("<<"), m.start() - line_start)
                if found is None:
                    continue
                value, start, quoted = found
            yield m.group("key"), value, base + start, quoted
            if "=" in value or ":" in value:
                todo.append((value, base + start))


def _cells(line: str, delimiter: str) -> list[str]:
    return [c.strip() for c in line.split(delimiter)]


def _table_header(line: str) -> tuple[str, int, int] | None:
    """(delimiter, column, cell count) when `line` is the header row of a table with a credential column."""
    for delimiter in _TABLE_DELIMITERS:
        if delimiter not in line:
            continue
        cells = _cells(line, delimiter)
        if len(cells) < 2:
            return None
        labels = [c.strip("\"'") for c in cells]
        for words in (label.split() for label in labels):
            if len(words) > 3 or any(not _TABLE_WORD_RE.fullmatch(w) or w.lower() in _FUNCTION_WORDS for w in words):
                return None
        for i, label in enumerate(labels):
            if label and _key_is_secret("_".join(label.split())):
                return delimiter, i, len(cells)
        return None
    return None


def _detect_table_secrets(text: str) -> list[int]:
    """Values under the credential column of a markdown, CSV, TSV or semicolon table."""
    out: list[int] = []
    header: tuple[str, int, int] | None = None
    pos = 0
    for line in text.split("\n"):
        if header is not None:
            delimiter, column, count = header
            parts = line.split(delimiter)
            if len(parts) != count:
                header = None
            else:
                cell = parts[column].strip()
                value, quoted = _unquote(cell)
                if value and not _TABLE_RULE_RE.fullmatch(value) and not re.search(r"\s", value) \
                        and _secret_value(value, quoted, _MIN_PASSWORD_LENGTH, _MIN_PASSWORD_ENTROPY):
                    out.append(pos + line.find(value))
        if header is None:
            header = _table_header(line)
        pos += len(line) + 1
    return out


def _detect_xml_secrets(text: str) -> list[int]:
    out: list[int] = []
    for m in _XML_ELEMENT_RE.finditer(text):
        if _key_is_secret(m.group("key")) and _secret_value(m.group("value"), True):
            out.append(m.start("value"))
    for m in _XML_PAIR_RE.finditer(text):
        if _key_is_secret(m.group("key")) and _secret_value(m.group("value"), True):
            out.append(m.start("value"))
    return out


def _detect_secret(text: str) -> list[int]:
    out: set[int] = set()
    for key, value, start, quoted in _assignments(text):
        if _key_is_secret(key) and _secret_value(value, quoted):
            out.add(start)
    for rx, min_len, min_ent in _SECRET_SHAPES:
        for m in rx.finditer(text):
            value, quoted = _unquote(m.group("value"))
            if _secret_value(value, quoted, min_len, min_ent):
                out.add(m.start("value") + (1 if quoted else 0))
    out.update(m.start("value") for m in _HASH_LINE_RE.finditer(text))
    out.update(_detect_xml_secrets(text))
    out.update(_detect_table_secrets(text))
    out.update(_detect_keypair(text))
    return sorted(out)


_AK_RE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Z0-9]{0,19}[0-9])(?=[A-Z0-9]{0,19}[A-Z])[A-Z0-9]{20}(?![A-Za-z0-9])")
_SK_RE = re.compile(
    r"(?<![A-Za-z0-9/+=])(?=[A-Za-z0-9/+]{0,39}[a-z])(?=[A-Za-z0-9/+]{0,39}[A-Z])(?=[A-Za-z0-9/+]{0,39}[0-9])"
    r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])"
)
_MIN_SK_ENTROPY = 4.0
_KEYPAIR_LINES = 2
"""The secret of a bare key pair may be this many lines before or after the key id (a blank line or a nested
YAML level between them)."""


def _detect_keypair(text: str) -> list[int]:
    """An access key pair written without a variable name: a key id of 20 capitals and digits with, within two
    lines before or after it, a secret of 40 letters, digits, slashes and plus signs in both cases. A git commit
    id is lower-case hex and never matches."""
    out: list[int] = []
    starts = _line_starts(text)
    for m in _AK_RE.finditer(text):
        line = bisect.bisect_right(starts, m.start()) - 1
        lo = starts[max(0, line - _KEYPAIR_LINES)]
        hi_line = line + _KEYPAIR_LINES + 1
        hi = starts[hi_line] if hi_line < len(starts) else len(text)
        window = text[lo:hi]
        for s in _SK_RE.finditer(window):
            if _entropy(s.group(0)) >= _MIN_SK_ENTROPY:
                out.append(lo + s.start())
                break
    return out


def _detect_private_key(text: str) -> list[int]:
    out = [m.start() for m in _PRIVATE_KEY_RE.finditer(text)]
    if _JWK_KTY in text:
        out.extend(m.start() for m in _JWK_D_RE.finditer(text))
    return sorted(out)


def _detect_token(text: str) -> list[int]:
    return sorted(
        m.start() for rx in _TOKEN_PATTERNS for m in rx.finditer(text) if any(c.isdigit() for c in m.group(1))
    )


def _detect_homepath(text: str) -> list[int]:
    out: list[int] = []
    for m in _HOMEPATH_RE.finditer(text):
        user = next((g for g in m.groups() if g), "")
        if user.lower() in _GENERIC_USERS:
            continue
        out.append(m.start())
    return out


def _detect_blocklist(text: str) -> list[int]:
    rx = blocklist_re()
    return [] if rx is None else [m.start() for m in rx.finditer(text) if m.end() > m.start()]


def _uuid_bounded(text: str, m: re.Match) -> bool:
    """A dash next to the UUID must not continue a hex group (a longer dashed hex id is not a UUID)."""
    before = text[max(0, m.start() - 40):m.start()]
    if before.endswith("-"):
        g = re.search(r"[0-9A-Za-z]+-$", before)
        if g and _HEX_GROUP_RE.fullmatch(g.group(0)[:-1]):
            return False
    if text.startswith("-", m.end()):
        g = re.compile(r"[0-9A-Za-z]+").match(text, m.end() + 1)
        if g and _HEX_GROUP_RE.fullmatch(g.group(0)):
            return False
    return True


def _detect_identifier(text: str) -> list[int]:
    out = [m.start() for m in _HEX32_RE.finditer(text) if len(set(m.group(1).lower())) > 1]
    out.extend(m.start() for m in _UUID_RE.finditer(text)
               if len(set(m.group(0).replace("-", ""))) > 1 and _uuid_bounded(text, m))
    out.extend(m.start("n") for m in _TENANT_RE.finditer(text))
    return out


DETECTORS: dict[str, Callable[[str], list[int]]] = {
    "secret": _detect_secret,
    "private-key": _detect_private_key,
    "token": _detect_token,
    "homepath": _detect_homepath,
    "blocklist": _detect_blocklist,
    "identifier": _detect_identifier,
}
"""Pattern detectors by class. The name class needs the register and is handled by `_detect_names`."""


_norm_cache: list = [None, None]


def _normalized(text: str) -> normalize.Normalized:
    """The normalised form of `text`, kept for the next call with the same text (one file, several passes)."""
    if _norm_cache[0] is not text and _norm_cache[0] != text:
        _norm_cache[0] = text
        _norm_cache[1] = normalize.normalize(text)
    return _norm_cache[1]


def _name_spans(text: str, n: normalize.Normalized, matcher) -> list[Span]:
    """Name spans in the normalised text `n` of `text`: from a Matcher here or from a check.RemoteCheck (which
    normalises `text` itself and answers positions in the same normalised text)."""
    if isinstance(matcher, Matcher):
        return matcher.find(n.text)
    return matcher.name_spans(text)


def _detect_names(text: str, matcher) -> list[int]:
    """Offsets in `text` of every registered form, found in the normalised text and mapped back."""
    n = _normalized(text)
    return [normalize.original_span(n, s.start, s.end)[0] for s in _name_spans(text, n, matcher)]


def _detect_all(text: str, matcher) -> list[tuple[str, int]]:
    """(class, offset in `text`) of every hit: the name class over the normalised text, every pattern detector
    over the text as given and, when it differs, over the normalised text with the offsets mapped back."""
    out: list[tuple[str, int]] = []
    if matcher is not None:
        out.extend(("name", off) for off in _detect_names(text, matcher))
    n = _normalized(text)
    for cls, detect in list(DETECTORS.items()):
        out.extend((cls, off) for off in detect(text))
        if n.text != text:
            out.extend((cls, normalize.original_span(n, off, off + 1)[0]) for off in detect(n.text))
    return out


# --------------------------------------------------------------------------- scanning


def _line_starts(text: str) -> list[int]:
    return [0] + [m.end() for m in re.finditer("\n", text)]


def _order(f: Finding) -> tuple:
    return (f.line, CLASSES.index(f.cls) if f.cls in CLASSES else len(CLASSES), f.cls)


def _scan_text(label: str, text: str, matcher: Matcher | None) -> list[Finding]:
    """Findings of one decoded text, one per line and class. What a base64 or hex block of the text decodes to
    is scanned as well; its findings are on the line of the block."""
    starts = _line_starts(text)
    hits: set[tuple[int, str]] = set()
    for cls, off in _detect_all(text, matcher):
        hits.add((bisect.bisect_right(starts, off), cls))
    blocks = extract_text.encoded_blocks(text)
    if blocks:
        joined = "\n".join(decoded for _, decoded in blocks)
        block_starts: list[int] = []
        pos = 0
        for _, decoded in blocks:
            block_starts.append(pos)
            pos += len(decoded) + 1
        for cls, off in _detect_all(joined, matcher):
            block = max(0, bisect.bisect_right(block_starts, off) - 1)
            hits.add((bisect.bisect_right(starts, blocks[block][0]), cls))
    return sorted((Finding(label, line, cls) for line, cls in hits), key=_order)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _is_opaque(label: str, data: bytes) -> bool:
    return (b"\x00" in data[:BINARY_PROBE] or data.startswith(_OPAQUE_MAGICS)
            or Path(label).suffix.lower() in _OPAQUE_SUFFIXES)


def _scan_bytes(label: str, data: bytes, matcher: Matcher | None) -> list[Finding]:
    if _is_opaque(label, data):
        return [Finding(label, 0, "opaque")]
    return _scan_text(label, _decode(data), matcher)


RATE_WAIT = check.RATE_WAIT


def _load_matcher(register_path: Path | None, rate_wait: float = 0.0):
    """None without a register path. A readable register gives a Matcher, a folder without any register an
    empty Matcher, a register this user cannot read or an encrypted one a check.RemoteCheck. RegisterError is
    raised for a broken register; CheckUnavailable comes later, from the first name check that cannot run."""
    if register_path is None:
        return None
    kind, entries = check.register_source(Path(register_path))
    if kind == check.LOCAL:
        return Matcher(register.forms_for_matching(entries))
    if kind == check.MISSING:
        return Matcher([])
    return check.RemoteCheck(check.remote_socket(), rate_wait)


def _scan_paths(files: Iterable[Path], matcher: Matcher | None) -> list[Finding]:
    out: list[Finding] = []
    for f in files:
        f = Path(f)
        found: list[Finding] = []
        if matcher is not None and _detect_names(str(f), matcher):
            found.append(Finding(str(f), 0, "name"))
        try:
            data = f.read_bytes()
        except OSError:
            found.append(Finding(str(f), 0, "opaque"))
            out.extend(sorted(found, key=_order))
            continue
        found.extend(_scan_bytes(str(f), data, matcher))
        out.extend(sorted(set(found), key=_order))
    return out


MESSAGE_LABEL = "commit message"
_SCISSORS = ">8"


def message_text(raw: str) -> str:
    """A commit message as git will keep it: comment lines emptied (the line count stays) and everything from the
    scissors line on dropped, so that the diff of `git commit -v` is not scanned as the message."""
    out: list[str] = []
    for line in raw.split("\n"):
        if line.startswith("#"):
            if _SCISSORS in line:
                break
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


_ATTRIBUTION_RE = re.compile(
    r"(?im)^[ \t]*co-authored-by:[^\n]*(?:claude|anthropic)|generated with \[?claude code|noreply@anthropic\.com")
"""A commit that names Claude as its co-author or generator: the hosting service would show Claude as a
contributor. The owner alone is the author of this repository and of every project."""


def _attribution(label: str, text: str) -> list[Finding]:
    starts = _line_starts(text)
    return sorted({Finding(label, bisect.bisect_right(starts, m.start()), "attribution")
                   for m in _ATTRIBUTION_RE.finditer(text)}, key=_order)


def scan_message(path: Path, matcher: Matcher | None) -> list[Finding]:
    """Findings of a commit message file (the commit-msg hook gives its path), labelled "commit message". A
    co-author or generator line that names Claude is a finding of the class attribution."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return [Finding(MESSAGE_LABEL, 0, "opaque")]
    text = message_text(_decode(data))
    return sorted(_scan_text(MESSAGE_LABEL, text, matcher) + _attribution(MESSAGE_LABEL, text), key=_order)


def _merged(spans: list[Span]) -> list[Span]:
    out: list[Span] = []
    for s in sorted(spans, key=lambda s: (s.start, s.end)):
        if out and s.start < out[-1].end:
            out[-1] = Span(out[-1].start, max(out[-1].end, s.end), out[-1].cls)
        else:
            out.append(Span(s.start, s.end, s.cls))
    return out


def masked_path(path: str, matcher) -> str:
    """`path` for printing: every registered form in it replaced by [name]."""
    if matcher is None:
        return path
    n = normalize.normalize(path)
    spans = _name_spans(path, n, matcher)
    if not spans:
        return path
    size = len(n.text)
    spans = _merged([Span(min(s.start, size), min(s.end, size), s.cls) for s in spans])
    return Matcher([]).replace(n.text, spans, lambda s: "[name]")


def scan_files(files: list[Path], register_path: Path | None) -> list[Finding]:
    """Findings of every file, in the order given. A binary or unreadable file is one `opaque` finding."""
    return _scan_paths(files, _load_matcher(register_path))


# --------------------------------------------------------------------------- git


GIT_TIMEOUT = 60
"""Seconds one git call may take before the gate gives up (a hung hook refuses the commit)."""


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False,
                           timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GateError("git %s timed out after %d seconds" % (args[0], GIT_TIMEOUT)) from None
    except OSError as err:
        raise GateError("git cannot be run (%s)" % type(err).__name__) from None
    if r.returncode != 0:
        raise GateError("git %s failed with exit code %d" % (args[0], r.returncode))
    return r.stdout


def _toplevel(repo: Path) -> Path:
    out = _git(Path(repo), "rev-parse", "--show-toplevel").strip()
    return Path(os.fsdecode(out))


def _raw_entries(out: bytes) -> list[tuple[str, str, str]]:
    """(path, new mode, new blob id) from the -z --raw output of git diff, without repeats."""
    parts = out.split(b"\0")
    entries: list[tuple[str, str, str]] = []
    i = 0
    while i + 1 < len(parts):
        meta = parts[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        fields = meta[1:].split()
        if len(fields) >= 4:
            entry = (os.fsdecode(parts[i + 1]), fields[1].decode("ascii"), fields[3].decode("ascii"))
            if entry not in entries:
                entries.append(entry)
        i += 2
    return entries


def _staged_entries(top: Path) -> list[tuple[str, str, str]]:
    """(path relative to the top, new mode, new blob id) of every staged added, modified or retyped file."""
    return _raw_entries(_git(top, "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames",
                             "--diff-filter=AMT"))


def staged_files(repo: Path) -> list[Path]:
    """Staged added or modified files of the repository (submodules left out), as paths in the work tree."""
    top = _toplevel(Path(repo))
    return [top / rel for rel, mode, _ in _staged_entries(top) if mode != "160000"]


def _read_objects(top: Path, ids: list[str], kind: bytes = b"blob") -> dict[str, bytes]:
    """The content of every object of `kind` among `ids` (a missing id or another kind is left out)."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    out = _git(top, "cat-file", "--batch", stdin=("\n".join(ids) + "\n").encode("ascii"))
    objects: dict[str, bytes] = {}
    pos = 0
    for object_id in ids:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].split()
        pos = nl + 1
        if len(header) < 3:
            continue
        size = int(header[2])
        if header[1] == kind:
            objects[object_id] = out[pos:pos + size]
        pos += size + 1
    return objects


def _read_blobs(top: Path, ids: list[str]) -> dict[str, bytes]:
    return _read_objects(top, ids, b"blob")


def _object_types(top: Path, ids: list[str]) -> dict[str, str]:
    """The type of every object among `ids` ("missing" for one that is not here)."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    out = _git(top, "cat-file", "--batch-check", stdin=("\n".join(ids) + "\n").encode("ascii"))
    types: dict[str, str] = {}
    for line in out.decode("ascii", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            types[fields[0]] = fields[1]
    return types


def _scan_staged(repo: Path, matcher: Matcher | None) -> list[Finding]:
    """Scan what the commit will carry: the staged content from the index and the staged path names."""
    top = _toplevel(Path(repo))
    entries = [e for e in _staged_entries(top) if e[1] != "160000"]
    blobs = _read_blobs(top, [blob_id for _, _, blob_id in entries])
    out: list[Finding] = []
    for rel, _, blob_id in entries:
        found = {Finding(rel, 0, f.cls) for f in _scan_text(rel, rel, matcher)}
        data = blobs.get(blob_id)
        if data is None:
            found.add(Finding(rel, 0, "opaque"))
        else:
            found.update(_scan_bytes(rel, data, matcher))
        out.extend(sorted(found, key=_order))
    return out


# --------------------------------------------------------------------------- selftest

_SELFTEST_BLOCKED = "selftest" + "-blocked"
"""A word the selftest puts on a blocklist of its own, so that the class is proven with or without the owner's list."""
_SELFTEST_CODE = "ORG-SELF"
_SELFTEST_FORM = "Qwxzv Probefirma"
"""An invented form, also listed in tests/fixtures.py. It is planted only into the selftest's own folder."""


def _selftest_cases() -> dict[str, bytes]:
    """One planted case per class on line 2. Built from pieces so that this source does not match itself."""
    first = "selftest case\n"
    cases = {
        "name": "offer draft for %s, second round\n" % _SELFTEST_FORM,
        "secret": "db_pass" + "word = \"" + "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc" + "\"\n",
        "private-key": "-----BEGIN " + "OPENSSH PRI" + "VATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
        + "-----END " + "OPENSSH PRI" + "VATE KEY-----\n",
        "token": "remote push uses " + "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi" + "\n",
        "homepath": "log file under /ho" + "me/builder/awb.log\n",
        "blocklist": "copied from the " + _SELFTEST_BLOCKED + " folder\n",
        "identifier": "project id " + "4f1c9a7e2b6d" + "40f8a3c5e9b1d7f2a6c0" + "\n",
    }
    out = {cls: (first + body).encode("utf-8") for cls, body in cases.items()}
    out["opaque"] = b"\x00\x01\x02 binary \x00"
    return out


_SELFTEST_CLEAN = "release 1.4.2 of 2026-09-22 for CUST-Q7M4 in tcp-q7m4, see the notes\nversion v0.1.0\n"


def _selftest_message() -> tuple[str, list[tuple[str, int]]]:
    """A planted commit message and what the gate must find in it: a name on line 3 and a home path on line 4,
    nothing on the comment line 5 (git drops it) and nothing after the scissors line."""
    message = ("second round\n\nthe offer for %s\nlog under /ho" % _SELFTEST_FORM + "me/builder/awb.log\n"
               + "# On branch offer-%s\n# ------------------------ >8 ------------------------\n"
               % _SELFTEST_FORM.split()[0].lower()
               + "diff --git a/x b/x\n+push with " + "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi\n")
    return message, [("name", 3), ("homepath", 4)]


_SELFTEST_CLEAN_MESSAGE = "release notes\n\nfor CUST-Q7M4 in tcp-q7m4, version v0.1.0\n"


def selftest(register_path: Path | None = None) -> list[str]:
    """Plant one case per class in a private temporary folder and check that exactly that class is found on
    the planted line, then check that a clean file gives nothing, then the same for a commit message. Returns
    the failures, empty when all pass.

    The name case uses a temporary register with an invented form, so the selftest never plants a real form.
    A register at `register_path`, when it exists, is only loaded to prove that it is well formed.
    """
    failures: list[str] = []
    if register_path is not None:
        try:
            check.register_source(Path(register_path))
        except register.RegisterError as err:
            failures.append("the register does not load: %s" % err)
    with tempfile.TemporaryDirectory(prefix="awb-gate-selftest-") as tmp:
        root = Path(tmp)
        own = root / "blocklist.txt"
        own.write_text(_SELFTEST_BLOCKED + "\n", encoding="utf-8")
        before = os.environ.get("AWB_BLOCKLIST")
        os.environ["AWB_BLOCKLIST"] = ":".join(x for x in (before, str(own)) if x)
        try:
            return failures + _selftest_in(root)
        finally:
            if before is None:
                os.environ.pop("AWB_BLOCKLIST", None)
            else:
                os.environ["AWB_BLOCKLIST"] = before


def _selftest_in(root: Path) -> list[str]:
    failures: list[str] = []
    if True:
        reg = root / "register.tsv"
        register.save(reg, [register.Entry(_SELFTEST_CODE, "ORG", _SELFTEST_FORM, "2026-09-22", "active")])
        for cls, content in _selftest_cases().items():
            f = root / ("case-%s.txt" % cls)
            f.write_bytes(content)
            got = scan_files([f], reg)
            want_line = 0 if cls == "opaque" else 2
            if [(x.cls, x.line) for x in got] != [(cls, want_line)]:
                found = ", ".join("%s on line %d" % (x.cls, x.line) for x in got) or "nothing"
                failures.append("%s: expected one %s finding on line %d, found %s" % (cls, cls, want_line, found))
        clean = root / "case-clean.txt"
        clean.write_text(_SELFTEST_CLEAN, encoding="utf-8")
        got = scan_files([clean], reg)
        if got:
            failures.append("clean: expected nothing, found %s" % ", ".join(sorted({x.cls for x in got})))
        if failures:
            return failures
        # the commit message path, proven with classes that passed above: comment lines and the part after the
        # scissors line are not the message
        matcher = _load_matcher(reg)
        message, want = _selftest_message()
        msg = root / "COMMIT_EDITMSG"
        msg.write_text(message, encoding="utf-8")
        got = scan_message(msg, matcher)
        if [(x.cls, x.line) for x in got] != want:
            found = ", ".join("%s on line %d" % (x.cls, x.line) for x in got) or "nothing"
            failures.append("message: expected %s, found %s"
                            % (", ".join("%s on line %d" % w for w in want), found))
        msg.write_text(_SELFTEST_CLEAN_MESSAGE, encoding="utf-8")
        got = scan_message(msg, matcher)
        if got:
            failures.append("clean message: expected nothing, found %s" % ", ".join(sorted({x.cls for x in got})))
    return failures


# --------------------------------------------------------------------------- the commit hook of other repositories


INSTALLED_AWB = Path("/usr/local/bin/awb")
"""The command the seal installs for the work user (seal/setup.sh). An installed hook prefers it at run time."""
HOOK_MARK = "# awb commit gate, written by awb gate --install"
"""The line that tells a hook written here from a hook of another tool."""


_ZERO_SHA_RE = re.compile(r"^0+$")


def parse_push_lines(text: str) -> list[tuple[str, str, str, str]]:
    """(local ref, local sha, remote ref, remote sha) of the lines git gives a pre-push hook on standard input."""
    out: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x) for x in (parts[1], parts[3])):
            out.append((parts[0], parts[1], parts[2], parts[3]))
    return out


def _commit_entries(top: Path, commit: str) -> list[tuple[str, str, str]]:
    """(path, new mode, new blob id) of every file a commit adds, changes or retypes (the root commit: all). A
    merge commit is diffed against each of its parents (-m), so a conflict resolved with a secret is seen."""
    return _raw_entries(_git(top, "diff-tree", "--root", "-r", "-m", "--no-commit-id", "--raw", "-z",
                             "--no-abbrev", "--no-renames", "--diff-filter=AMT", commit))


def pushed_commits(repo: Path, updates: list[tuple[str, str, str, str]], remote: str | None = None) -> list[str]:
    """Every commit a push carries that the remote does not have: remote..local for an update, and for a new
    branch every commit that no remote-tracking branch of `remote` (of any remote when it is not known)
    reaches. A deletion carries none."""
    top = _toplevel(Path(repo))
    commits: list[str] = []
    for _, local, _, remote_sha in updates:
        if _ZERO_SHA_RE.match(local):
            continue
        out = None
        if not _ZERO_SHA_RE.match(remote_sha):
            try:
                out = _git(top, "rev-list", "%s..%s" % (remote_sha, local))
            except GateError:
                out = None          # the remote commit is not here (a forced push): fall back below
        if out is None:
            out = _git(top, "rev-list", local, "--not", "--remotes=%s" % remote if remote else "--remotes")
        commits.extend(os.fsdecode(out).split())
    return list(dict.fromkeys(commits))


def _commit_texts(top: Path, commits: list[str]) -> dict[str, str]:
    """Author name and mail, committer name and mail and the message of every commit, as one text each."""
    if not commits:
        return {}
    out = _git(top, "log", "--no-walk=unsorted", "--stdin", "-z", "--format=%H%n%an%n%ae%n%cn%n%ce%n%B",
               stdin=("\n".join(commits) + "\n").encode("ascii"))
    texts: dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry.strip():
            continue
        commit, _, body = _decode(entry).partition("\n")
        texts[commit.strip()] = body
    return texts


def _scan_pushed(repo: Path, updates: list[tuple[str, str, str, str]], matcher: Matcher | None,
                 remote: str | None = None) -> list[Finding]:
    """Scan what a push carries: every file of every pushed commit, as that commit has it (a secret that one
    commit adds and a later one removes is still in the history that the push publishes), the author, the
    committer and the message of every pushed commit ("commit <id>", line 0), the name of every pushed ref and,
    for an annotated tag, its tagger and message ("ref <name>", line 0)."""
    top = _toplevel(Path(repo))
    out: list[Finding] = []
    seen_refs: set[str] = set()
    tag_candidates: dict[str, str] = {}
    for local_ref, local, remote_ref, _ in updates:
        for name in (local_ref, remote_ref):
            if name and not name.startswith("(") and name not in seen_refs:
                seen_refs.add(name)
                out.extend(sorted({Finding("ref " + name, 0, f.cls) for f in _scan_text(name, name, matcher)},
                                  key=_order))
        if not _ZERO_SHA_RE.match(local):
            tag_candidates.setdefault(local, local_ref)
    types = _object_types(top, list(tag_candidates))
    tags = _read_objects(top, [i for i, t in types.items() if t == "tag"], b"tag")
    for object_id, data in tags.items():
        label = "ref " + tag_candidates[object_id]
        out.extend(sorted({Finding(label, 0, f.cls) for f in _scan_text(label, _decode(data), matcher)}, key=_order))
    commits = pushed_commits(top, updates, remote)
    for commit, text in _commit_texts(top, commits).items():
        label = "commit " + commit[:10]
        out.extend(sorted({Finding(label, 0, f.cls) for f in _scan_text(label, text, matcher) + _attribution(label, text)},
                          key=_order))
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for commit in commits:
        for rel, mode, blob_id in _commit_entries(top, commit):
            if mode == "160000" or blob_id in seen:
                continue
            seen.add(blob_id)
            entries.append((rel, blob_id, commit[:10]))
    blobs = _read_blobs(top, [blob_id for _, blob_id, _ in entries])
    for rel, blob_id, commit in entries:
        label = "%s:%s" % (commit, rel)
        found = {Finding(label, 0, f.cls) for f in _scan_text(rel, rel, matcher)}
        data = blobs.get(blob_id)
        if data is None:
            found.add(Finding(label, 0, "opaque"))
        else:
            found.update(Finding(label, f.line, f.cls) for f in _scan_bytes(rel, data, matcher))
        out.extend(sorted(found, key=_order))
    return out


def staged_blobs(repo: Path) -> list[tuple[str, bytes | None]]:
    """(path relative to the top, staged content) of every staged added or modified file, submodules left out.
    The content is None when git gave no blob for it."""
    top = _toplevel(Path(repo))
    entries = [e for e in _staged_entries(top) if e[1] != "160000"]
    blobs = _read_blobs(top, [blob_id for _, _, blob_id in entries])
    return [(rel, blobs.get(blob_id)) for rel, _, blob_id in entries]


def hook_fallback() -> str:
    """How an installed hook runs the Workbench where the seal's awb is missing: this interpreter with -m awb.
    The hook lives under .git/hooks, which is never committed, so an interpreter path under home is fine there."""
    return "%s -m awb" % shlex.quote(sys.executable)


def _hook_lines(fallback: str | None, installed: Path, what: str, refused: str) -> list[str]:
    fallback = fallback or hook_fallback()
    seal = shlex.quote(str(installed))
    return [
        "#!/bin/sh",
        HOOK_MARK,
        "# The self-test of the gate first, then the gate over %s." % what,
        "# Any failure refuses the %s. The awb of the seal wins when it is there." % refused,
        "if [ -x %s ]; then set -- %s; else set -- %s; fi" % (seal, seal, fallback),
        'refuse() { echo "awb gate: %s refused, $1." >&2; exit 1; }' % refused,
        '"$@" gate --selftest >/dev/null 2>&1 || refuse "the gate failed its self-test"',
    ]


def hook_script(fallback: str | None = None, *, kb: bool = False, installed: Path = INSTALLED_AWB) -> str:
    """The text of a pre-commit hook: the self-test of the gate first (R-004), then the gate over the staged
    content and, for the knowledge base, `awb kb verify --staged`. Any failure refuses the commit."""
    what = "what the commit carries%s" % (", then the checks of awb kb add over every staged entry" if kb else "")
    lines = _hook_lines(fallback, installed, what, "commit")
    lines.append('"$@" gate --staged || refuse "see the findings above"')
    if kb:
        lines.append('"$@" kb verify --staged || refuse "a staged entry does not pass the checks of awb kb add"')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def message_hook_script(fallback: str | None = None, *, installed: Path = INSTALLED_AWB) -> str:
    """The text of a commit-msg hook: the self-test of the gate first, then the gate over the commit message
    (git gives its file as the first argument). Any failure refuses the commit."""
    base = _hook_lines(fallback, installed, "the commit message (its file is $1)", "commit")
    lines = base[:2] + ['msg="$1"'] + base[2:]
    lines.append('"$@" gate --message "$msg" || refuse "the commit message carries a finding, see above"')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def push_hook_script(fallback: str | None = None, *, installed: Path = INSTALLED_AWB) -> str:
    """The text of a pre-push hook: the self-test of the gate first, then the gate over every file, author,
    committer and message of every commit the push carries and over the pushed ref names (git passes the
    updates on standard input and the remote as the first argument). Any failure refuses the push."""
    base = _hook_lines(fallback, installed, "every file, author, message and ref of every commit the push carries",
                       "push")
    lines = base[:2] + ['remote="$1"'] + base[2:]
    lines.append('top=$(git rev-parse --show-toplevel) || refuse "no repository"')
    lines.append('"$@" gate --pushed --repo "$top" --remote "$remote" || refuse "see the findings above"')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


HOOK_NAMES = ("pre-commit", "commit-msg", "pre-push")


def install_hook(repo: Path, *, kb: bool = False, force: bool = False, fallback: str | None = None,
                 installed: Path = INSTALLED_AWB) -> Path:
    """Write the pre-commit hook of `repo` (with `kb`, the one of the knowledge base) and next to it the
    commit-msg and the pre-push hook, and return the path of the pre-commit hook.

    Refused: a repository that runs its hooks from core.hooksPath (its hook is a tracked file, like the one of the
    Workbench itself) and, unless `force`, a hook of another tool under any of the three names."""
    top = _toplevel(Path(repo))
    try:
        r = subprocess.run(["git", "-C", str(top), "config", "--get", "core.hooksPath"], capture_output=True,
                           check=False, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        raise GateError("git cannot be run (%s)" % type(err).__name__) from None
    if r.returncode == 0 and r.stdout.strip():
        raise GateError("this repository runs its hooks from core.hooksPath; its hook is a tracked file, "
                        "change it there")
    hooks_dir = Path(os.fsdecode(_git(top, "rev-parse", "--git-path", "hooks").strip()))
    if not hooks_dir.is_absolute():
        hooks_dir = top / hooks_dir
    scripts = {
        "pre-commit": hook_script(fallback, kb=kb, installed=installed),
        "commit-msg": message_hook_script(fallback, installed=installed),
        "pre-push": push_hook_script(fallback, installed=installed),
    }
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        if hook.exists() and not force:
            try:
                current = hook.read_text(encoding="utf-8", errors="replace")
            except OSError:
                current = ""
            if HOOK_MARK not in current:
                raise GateError("a %s hook of another tool is already there; give --force to replace it" % name)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        hook.write_text(scripts[name], encoding="utf-8")
        os.chmod(hook, 0o755)
    return hooks_dir / "pre-commit"


# --------------------------------------------------------------------------- command line

_SKIP_DIRS = frozenset((".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"))


def _expand(paths: Iterable[Path]) -> list[Path]:
    """Files as given, folders walked (version control, virtual environments and caches left out)."""
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if not p.is_dir():
            out.append(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.endswith(".egg-info"))
            out.extend(Path(root) / name for name in sorted(files))
    return out


def main(argv: list[str] | None = None) -> int:
    """`awb gate [--staged] [--pushed [--remote NAME]] [--message FILE] [--selftest] [--register PATH]
    [--repo PATH] [PATH...]`. With `awb gate --install [--kb] [--force] [--repo PATH]` it writes the commit,
    commit-msg and push hooks of another repository.

    Prints `cls  file:line` per finding. Exit 0 clean, 1 on findings or a failed selftest, 2 on an error.
    """
    from awb.cli import SafeParser

    ap = SafeParser(
        prog="awb gate",
        description="Refuse names, secrets, home paths, blocked words and ids. Prints class and file:line only.",
    )
    ap.add_argument("--staged", action="store_true", help="scan the staged content of the repository")
    ap.add_argument("--pushed", action="store_true",
                    help="scan every file of every commit a push carries (the pre-push lines on standard input)")
    ap.add_argument("--remote", default=None,
                    help="with --pushed: the remote the push goes to (a new branch is new against it only)")
    ap.add_argument("--message", type=Path, default=None,
                    help="scan a commit message file (the commit-msg hook gives it)")
    ap.add_argument("--selftest", action="store_true", help="prove that every class can be found")
    ap.add_argument("--register", type=Path, default=None, help="register file (default: the vault register)")
    ap.add_argument("--no-register", action="store_true",
                    help="run without a register: every class but name is checked")
    ap.add_argument("--repo", type=Path, default=None,
                    help="repository for --staged and --install (default: the current one)")
    ap.add_argument("--install", action="store_true",
                    help="write the commit hook of the repository: self-test first, then --staged")
    ap.add_argument("--kb", action="store_true", help="with --install: also run awb kb verify --staged")
    ap.add_argument("--force", action="store_true", help="with --install: replace a hook of another tool")
    ap.add_argument("paths", nargs="*", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.install:
        try:
            hook = install_hook(args.repo if args.repo is not None else Path.cwd(), kb=args.kb, force=args.force)
        except GateError as err:
            print("awb gate: %s" % err, file=sys.stderr)
            return 2
        print("awb gate: the commit hook is installed: %s" % hook)
        return 0
    if not (args.staged or args.pushed or args.message or args.selftest or args.paths):
        print("awb gate: give --staged, --pushed, --message FILE, --selftest, --install or at least one path",
              file=sys.stderr)
        return 2
    updates = parse_push_lines(sys.stdin.read()) if args.pushed else []

    register_path = args.register
    if register_path is None:
        from awb import config

        register_path = config.paths().register

    if args.selftest:
        failures = selftest(register_path)
        for f in failures:
            print("selftest failed: %s" % f)
        if failures:
            return 1
        print("selftest passed: %d cases" % (len(_selftest_cases()) + 3))
        if not (args.staged or args.pushed or args.message or args.paths):
            return 0

    findings: list[Finding] = []
    try:
        if args.no_register:
            register_path = None
        elif check.register_source(Path(register_path))[0] == check.MISSING:
            print("awb gate: no register found, names cannot be checked; give --register PATH or --no-register "
                  "to run without the name class", file=sys.stderr)
            return 2
        matcher = _load_matcher(register_path, rate_wait=RATE_WAIT)
        if isinstance(matcher, check.RemoteCheck):
            matcher.ping()
        if args.staged:
            findings.extend(_scan_staged(args.repo if args.repo is not None else Path.cwd(), matcher))
        if args.pushed:
            findings.extend(_scan_pushed(args.repo if args.repo is not None else Path.cwd(), updates, matcher,
                                         args.remote or None))
        if args.message:
            findings.extend(scan_message(args.message, matcher))
        if args.paths:
            findings.extend(_scan_paths(_expand(args.paths), matcher))
        lines = ["%s  %s:%d" % (f.cls, masked_path(f.file, matcher), f.line) for f in findings]
    except (GateError, register.RegisterError) as err:   # check.CheckUnavailable is a RegisterError
        print("awb gate: %s" % err, file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
