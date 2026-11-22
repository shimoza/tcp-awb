"""The secrets of a log of our own lab runs (task GT, his decision of 2026-10-27).

A project keeps full live examples of API calls and responses from the lab tenants: request bodies, full response
bodies and every id (domains, projects, users, groups, resources, request ids, the header and the claims of a
JWT). Only these are touched:

    password, login        replaced with [secret] and [login]
    console password       a password an image prints on its serial console: the `user:password` lines after the
                           cloud-init line "Set the following 'random' passwords", the `user:password` pairs of a
                           chpasswd line and the lines of its list, `password for user: x`, also inside the JSON of
                           an os-getConsoleOutput answer where a line break is the two characters \\n (tcp-6edq)
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
_AFTER_BREAK = r"(?:(?<![\w-])|(?<=\\[nrt]))"
"""The start of a field: no word character before it, or a line break or a tab written as JSON escapes (the
console text under output of os-getConsoleOutput, `...\\npassword: x`)."""
_PASSWORD_RE = re.compile(
    r"(?i)" + _AFTER_BREAK + r"(?P<key>[\"']?(?:[\w.-]*[_.-])?(?:pass(?:word|wd|wort|phrase)?|pwd|kennwort)" + _SEP + r")"
    + _VALUE)
PS_PASSWORD_RE = re.compile(
    r"""(?i)(?<![\w-])(?P<key>(?:-[A-Za-z]*(?:password|passwort|pwd)|convertto-securestring(?:[ \t]+-string)?)"""
    r"""[ \t]+)(?P<q>["'])?(?P<value>(?(q)[^"'\n]+|[^\s"'`,;<>(){}\[\]]+))""")
"""A PowerShell password parameter or the text of ConvertTo-SecureString with its literal value
(`-Password 'x'`, `ConvertTo-SecureString "x" -AsPlainText`)."""
_PS_CODE_RE = re.compile(r"\$(?:env:)?[\w:]+|" + r"(?:New|Get|Set|Read|ConvertTo|ConvertFrom|Import|Export|Invoke)-"
                         r"[A-Z][A-Za-z]+")


def ps_password_sub(text: str, counts: Counter) -> str:
    """`text` with the literal of every PowerShell password parameter replaced by [secret]; a variable, an
    environment value or a cmdlet as the value is code and stays (tcp-renz)."""
    def sub(m):
        value = m.group("value")
        if _kept(value) or _PS_CODE_RE.fullmatch(value) or (not m.group("q") and value.startswith("$")):
            return m.group(0)
        counts["secret"] += 1
        return m.group("key") + (m.group("q") or "") + SECRET
    return PS_PASSWORD_RE.sub(sub, text)


_LOGIN_RE = re.compile(
    r"(?i)" + _AFTER_BREAK + r"(?P<key>[\"']?(?:login(?:[_-]?name)?|user[_-]?name|username)" + _SEP + r")" + _VALUE)
_AUTH_USER_RE = re.compile(r"""(?P<key>"user"\s*:\s*\{[^{}]*?"name"\s*:\s*")(?P<q>)(?P<value>[^"\n]+)(?=")""")
"""The name of the user in an IAM body ({"user": {"name": ...}}): the login of a password or a user call."""
_PASSWORD_FOR_RE = re.compile(
    r"(?i)" + _AFTER_BREAK + r"(?P<key>(?:new[ \t]+)?password[ \t]+(?:for|of)[ \t]+(?:user[ \t]+)?[\w.@-]{1,64}[ \t]*"
    r"(?:is[ \t]+|[:=][ \t]*))"
    r"""(?P<q>["']?)(?P<value>(?=[^\s"'`,;<>(){}\[\]]*[\d!#$%&*+?@^~])[^\s"'`,;<>(){}\[\]]+)""")
"""`Password for ubuntu: x`, `new password of user root is x`: a console line that names the user first. The
value carries a digit or a symbol, so prose (`the password for root: change it`) stays."""
_CONSOLE_HINT_RE = re.compile(r"(?i)chpasswd|set the following '?random'? passwords?")
_CONSOLE_BREAK_RE = re.compile(r"(\r?\n|\\r\\n|\\n|\\r)")
"""A line break of a console text: a real one or the JSON escape of one (\\n, \\r\\n)."""
_RANDOM_HEAD_RE = re.compile(r"(?i)set the following '?random'? passwords?")
_CONSOLE_PAIR_LINE_RE = re.compile(
    r"""(?P<key>[ \t"']*(?:\[[ \t\d.]+\][ \t]*)?(?:cloud-init\[\d+\]:[ \t]*)?(?:-[ \t]+)?["']?"""
    r"""[a-z_][a-z0-9_.-]{0,31}:)(?P<q>)(?P<value>[^\s"'\\]+)(?P<rest>["']?[ \t]*["']?[ \t]*,?[ \t]*)""")
"""A line of a console password list (`ubuntu:x`, `  - root:x`, `[  12.3] cloud-init[812]: ubuntu:x`)."""
_CHPASSWD_LIST_RE = re.compile(r"""(?i)[ \t"']*(?:list|expire|users|type)["']?[ \t]*:.*""")
_INLINE_PAIR_RE = re.compile(r"""(?<![\w./:@-])(?P<key>[a-z_][a-z0-9_.-]{0,31}:)(?P<q>)(?P<value>[^\s"'\\|;:/][^\s"'\\|;]*)""")


def _console_passwords(text: str, counts: Counter) -> str:
    """`text` with the passwords of a console masked: the `user:password` lines after the cloud-init line "Set the
    following 'random' passwords", the `user:password` pairs on a chpasswd line and the list lines after it. A line
    break may be the JSON escape \\n of an answer body."""
    if not _CONSOLE_HINT_RE.search(text):
        return text
    parts = _CONSOLE_BREAK_RE.split(text)

    def value(m) -> str:
        """The text of the match `m` with its value masked."""
        if _kept(m.group("value")):
            return m.group(0)
        counts["secret"] += 1
        a, b = m.start("value") - m.start(), m.end("value") - m.start()
        return m.group(0)[:a] + SECRET + m.group(0)[b:]

    state = 0                   # 1: after the random passwords line, 2: after a chpasswd line
    for i in range(0, len(parts), 2):
        line = parts[i]
        if state:
            m = _CONSOLE_PAIR_LINE_RE.fullmatch(line)
            if m:
                parts[i] = value(m)
                continue
            if state == 2 and _CHPASSWD_LIST_RE.fullmatch(line):
                continue
            state = 0
        if _RANDOM_HEAD_RE.search(line):
            state = 1
        elif "chpasswd" in line.lower():
            parts[i] = _INLINE_PAIR_RE.sub(lambda m: m.group(0) if m.group("key").lower() == "chpasswd:" else value(m),
                                           line)
            state = 2
    return "".join(parts)


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
# a PowerShell cmdlet with its arguments after it ($secret = New-Object System.Security.SecureString): code, no value
_CMDLET_RE = re.compile(r"(?:New|Get|Set|Read|ConvertTo|ConvertFrom|Import|Export|Invoke|Select|Join|Test|Start|"
                        r"Out|Format|Resolve|Split|Find|Copy|Add|Remove)-[A-Z][A-Za-z]+")


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
    if _CMDLET_RE.fullmatch(value) and after[:1] in (" ", "\t", "\n", "\r", ""):
        return True             # a PowerShell cmdlet call, its arguments after it
    return bool(_DOTTED_RE.fullmatch(value) or (variables and _VARIABLE_RE.fullmatch(value)))


def _code_value(m: re.Match) -> bool:
    return code_value(m.group("key"), bool(m.group("q")), m.group("value"), m.string[m.end():m.end() + 1])


_HEADING_LINE_RE = re.compile(r"[ \t]{0,3}#{1,6}[ \t]")
_HEADING_WORD_RE = re.compile(r"[A-Za-z][a-z]+")
"""A Markdown heading and a plain word in it: lower case letters, the first one maybe a capital."""


def heading_word(m: re.Match) -> bool:
    """A field of a Markdown heading whose value is a plain word with no value shape (`# Password: policy and
    rotation`, `## Bearer authentication`): a title, no secret (tcp-6ntg). An unquoted value with a digit, a symbol
    or mixed case stays a value, also in a heading."""
    if m.group("q") or not _HEADING_WORD_RE.fullmatch(m.group("value")):
        return False
    a = m.string.rfind("\n", 0, m.start()) + 1
    return bool(_HEADING_LINE_RE.match(m.string, a))


def short(value: str) -> str:
    """The first 8 characters, "..." and the length."""
    return "%s...(%d chars)" % (value[:KEEP], len(value))


_SHORT_RE = re.compile(r"""(?<![^\s"'`,;<>(){}\[\]])[^\s"'`,;<>(){}\[\]]{1,%d}\.\.\.\(\d+ chars\)""" % KEEP)


def without_short(text: str) -> str:
    """`text` with every value `short` wrote replaced by [secret]: the first 8 characters of a random token are no
    words, and a short form of a name in them is chance, not a name (FL2, 2026-10-30)."""
    return _SHORT_RE.sub(SECRET, text)


def _kept(value: str) -> bool:
    """A value that is no secret to touch: a placeholder, a reference, a value shortened before."""
    v = value.strip()
    return len(v) < 3 or bool(_PLACEHOLDER_RE.search(v)) or bool(_REFERENCE_RE.match(v))


def shorten(text: str) -> tuple[str, Counter]:
    """`text` with its passwords and logins masked, its temporary credentials shortened and its permanent keys and
    private keys replaced and the counts per kind (never a value)."""
    from awb import gate

    counts: Counter = Counter()
    kept: list[str] = []

    def console(m):             # the token of a VNC console link stays whole (his decision of 2026-11-18)
        kept.append(m.group(0))
        return "[console token %d]" % (len(kept) - 1)

    if "vnc" in text.lower():
        text = gate.CONSOLE_TOKEN_RE.sub(console, text)
    text, counts = _shorten(text, counts)
    for i, value in enumerate(kept):
        text = text.replace("[console token %d]" % i, value, 1)
    return text, counts


def _shorten(text: str, counts: Counter) -> tuple[str, Counter]:
    temporary = bool(_TEMP_HINT_RE.search(text))

    def private(m):
        counts["private key"] += 1
        return PRIVATE_KEY

    text = _PRIVATE_BLOCK_RE.sub(private, text)
    text = _PRIVATE_OPEN_RE.sub(private, text)

    def field(kind: str):
        def sub(m):
            value = m.group("value")
            if _kept(value) or (kind in ("secret", "login") and _code_value(m)) or heading_word(m):
                return m.group(0)
            counts[kind] += 1
            new = short(value) if kind == "shortened" else (LOGIN if kind == "login" else SECRET)
            return m.group("key") + m.group("q") + new
        return sub

    text = _console_passwords(text, counts)
    text = _PASSWORD_RE.sub(field("secret"), text)
    text = _PASSWORD_FOR_RE.sub(field("secret"), text)
    text = ps_password_sub(text, counts)
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
