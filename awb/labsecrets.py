"""The secrets of a log of our own lab runs (task GT, his decision of 2026-10-27).

A project keeps full live examples of API calls and responses from the lab tenants: request bodies, full response
bodies and every id (domains, projects, users, groups, resources, request ids, the header and the claims of a
JWT). Only these are touched:

    password, login        replaced with [secret] and [login]
    temporary credential   an IAM token, a JWT, a security token, a signature, a bearer value and the access and
                           secret key of a temporary credential (a text that carries a security token) are
                           shortened to their first 8 characters, "..." and the length: `MIIEhgYJ...(4321 chars)`
    permanent key          an access or secret key without a security token beside it is never written: [secret]
    private key            [private key]
    anything else the gate still takes for a secret, a private key or a token: the value becomes [secret] (a
    token is shortened); a line where that fails becomes [secret] as a whole

A shortened value carries "...", which the gate reads as a placeholder, so the commit gate passes the file.
Standard library only. Nothing here prints, logs or raises with a value.
"""
from __future__ import annotations

import re
from collections import Counter

SECRET = "[secret]"
LOGIN = "[login]"
PRIVATE_KEY = "[private key]"
KEEP = 8
GATE_CLASSES = ("secret", "private-key", "token")

_VALUE = r"""(?P<q>["']?)(?P<value>[^\s"'`,;<>(){}\[\]]+)"""
_SEP = r"""["']?\s*[:=]\s*"""
_LONG = r"""(?P<q>["']?)(?P<value>[A-Za-z0-9+/=_.~-]{16,})"""
"""A key-shaped value: a token, a key or a signature is at least 16 characters long."""
_TEMP_KEY_RE = re.compile(
    r"(?i)(?<![\w-])(?P<key>[\"']?(?:x-subject-token|x-auth-token|x-security-token|security[_-]?token|"
    r"session[_-]?token|access[_-]?token|id[_-]?token|refresh[_-]?token|auth[_-]?token|token|signature)"
    + _SEP + r")" + _LONG)
_BEARER_RE = re.compile(r"(?i)(?P<key>\bbearer\s+)(?P<q>)(?P<value>[A-Za-z0-9._~+/=-]{12,})")
_JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_IAM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])MII[A-Za-z0-9+/=_-]{60,}")
_KEY_RE = re.compile(
    r"(?i)(?<![\w-])(?P<key>[\"']?(?:access|secret|ak|sk|access[_-]?key(?:[_-]?id)?|accesskey(?:id)?|"
    r"secret[_-]?(?:access[_-]?)?key|secretkey)" + _SEP + r")" + _LONG)
_SIGNED_ACCESS_RE = re.compile(r"(?P<key>\bAccess=)(?P<q>)(?P<value>[A-Za-z0-9]{12,})")
_PASSWORD_RE = re.compile(
    r"(?i)(?<![\w-])(?P<key>[\"']?(?:[\w.-]*[_.-])?(?:pass(?:word|wd|wort|phrase)?|pwd|kennwort)" + _SEP + r")"
    + _VALUE)
_LOGIN_RE = re.compile(
    r"(?i)(?<![\w-])(?P<key>[\"']?(?:login(?:[_-]?name)?|user[_-]?name|username)" + _SEP + r")" + _VALUE)
_AUTH_USER_RE = re.compile(r"""(?P<key>"user"\s*:\s*\{[^{}]*?"name"\s*:\s*")(?P<q>)(?P<value>[^"\n]+)(?=")""")
"""The name of the user in an IAM body ({"user": {"name": ...}}): the login of a password or a user call."""
_REFERENCE_RE = re.compile(r"^(?:var|local|module|data|each|self|path|count)\.[\w.\[\]-]+$")
"""A reference of Terraform (var.admin_password), never a value."""
_TEMP_HINT_RE = re.compile(r"(?i)security[_-]?token|x-security-token|sessiontoken|session_token")
_PRIVATE_BLOCK_RE = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----.*?-----END \1-----", re.S)
_PRIVATE_OPEN_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*\Z")
_PLACEHOLDER_RE = re.compile(r"(?i)^(?:\[[a-z ]+\]|\*+|x+|<[^<>]*>|\$\{?[\w.]+\}?|\{\{.*\}\}|null|none|true|false|"
                             r"redacted|changeme|-+|\.+)$|\.\.\.")
_VALUE_AT_RE = re.compile(r"[^\s\"'`,;<>(){}\[\]]+")
_MAX_ROUNDS = 500


_DOTTED_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_VARIABLE_RE = re.compile(r"[a-z_]+")


def code_value(sep: str, quoted: bool, value: str, after: str, variables: bool = True) -> bool:
    """A password or login field of code, not a value (task T7, the rules of the gate's prose_password_spans): an
    unquoted call, index or format field (`getpass.getpass(...)`, `os.environ["X"]`, `{password}`), and after `=`
    an attribute (`args.password`) or, with `variables`, a lower case variable name (`password=password`). A quoted
    value, a value after `:` and a value with a digit or a symbol stay values. `after` is the text right after the
    value."""
    if quoted:
        return False
    call = re.fullmatch(r"[A-Za-z_][\w.]*[(\[]", value)
    if call or after[:1] in ("(", "[") or re.match(r"\{[A-Za-z_]\w*(?:[.!:][^}]*)?\}?$", value):
        return True             # a call, an index or a format field ({password})
    if "=" not in sep:
        return False
    return bool(_DOTTED_RE.fullmatch(value) or (variables and _VARIABLE_RE.fullmatch(value)))


def _code_value(m: re.Match) -> bool:
    return code_value(m.group("key"), bool(m.group("q")), m.group("value"), m.string[m.end():m.end() + 1])


def short(value: str) -> str:
    """The first 8 characters, "..." and the length."""
    return "%s...(%d chars)" % (value[:KEEP], len(value))


def _kept(value: str) -> bool:
    """A value that is no secret to touch: a placeholder, a reference, a value shortened before."""
    v = value.strip()
    return len(v) < 3 or bool(_PLACEHOLDER_RE.search(v)) or bool(_REFERENCE_RE.match(v))


def shorten(text: str) -> tuple[str, Counter]:
    """`text` with its passwords and logins masked, its temporary credentials shortened and its permanent keys and
    private keys replaced and the counts per kind (never a value)."""
    counts: Counter = Counter()
    temporary = bool(_TEMP_HINT_RE.search(text))

    def private(m):
        counts["private key"] += 1
        return PRIVATE_KEY

    text = _PRIVATE_BLOCK_RE.sub(private, text)
    text = _PRIVATE_OPEN_RE.sub(private, text)

    def field(kind: str):
        def sub(m):
            value = m.group("value")
            if _kept(value) or (kind in ("secret", "login") and _code_value(m)):
                return m.group(0)
            counts[kind] += 1
            new = short(value) if kind == "shortened" else (LOGIN if kind == "login" else SECRET)
            return m.group("key") + m.group("q") + new
        return sub

    text = _PASSWORD_RE.sub(field("secret"), text)
    text = _LOGIN_RE.sub(field("login"), text)
    text = _AUTH_USER_RE.sub(field("login"), text)
    text = _KEY_RE.sub(field("shortened" if temporary else "secret"), text)
    text = _SIGNED_ACCESS_RE.sub(field("shortened" if temporary else "secret"), text)
    text = _TEMP_KEY_RE.sub(field("shortened"), text)
    text = _BEARER_RE.sub(field("shortened"), text)

    def bare(m):
        counts["shortened"] += 1
        return short(m.group(0))

    text = _JWT_RE.sub(bare, text)
    text = _IAM_TOKEN_RE.sub(bare, text)
    text = _keypairs(text, temporary, counts)
    return _gate_rest(text, counts), counts


def _keypairs(text: str, temporary: bool, counts: Counter) -> str:
    """A key pair the gate finds without a field name (a key id of 20 capitals and digits with its secret within two
    lines): both shortened in a text with a security token, else both [secret]. The id goes with its secret."""
    from awb import gate

    spans: set[tuple[int, int]] = set()
    starts = gate._line_starts(text)
    for off in gate._detect_keypair(text):
        m = gate._SK_RE.match(text, off)
        if m is None:
            continue
        spans.add((m.start(), m.end()))
        line = text.count("\n", 0, off)
        lo = starts[max(0, line - gate._KEYPAIR_LINES)]
        hi_line = line + gate._KEYPAIR_LINES + 1
        hi = starts[hi_line] if hi_line < len(starts) else len(text)
        for a in gate._AK_RE.finditer(text, lo, hi):
            spans.add((a.start(), a.end()))
    for a, b in sorted(spans, reverse=True):
        value = text[a:b]
        counts["shortened" if temporary else "secret"] += 1
        text = text[:a] + (short(value) if temporary else SECRET) + text[b:]
    return text


def _gate_hits(text: str) -> list[tuple[int, str]]:
    from awb import gate

    out = []
    for cls in GATE_CLASSES:
        out.extend((off, cls) for off in gate.DETECTORS[cls](text))
    return sorted(out)


def _gate_rest(text: str, counts: Counter) -> str:
    """What the gate still finds: the value at its offset replaced (a token shortened), a line where that does not
    help replaced as a whole."""
    for _ in range(_MAX_ROUNDS):
        hits = _gate_hits(text)
        if not hits:
            return text
        off, cls = hits[0]
        m = _VALUE_AT_RE.match(text, off)
        if m is None or _kept(m.group(0)):
            break
        new = short(m.group(0)) if cls == "token" else SECRET
        counts["shortened" if cls == "token" else "secret"] += 1
        before = text
        text = text[:m.start()] + new + text[m.end():]
        if _gate_hits(text) == hits:      # the same finding again: the value is not where the gate points
            text = before
            break
    from awb import gate

    found = {f.line for f in gate._scan_text("lab", text, None) if f.cls in GATE_CLASSES and f.line > 0}
    if found:
        lines = text.split("\n")
        for n in sorted(found):
            if 0 < n <= len(lines):
                lines[n - 1] = SECRET
                counts["secret line"] += 1
        text = "\n".join(lines)
    return text


def said(counts: Counter) -> str:
    """The counts in one short phrase: "2 shortened, 1 secret"."""
    return ", ".join("%d %s" % (n, kind) for kind, n in sorted(counts.items()) if n)
