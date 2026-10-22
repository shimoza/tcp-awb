"""The rules of `awb clean --kind logs` and the secret wipe of both kinds (task lc-laptop-clean).

A dump of a technical problem (logs, configs, tickets) is full of capitalised technical words that the candidate
rules of the intake would take for names. Here no name is guessed from the shape of a word: a value is wiped only
when its place says what it is.

    secret    [secret]    a private key block, the value of a key-like field (password, secret, token, api_key,
                          access_key, client_secret, passphrase, Passwort, Kennwort ...), an Authorization or token
                          header, a credential with a known prefix (ghp_, glpat-, xox, AKIA, sk-, a JWT) and the
                          password in the userinfo of a URL; a line the commit gate still finds a secret in after
                          that is removed as a whole
    user      [user 1]    the value of a user field (user=, login:, owner=, upn=, author: ...), the user of sshd and
                          PAM lines ("Failed password for X", "session opened for user X", "logged in as X"),
                          DOMAIN\\user and the folder of a home path
    host      [host 1]    a host name under an internal suffix (.local, .lan, .internal, .corp, .intra ...), the
                          value of a host field (host=, hostname:, server=, node= ...), the host column of a syslog
                          line and the host after "host" in a sentence
    ip        [ip 1]      every IPv4 and IPv6 address except loopback, unspecified, broadcast, netmask, link local,
                          multicast and the service range of the platform (100.125.0.0/16)

The tokens are numbered per run, one number per value, so two lines about one host still read as one host. Mail
addresses, phone numbers, public host names that are not public product or service hosts (rules/allow-domains.txt)
and the registered forms are the engine's (awb/intake/engine.py) and run after these rules. Standard library only.
Nothing here prints, logs or raises with a value.
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter

SECRET = "[secret]"
PRIVATE_KEY = "[private key]"
DROPPED_LINE = "[line removed: secret]"

SYSTEM_USERS = frozenset((
    "root", "admin", "administrator", "system", "nobody", "daemon", "www-data", "nginx", "apache", "postgres",
    "mysql", "redis", "ubuntu", "ec2-user", "centos", "debian", "fedora", "linux", "guest", "anonymous", "service",
    "jenkins", "git", "docker", "kube", "kubelet", "operator", "sshd", "cron", "bin", "sys", "sync", "mail",
    "systemd-network", "systemd-resolve", "systemd-timesync", "messagebus", "syslog", "polkitd", "chrony", "ntp",
    "local", "localhost", "null", "none", "true", "false", "unknown", "n/a", "na", "-", "*", "default", "user",
    "users", "everyone", "authenticated", "local service", "network service", "trustedinstaller", "sa", "dbo",
    "public", "paas", "cce", "obs", "ecs", "iam", "tomcat", "oracle", "grid", "elasticsearch", "kibana", "grafana",
    "prometheus", "node", "app", "web", "test", "demo", "backup", "monitor", "monitoring", "deploy", "ansible",
    "terraform", "admin1", "readonly", "read", "write", "owner", "id", "name", "uid", "gid", "pid", "self",
))
"""Accounts every system has: never a person and never a customer, kept as they are."""

INTERNAL_SUFFIXES = ("local", "lan", "internal", "intern", "corp", "intra", "intranet", "home", "localdomain",
                     "priv", "private", "ad", "domain", "office", "site", "loc", "int")
"""The last label of a host name that only an internal network resolves."""

_PLACEHOLDER = re.compile(r"(?i)^(?:\*+|x+|<[^<>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|%[A-Za-z_]+%|\$[A-Za-z_]\w*|null|none|"
                          r"nil|true|false|redacted|\[redacted\]|\[secret\]|changeme|password|secret|\?+|-|\.+|"
                          r"\[[a-z ]+\d*\])$")

_SECRET_LONG = r"pass(?:word|wd|wort|phrase)|kennwort|secret|token|api_?key|credentials?"
"""Key words that end a field name with or without a separator before them (dbpassword, db_password)."""
_SECRET_SHORT = (r"pass|pwd|pw|access_?key(?:_?id)?|private_?key|auth_?token|bearer|sk|ak|x-auth-token|"
                 r"x-subject-token|x-api-key|connection_?string|conn_?str|dsn")
"""Key words that are a secret only as a whole word or after a separator (task=... is no sk)."""
_SECRET_FIELD_RE = re.compile(
    r"""(?ix)(?P<key>(?<![\w-])(?:[\w.-]*(?:%s)|(?:[\w.-]*[_.-])?(?:%s))) (?P<sep>["']?\s*(?:=|:|=>)\s*)"""
    r"""(?P<q>["'])?(?P<value>(?(q)[^"'\n]+|[^\s,;&"'<>\]\)}]+))""" % (_SECRET_LONG, _SECRET_SHORT))
_SECRET_FLAG_RE = re.compile(r"""(?i)(?<![\w-])(?P<key>-p|--password|--passwd|--pass|--secret|--token|--api-key|"""
                             r"""--access-key|--secret-key|--sk|--ak)(?P<sep>[ \t=]+)(?P<q>["'])?"""
                             r"""(?P<value>(?(q)[^"'\n]+|[^\s"']+))""")
_AUTH_HEADER_RE = re.compile(r"(?i)(?P<key>\b(?:authorization|proxy-authorization)\s*[:=]\s*[\"']?"
                             r"(?:bearer|basic|token|digest|negotiate|obs|aws4-hmac-sha256|sdk-hmac-sha256)?\s*)"
                             r"(?P<value>[^\s\"',;]+(?:[ ,]+(?:credential|signature|signedheaders)=[^\s\"',;]+)*)")
_TOKEN_RE = re.compile(
    r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|"
    r"hf_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{30,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|MII[A-Za-z0-9+/]{60,}={0,2})(?![\w-])")
_USERINFO_RE = re.compile(r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]{1,15}://)(?P<user>[^\s:/@\[\]]{1,64}):"
                          r"(?P<pw>[^\s/@]{1,128})@")
_PRIVATE_BLOCK_RE = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----.*?-----END \1-----", re.S)
_PRIVATE_OPEN_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*\Z")

_USER_FIELD_RE = re.compile(
    r"""(?ix)(?<![\w-])(?P<key>(?:[\w.-]*[_.-])?(?:user(?:_?name)?|login(?:_?name)?|logon(?:_?name)?|uid|cn|"""
    r"""user_?id|account(?:_?name)?|"""
    r"""owner|principal|author|actor|initiator|requester|requestor|reporter|assignee|(?:created|updated|modified|"""
    r"""changed|approved)_?by|samaccountname|upn|userprincipalname|benutzer(?:name)?|bearbeiter|ersteller|"""
    r"""remote_?user|db_?user|os_?user|ssh_?user|admin_?user|iam_?user))"""
    r"""(?P<sep>["']?\s*(?:=|:|=>)\s*)(?P<q>["']?)(?P<value>[^\s,;"'<>\[\]\(\){}|]{1,128})""")
_USER_FLAG_RE = re.compile(r"(?<![\w-])(?:-u|--user|--username|--login)(?:[ \t]+|=)(?P<value>[A-Za-z][\w.@$-]{0,63})")
_GLUED_PASSWORD_RE = re.compile(r"(?i)\b(?P<key>(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]*?\s-p)(?P<value>[^\s'\"]{4,})")
_NAME_MAIL_RE = re.compile(r"(?P<value>(?<![\w.-])[A-ZÄÖÜ][\w'’.-]+(?:[ \t]+[A-ZÄÖÜ][\w'’.-]+){1,3})(?=[ \t]*<\[mail \d+\]>)")
_USER_PHRASE_RES = (
    re.compile(r"(?i)\b(?:accepted|failed)\s+(?:password|publickey|keyboard-interactive(?:/pam)?|none|hostbased)"
               r"\s+for\s+(?:invalid\s+user\s+)?(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
    re.compile(r"(?i)\b(?:invalid|illegal|unknown)\s+user\s+(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
    re.compile(r"(?i)\bsession\s+(?:opened|closed)\s+for\s+user\s+(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
    re.compile(r"(?i)\blogged\s+(?:in|on|off|out)\s+as\s+(?:user\s+)?(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
    re.compile(r"(?i)\blog(?:in|on)\s+(?:of|for|by|as)\s+(?:user\s+)?(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
    re.compile(r"(?i)\b(?:for|by|of)\s+user\s+['\"]?(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
    re.compile(r"(?i)\buser\s+['\"](?P<value>[^\s'\"]{1,64})['\"]"),
    re.compile(r"(?i)\bBenutzer\s+['\"]?(?P<value>[^\s'\",;()\[\]<>]{1,64})"),
)
_DOMAIN_USER_RE = re.compile(r"(?<![\w\\:])(?P<value>[A-Z][A-Z0-9-]{1,14}\\{1,2}[A-Za-z][\w.$-]{1,63})(?![\w\\])")
_HOME_RE = re.compile(r"(?i)(?P<head>(?:[a-z]:)?[\\/]{1,2}(?:users|home|dokumente und einstellungen|documents and "
                      r"settings)[\\/]{1,2})(?P<value>[^\\/\s\"':*?<>|]{1,64})")

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_MAIL_RE = re.compile(r"(?<![\w.%%+-])(?P<value>[A-Za-z0-9][\w.%%+-]{0,63}@(?:%s\.)+[A-Za-z]{2,24})(?![\w-])" % _LABEL)
_URL_USER_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,15}://(?P<value>[^\s:/@\[\]]{1,64})(?=:\[secret\]@|@)")
_INTERNAL_HOST_RE = re.compile(r"(?<![\w.-])(?P<value>(?:%s\.)+(?:%s))(?![\w-]|\.[A-Za-z0-9])"
                               % (_LABEL, "|".join(INTERNAL_SUFFIXES)), re.I)
_HOST_FIELD_RE = re.compile(
    r"""(?ix)(?<![\w-])(?P<key>host(?:_?name)?|server_?name|node_?name|instance_?name|"""
    r"""computer(?:_?name)?|machine(?:_?name)?|fqdn|dns_?name|vm(?:_?name)?|remote_?host|client_?host|src_?host|"""
    r"""dst_?host|source_?host|dest(?:ination)?_?host|workstation(?:_?name)?|rechner(?:name)?)"""
    r"""(?P<sep>["']?\s*(?:=|:|=>)\s*)(?P<q>["']?)(?P<value>[A-Za-z0-9][\w.-]{0,253})""")
_HOST_PHRASE_RE = re.compile(r"(?i)\b(?:on|from|to|at|for)\s+(?:the\s+)?(?:host|server|node|machine)\s+['\"]?"
                             r"(?P<value>[A-Za-z0-9][\w.-]{1,253})")
_SYSLOG_RE = re.compile(r"(?m)^(?P<head>(?:<\d{1,3}>\d?\s*)?(?:[A-Z][a-z]{2}\s{1,2}\d{1,2}\s\d\d:\d\d:\d\d|"
                        r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?)\s+)"
                        r"(?P<value>[A-Za-z0-9][\w.-]{0,253})(?=\s+[\w./()-]+(?:\[\d+\])?:\s)")
_IPV4_RE = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)"
                      r"(?:/(?:3[0-2]|[12]?\d))?(?![\w]|\.\d)")
_IPV6_RE = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{1,4}:){2,7}(?::?[0-9A-Fa-f]{1,4}|:)(?:/\d{1,3})?(?![\w:])|"
                      r"(?<![\w:.])::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}(?![\w:])")
_VERSION_BEFORE = re.compile(r"(?i)(?:\bv|version|ver\.?|release|build|firmware|fw|rev)[\s:=]*$")
_TOKEN_VALUE = re.compile(r"^\[(?:user|host|ip|secret|person|company|place|name)(?: \d+)?\]$")


_PLAIN: frozenset[str] | None = None


def _plain_word(value: str) -> bool:
    """A word of the public lists (rules/common-words.txt, rules/tech-terms.txt, the intake-seed of
    rules/allowed-terms.txt): Get, nginx, worker are no user and no host."""
    global _PLAIN
    if _PLAIN is None:
        from awb import patterns, words
        from awb.intake import wipe

        _PLAIN = frozenset(words.load_plain(patterns.RULES_DIR / "common-words.txt")
                           | words.load_plain(patterns.RULES_DIR / "tech-terms.txt")
                           | wipe._list("allowed-terms.txt", section="intake-seed"))
    return value.casefold() in _PLAIN


def _keep_user(value: str) -> bool:
    v = value.strip("'\"").rstrip(".:")
    if not v or _PLACEHOLDER.match(v) or _TOKEN_VALUE.match(v) or v.casefold() in SYSTEM_USERS:
        return True
    if _plain_word(v):
        return True
    if re.fullmatch(r"[\d.:-]+", v):           # a number (uid=1000), not a name
        return True
    if re.fullmatch(r"[A-Z]+-[A-Z2-7]{4}(?:-[A-Z]+-\d{1,3})?", v):   # a code of the Workbench
        return True
    if v.startswith(("$", "%", "{", "[")):
        return True
    return False


def _keep_host(value: str) -> bool:
    v = value.strip("'\".").casefold()
    if not v or v in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "*", "none", "null",
                      "true", "false", "default", "any", "all", "unknown", "-"):
        return True
    if _TOKEN_VALUE.match(v) or re.fullmatch(r"[\d.:/]+", v):   # an address is the ip rule's
        return True
    if not re.search(r"[a-z]", v):
        return True
    if "." not in v and _plain_word(v):
        return True
    from awb import patterns

    return patterns._allowed_host(v)


def _ip_kept(value: str, text: str, start: int) -> bool:
    if _VERSION_BEFORE.search(text[max(0, start - 12):start]):
        return True
    try:
        addr = ipaddress.ip_interface(value).ip
    except ValueError:
        return True
    if addr.version == 4:
        if str(addr).startswith(("255.", "0.")) or addr.is_loopback or addr.is_link_local or addr.is_multicast:
            return True
        if addr in ipaddress.ip_network("100.125.0.0/16"):
            return True
        return False
    return addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast


class Tokens:
    """One number per value and class for one run, and the rows of the mapping (never written by this module)."""

    def __init__(self):
        self.map: dict[tuple[str, str], str] = {}
        self.numbers: Counter = Counter()
        self.rows: list[tuple[str, str, str]] = []

    def token(self, cls: str, value: str) -> str:
        key = (cls, value.casefold() if cls != "ip" else value)
        tok = self.map.get(key)
        if tok is None:
            self.numbers[cls] += 1
            tok = self.map[key] = "[%s %d]" % (cls, self.numbers[cls])
            self.rows.append((tok, cls, value))
        return tok


def wipe_secrets(text: str, counts: Counter) -> str:
    """Every secret of the rules above replaced; the counts per class go into `counts` (never a value)."""
    def private(m):
        counts["private key"] += 1
        return PRIVATE_KEY

    text = _PRIVATE_BLOCK_RE.sub(private, text)
    text = _PRIVATE_OPEN_RE.sub(private, text)

    def field(m):
        value = m.group("value")
        if _PLACEHOLDER.match(value.strip()) or len(value.strip()) < 3:
            return m.group(0)
        if re.fullmatch(r"[^\W\d_]+", value.strip()) and _plain_word(value.strip()):
            return m.group(0)      # "Password: must have 12 characters", "token: expired" carry no secret
        counts["secret"] += 1
        return m.group("key") + m.group("sep") + (m.group("q") or "") + SECRET

    text = _AUTH_HEADER_RE.sub(lambda m: (counts.update(["secret"]), m.group("key") + SECRET)[1], text)
    text = _USERINFO_RE.sub(lambda m: (counts.update(["secret"]), m.group("scheme") + m.group("user") + ":" + SECRET
                                       + "@")[1], text)
    text = _SECRET_FIELD_RE.sub(field, text)
    text = _SECRET_FLAG_RE.sub(field, text)
    text = _GLUED_PASSWORD_RE.sub(lambda m: (counts.update(["secret"]), m.group("key") + SECRET)[1], text)
    text = _TOKEN_RE.sub(lambda m: (counts.update(["secret"]), SECRET)[1], text)
    return text


def drop_secret_lines(text: str, counts: Counter) -> str:
    """The lines the commit gate still finds a secret, a private key or a token in, removed as a whole."""
    from awb import gate, hooks

    found = {f.line for f in gate._scan_text("clean", text, None) if f.cls in hooks.SECRET_CLASSES and f.line > 0}
    if not found:
        return text
    lines = text.split("\n")
    for n in sorted(found):
        if 0 < n <= len(lines):
            lines[n - 1] = DROPPED_LINE
            counts["secret line"] += 1
    return "\n".join(lines)


def wipe_log(text: str, tokens: Tokens, counts: Counter) -> str:
    """The users, hosts and addresses of a log, config or ticket replaced by their tokens (the module text)."""
    def sub_value(rx: re.Pattern, cls: str, keep, text: str) -> str:
        def repl(m):
            value = m.group("value")
            a, b = m.span("value")
            trail = len(value) - len(value.rstrip(".,:;!?"))
            if trail and trail < len(value):
                value, b = value[:-trail], b - trail
            if keep(value):
                return m.group(0)
            counts[cls] += 1
            return m.group(0)[:a - m.start()] + tokens.token(cls, value) + m.group(0)[b - m.start():]
        return rx.sub(repl, text)

    text = sub_value(_MAIL_RE, "mail", lambda v: False, text)
    text = sub_value(_URL_USER_RE, "user", _keep_user, text)
    text = _HOME_RE.sub(lambda m: m.group(0) if _keep_user(m.group("value"))
                        else (counts.update(["user"]), m.group("head") + tokens.token("user", m.group("value")))[1],
                        text)
    text = sub_value(_DOMAIN_USER_RE, "user", lambda v: _keep_user(v.split("\\")[-1]) and
                     v.split("\\")[0].casefold() in ("nt authority", "builtin", "nt service", "window manager"), text)
    text = sub_value(_USER_FIELD_RE, "user", _keep_user, text)
    text = sub_value(_USER_FLAG_RE, "user", _keep_user, text)
    for rx in _USER_PHRASE_RES:
        text = sub_value(rx, "user", _keep_user, text)
    text = sub_value(_SYSLOG_RE, "host", _keep_host, text)
    text = sub_value(_HOST_FIELD_RE, "host", lambda v: _keep_host(v) or bool(_IPV4_RE.fullmatch(v)), text)
    text = sub_value(_HOST_PHRASE_RE, "host", lambda v: _keep_host(v) or bool(_IPV4_RE.fullmatch(v)), text)
    text = sub_value(_INTERNAL_HOST_RE, "host", _keep_host, text)

    def ip(m):
        value = m.group(0)
        if _ip_kept(value, m.string, m.start()):
            return value
        counts["ip"] += 1
        return tokens.token("ip", value)

    text = _IPV4_RE.sub(ip, text)
    text = _IPV6_RE.sub(ip, text)
    # the display name in front of a wiped mail address ("Name <[mail 1]>") is that person's
    text = sub_value(_NAME_MAIL_RE, "person", lambda v: False, text)
    return text


_CODE_FIRST = frozenset("com org net io de eu ch at nl fr uk be it es se dk no fi pl cz java javax jakarta sun "
                        "android androidx kotlin kotlinx scala groovy akka".split())
_CODE_LAST = frozenset("java class kt scala groovy py pyc js ts go rb php cs cpp h hpp c rs sh ps1 bat cmd yaml yml "
                       "json xml conf cfg ini toml properties log txt md sql jar war ear so dll exe run main "
                       "init start stop call invoke apply execute handle process".split())


def _code_identifier(value: str) -> bool:
    """A dotted name of code, not a host: a reverse domain package path (com.vendor.app.Main.run, three labels or
    more after com, org, java ...), or a dotted name that ends in a file suffix or a method word."""
    if "://" in value or "/" in value or "@" in value:
        return False
    labels = value.strip(".").split(".")
    if any("_" in label for label in labels):        # no host label holds an underscore: a resource address
        return True
    if len(labels) >= 3 and labels[0].casefold() in _CODE_FIRST:
        return True
    return len(labels) >= 2 and (labels[-1].casefold() in _CODE_LAST
                                 or any(re.search(r"[a-z][A-Z]", label) for label in labels))


def log_view(engine) -> None:
    """The engine of a log run: the url hits that are names of code and the address hits that stand after a version
    word are left out (rpm 4.18.1.1, com.vendor.app.Main.run); every other class of the engine stays."""
    original = engine.spans

    def spans(text, candidates=True):
        names, structured = original(text, candidates)
        kept = []
        for s in structured:
            value = text[s.start:s.end]
            if s.cls == "url" and (_code_identifier(value) or re.match(r"\.[A-Za-z_$]", text[s.end:s.end + 2])
                                   or (s.start and text[s.start - 1] == ".")):
                continue      # a dotted name of code goes on after the hit: java.net.ConnectException
            if s.cls == "ip" and _VERSION_BEFORE.search(text[max(0, s.start - 12):s.start]):
                continue
            kept.append(s)
        return names, kept

    engine.spans = spans
