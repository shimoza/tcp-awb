"""Structured data found by pattern: mail (also at an IP literal such as user@[192.0.2.1], also with the at and
the dots spelled out or bracketed, also broken after the @), URL and bare domain (also defanged with hxxp and [.],
also a wildcard or a leading dot), IP (also with zero padded octets and in a cloud host name ip-a-b-c-d), MAC
(also Cisco dotted and in groups of four), IBAN (with any separators, also over a line break, checked by mod 97
or, with an IBAN label, by its length), BIC, VAT id, phone, register number, tax number.

Each hit is a `matcher.Span` with `cls` set to the lower-case kind of `codes.DATA_KINDS`. Spans never overlap:
when two patterns claim the same characters, the longer span wins, then the kind listed first in `PATTERNS`.
Documented and private addresses (`KEEP_IP`), public documentation hosts (`rules/allow-domains.txt`) and the
public API and documentation hosts of the platform itself (`PLATFORM_DOMAINS`) are never reported. A label in
front of a value (IBAN:, BIC-Code:, mac, Steuernummer) proves the value and is not part of the span. Nothing here
writes a matched value anywhere.

The red team of 2026-09-27 (calibration/redteam-2026-09-27.md) found the shapes the first version missed; every
one of them is a planted case now.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Callable

from awb.matcher import Span

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


def _load_allow_domains() -> tuple[str, ...]:
    f = RULES_DIR / "allow-domains.txt"
    if not f.exists():
        return ()
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            out.append(line)
    return tuple(out)


ALLOW_DOMAINS = _load_allow_domains()

PLATFORM_DOMAINS = (("otc.t-systems.com", 2), ("open-telekom-cloud.com", 1))
"""The public API, console and documentation hosts of the platform the Workbench serves (T Cloud Public), with
the most labels a host may carry in front of the domain: `iam.eu-de.otc.t-systems.com` and
`docs.otc.t-systems.com` are the platform, `<bucket>.obs.eu-de.otc.t-systems.com` is not (its first label is a
bucket name that a customer chose). Built in, because every Workbench text names these hosts."""

KEEP_IP = ("169.254.", "100.125.", "100.64.", "0.0.0.0", "127.", "10.", "192.168.", "255.", "::1", "fe80:",
           "198.18.", "198.19.", "224.")
"""Addresses that are never a customer's: link local, the platform's service range, private ranges, the benchmark
range of RFC 2544 (198.18.0.0/15, where the platform keeps a system route) and multicast (224.0.0.0/4)."""
_KEEP_172 = re.compile(r"^172\.(1[6-9]|2\d|3[01])\.")

# --------------------------------------------------------------------------- hosts

_CC_TLDS = (
    "ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt bw by bz ca "
    "cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er es et eu fi fj fk fm fo "
    "fr ga gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je "
    "jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo "
    "mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt "
    "pw py qa re ro rs ru rw sa sb sc sd se sg sh si sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl "
    "tm tn to tr tt tv tw tz ua ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw"
)
_GENERIC_TLDS = (
    "com org net info biz name pro mobi tel asia cat jobs museum aero coop int mil gov edu arpa post xxx "
    "example local intern internal lan corp home test invalid localhost onion "
    "cloud online site store shop app dev xyz email systems digital tech tools works world zone team center "
    "expert partners global international engineering energy finance bank insurance health care media news "
    "press travel hotel restaurant cafe bar pub club live life today now top one guru ninja rocks space studio "
    "design art gallery photo photos pics video tube radio city town land house homes immo immobilien haus "
    "gmbh ltd llc inc sarl srl ag group company solutions services consulting agency network software "
    "science education academy school university college institute foundation charity church community "
    "social family kids baby pet vet dog cat fashion style beauty fitness sport sports football golf bike "
    "auto car cars taxi bus train flights cruise vacations holiday tours events tickets show theater film "
    "movie music band audio games game play fun toys gift gifts wedding love dating chat forum blog wiki "
    "review reviews report reports data database host hosting server cloud domains web website page link "
    "click run zip mov map docs support help faq how guide tips tricks ideas plus best new cool "
    "berlin hamburg bayern nrw ruhr saarland koeln cologne wien tirol zuerich swiss alsace paris london "
    "amsterdam brussels nyc tokyo sydney melbourne moscow istanbul dubai africa asia lat quebec scot wales "
    "cymru irish frl eus gal bzh cat "
    "ai io co me tv fm am cc gg im is it la ly nu sh so st to ws"
)
_TLD_SET = frozenset((_CC_TLDS + " " + _GENERIC_TLDS).split())
_TLDS = "|".join(sorted(_TLD_SET, key=lambda t: (-len(t), t)))
"""Every country code and the common generic names. A file suffix that is also a domain (setup.py, main.tf,
report.md, libx.so, archive.zip) is a domain only with a host of two or more labels in front of it."""
_FILE_SUFFIX_TLDS = frozenset(
    "sh py pl rs md tf cc so ai ps gs am pm vb cd sc zip mov map cs yt ms"
    .split())
_DOT = r"(?:\\?\.|[\u2024\uff0e]|[ \t]*(?:\(dot\)|\[dot\]|\{dot\}|\(\.\)|\[\.\]|\{\.\})[ \t]*|(?-i:[ \t]+DOT[ \t]+))"
"""A dot in a host: plain, escaped for Markdown, a one dot leader or a full width dot, or spelled out or bracketed
the way defanged addresses are written."""
_AT = r"(?:[ \t]{0,2}\\?@[ \t]*(?:\n[ \t]*)?|\uff20|[ \t]*(?:\(at\)|\[at\]|\{at\}|<at>|\(a\))[ \t]*|(?-i:[ \t]+AT[ \t]+))"
_LABEL = r"[^\W_](?:(?:[^\W\s]|-){0,61}[^\W_])?"
"""A host label: letters and digits of any script, with hyphens and underscores inside."""
_HOST = r"(?:%s%s)+(?:%s)" % (_LABEL, _DOT, _TLDS)
_DEFANG_RE = re.compile(r"(?i)[ \t]*(?:\(dot\)|\[dot\]|\{dot\}|\(\.\)|\[\.\]|\{\.\})[ \t]*|[ \t]+DOT[ \t]+|\\\.|[\u2024\uff0e]")
_ABBREVIATION_RE = re.compile(r"[A-ZÄÖÜ][a-zäöüß]{0,5}\.[A-Za-z]{2}")
"""Tel.Nr, St.Nr, Bsp.AG: a short capitalised word, a dot and two letters is an abbreviation, not a host."""


def _plain_host(host: str) -> str:
    return _DEFANG_RE.sub(".", host).lower().rstrip(".").lstrip("*.")


def _allowed_host(host: str) -> bool:
    host = _plain_host(host)
    if any(host == d or host.endswith("." + d) for d in ALLOW_DOMAINS):
        return True
    for domain, depth in PLATFORM_DOMAINS:
        if host == domain:
            return True
        if host.endswith("." + domain) and len(host[:-len(domain) - 1].split(".")) <= depth:
            return True
    return False


def _host_ok(host: str) -> bool:
    """A bare host is a domain unless it is an allowed host, an abbreviation or a file name."""
    if _allowed_host(host):
        return False
    plain = _plain_host(host)
    if _ABBREVIATION_RE.fullmatch(host.strip("*.")):
        return False
    labels = plain.split(".")
    if labels[-1] in _FILE_SUFFIX_TLDS and len(labels) < 3:
        return False
    return True


def _url_ok(s: str) -> bool:
    host = re.sub(r"^[a-z]+://", "", s, flags=re.I).split("/")[0].split(":")[0].split("@")[-1]
    return not _allowed_host(host)


def _domain_ok(s: str) -> bool:
    return _host_ok(s.split("/")[0])


def _mail_ok(s: str) -> bool:
    return True


# --------------------------------------------------------------------------- addresses

_SECTION_REF = re.compile(
    r"(?i)(?:§§?|\b(?:section|sections|clause|chapter|annex|appendix|paragraph|para|item|figure|table|"
    r"abschnitt|kapitel|ziffer|ziff|anlage|absatz|nummer|sd)\b|(?<!ip)(?<!ip[- ])\b(?:nr|no)\b)[\s.:]*$")
"""A section reference standing right in front of a dotted number, such as "section 3.1.4.2" or "SD 3.1.2".
"IP-Nr." and "IP no." announce an address, not a section."""

_WEAK_SECTION_RE = re.compile(r"(?i)\b(?:nr|no)\b[\s.:]*$")
"""The section words that also announce an address ("server no. 203.0.113.9"): a section only when every
octet is small."""
_OCTET = r"\d{1,3}"
"""Any three digits, also full width ones and zero padded: the range is checked in code."""


def _octets(s: str) -> list[int] | None:
    addr = re.sub(r"^(?i:ip-)", "", s).split("/")[0]
    parts = re.split(r"[.-]", _DEFANG_RE.sub(".", addr))
    if len(parts) != 4 or not all(o.isdigit() for o in parts):
        return None
    octets = [int(o) for o in parts]
    return octets if all(o <= 255 for o in octets) else None


def _ipv4_ok(s: str) -> bool:
    octets = _octets(s)
    if octets is None:
        return False
    addr = ".".join(str(o) for o in octets)
    if _KEEP_172.match(addr):
        return False
    return not any(addr.startswith(k) for k in KEEP_IP if "." in k or k == "0.0.0.0")


def _ipv6_ok(s: str) -> bool:
    addr = s.split("/")[0]
    try:
        ip = ipaddress.IPv6Address(addr)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return False
    groups = [g for g in addr.split(":") if g]
    return len(groups) >= 2 and sum(len(g) for g in groups) >= 5


# --------------------------------------------------------------------------- bank and tax

_IBAN_LENGTH = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BI": 27, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28,
    "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MN": 20,
    "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24, "PL": 28, "PS": 29, "PT": 25,
    "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24, "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27,
    "SO": 23, "ST": 25, "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
}
_IBAN_SEP = r"[ \t\n\u00a0.\-\u2010\u2011\u2013*_]"
_IBAN_LABEL = r"(?i:iban)[\s:.=-]*"


def _iban_ok(s: str) -> int | bool:
    """The end of the IBAN inside `s` (the separators of the pattern may run on into the next words), or
    False when the letters and digits up to the length the country code announces are not a valid number."""
    lab = re.match(r"(?i)iban[\s:.=-]*", s)
    offset = lab.end() if lab else 0
    country = s[offset:offset + 2].upper()
    want = _IBAN_LENGTH.get(country)
    if want is None:
        return False
    chars: list[str] = []
    end = offset
    for i in range(offset, len(s)):
        if s[i].isalnum():
            chars.append(s[i].upper())
            end = i + 1
            if len(chars) == want:
                break
        elif s[i] == "\n" and "\n" in s[offset:i]:
            return False
    if len(chars) != want or not "".join(chars[2:4]).isdigit():
        return False
    iban = "".join(chars)
    moved = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    if int(digits) % 97 != 1:
        return False
    return end


_PHONE_SEP = r"(?:\\?[ \t\u00a0./\-\u2010\u2011\u2013]{1,3}|\n)"
_PHONE_SEP2 = r"(?:\\?[ \t\u00a0.\-\u2010\u2011\u2013]{1,3}|\n)"
"""Between the groups of the subscriber number: no slash, which separates one number from the next."""
_PHONE_START = r"(?<!\d:)(?:(?<![\w./,+(\uff0b])|(?<=[A-Za-z]\.)|(?<=\d/))"
"""Where a phone number may begin: not glued to a word or a number, but right after "Tel." or after the slash
that separates two numbers. Never right after the colon of a clock: the minutes, seconds and fraction of a time
(`ls -l`, a sub-second timestamp) are not a number to call (T8)."""
_CLOCK_BEFORE = re.compile(r"\d:\d\d(?::\d\d)?(?:[.,]\d{1,9})?[ \t]$")
"""A clock time right before a zone offset: `12:00:00.123456789 +0200` (`ls --full-time`)."""


def _listing_part(text: str, start: int, end: int) -> bool:
    """True for a phone-shaped span that is part of a listing (T8): it ends in the hour of a time
    (`0 2026-10-07 06:02`, the size and date columns of `ls -l`), it is the stem of a file name
    (`0002-2026-10-07-1.txt`) or it is the zone offset after a time."""
    if re.match(r":\d\d(?!\d)|\.[A-Za-z][A-Za-z0-9]{0,4}(?![\w.])", text[end:end + 7]):
        return True
    return text[start:start + 1] in ("+", "-") and bool(_CLOCK_BEFORE.search(text[max(0, start - 24):start]))
_DATE_RE = re.compile(r"0?\d{1,2}[ ./-]0?\d{1,2}[ ./-]\d{2,4}|0\d[ ./-]\d{4}")
"""dd.mm.yyyy, dd mm yyyy and mm/yyyy: a date, not a number to call."""


def _phone_ok(s: str) -> bool:
    s = s.replace("\uff0b", "+").replace("\\", "")
    n = sum(c.isdigit() for c in s)
    if not 6 <= n <= 15:
        return False
    body = re.sub(r"^\(?\+?\d{0,3}\)?[\s./-]*", "", s) if s.startswith(("(", "+")) else s
    if _DATE_RE.fullmatch(s.strip()) or _DATE_RE.fullmatch(body.strip()):
        return False
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", s.strip()):
        return False      # a dotted quad is an address (a kept one is not a number to call)
    # An unbroken run of digits that only starts with a zero is far more often a code than a number: WAF
    # and platform error codes (010000, 080263), order numbers, offsets. A written phone number carries a
    # country code or a separator between the area code and the subscriber number. Without either, ask for
    # the length a German number really has (shortest area code plus shortest subscriber number is nine).
    if not s.startswith(("+", "(")) and not any(c in " ./- ‐‑–\n" for c in s):
        return n >= 9
    return True


_BIC_LABEL = r"(?i:bic|swift)(?:[\s-]*code)?[\s:.=-]*"


def _bic_ok(s: str) -> bool:
    # an all-letter 8-character word is too often an ordinary word; accept it only with a digit, at full
    # length 11 in capitals, or when the caller saw a BIC or SWIFT label (the pattern carries the label then)
    if re.match(r"(?i)^(bic|swift)", s):
        return True
    return (len(s) == 11 and s.isupper()) or any(c.isdigit() for c in s)


_VAT_BODY = (
    r"DE[ .\t-]{0,2}\d{3}[ .]?\d{3}[ .]?\d{3}|ATU[ .\t-]{0,2}\d{4}[ .]?\d{4}|NL[ .\t-]{0,2}\d{9}[ .]?B[ .]?\d{2}"
    r"|FR[ .\t-]{0,2}[0-9A-Z]{2}[ .]?\d{9}"
    r"|IT[ .]?\d{11}|ES[ .]?[A-Z0-9]\d{7}[A-Z0-9]|PL[ .]?\d{10}|CZ[ .]?\d{8,10}|BE[ .]?0?\d{9}|LU[ .]?\d{8}"
    r"|DK[ .]?\d{8}|CHE[ .-]?\d{3}[ .]?\d{3}[ .]?\d{3}(?:[ ]?(?:MWST|TVA|IVA))?|SE[ .]?\d{12}|IE[ .]?\d[A-Z0-9+*]\d{5}[A-Z]{1,2}"
    r"|GB[ .]?\d{3}[ .]?\d{4}[ .]?\d{2}(?:[ .]?\d{3})?|PT[ .]?\d{9}|FI[ .]?\d{8}|HU[ .]?\d{8}|SK[ .]?\d{10}"
    r"|EE[ .]?\d{9}|LV[ .]?\d{11}|LT[ .]?\d{9}(?:\d{3})?|SI[ .]?\d{8}|HR[ .]?\d{11}|RO[ .]?\d{2,10}|BG[ .]?\d{9,10}"
    r"|CY[ .]?\d{8}[A-Z]|MT[ .]?\d{8}|EL[ .]?\d{9}|NO[ .]?\d{9}(?:[ ]?MVA)?"
)


def _vat_ok(s: str) -> bool:
    return sum(c.isdigit() for c in s) >= 7


_TAX_LABEL = (
    r"(?i:steuer[\s-]*(?:identifikationsnummer|nummer|nr\.?|id(?:[\s-]*(?:nr\.?|nummer))?)|st\.?[\s-]*nr\.?|stnr\.?"
    r"|steuerliche[\s-]+identifikationsnummer|idnr\.?|tax[\s-]*(?:id|number|no\.?)|tin)[\s:.=]*"
)

# --------------------------------------------------------------------------- the patterns

_P = re.compile

# (kind, pattern, check, label pattern stripped from the start of the span)
PATTERNS: list[tuple[str, re.Pattern, Callable[[str], bool] | None, re.Pattern | None]] = [
    ("MAIL", _P(r"(?i)(?<![\w.%%+-])(?:\"[^\"\n]{1,64}\"|[^\s@<>\"()\[\],;:]{1,64})%s"
                r"(?:%s(?![^\W_])|\[?(?:\d{1,3}\.){3}\d{1,3}\]?(?![\w.-]*\d)|\[ipv6:[0-9a-f:.]+\])"
                % (_AT, _HOST)), None, None),
    ("URL", _P(r"(?i)\b(?:https?|hxxps?|ftps?|sftp|ssh|smb|ldaps?|rdp|vnc|git|wss?)://"
               r"(?:\[\.\]|[^\s<>\"'`)\]}|])+(?<![.,;:!?])"), _url_ok, None),
    ("IBAN", _P(r"(?i)(?<![A-Za-z0-9])(?:iban[\s:.=-]*)?[A-Z]%s*[A-Z]%s*\d%s*\d(?:%s*[A-Z0-9]){11,30}(?![A-Za-z0-9])"
                % (_IBAN_SEP, _IBAN_SEP, _IBAN_SEP, _IBAN_SEP)), _iban_ok, _P(_IBAN_LABEL)),
    ("VAT", _P(r"(?i)\b(?:%s)\b" % _VAT_BODY), _vat_ok, None),
    ("IP", _P(r"(?<![\d.])(?:%s%s){3}%s(?:/(?:3[0-2]|[12]?\d))?(?![\d.]*\d)" % (_OCTET, _DOT, _OCTET)), _ipv4_ok,
     None),
    ("IP", _P(r"(?i)(?<![\w-])ip-%s-%s-%s-%s(?![\w-])" % (_OCTET, _OCTET, _OCTET, _OCTET)), _ipv4_ok, None),
    ("IP", _P(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{1,4}:){1,7}(?::[0-9A-Fa-f]{1,4}){0,6}(?:[0-9A-Fa-f]{1,4}|:)?"
              r"(?:/\d{1,3})?(?![\w:])"), _ipv6_ok, None),
    ("MAC", _P(r"(?i)(?<![\w:.-])(?:(?:mac|hwaddr|ether|lladdr)[\s:=-]*)?(?:[0-9a-f]{2}([:\- ])[0-9a-f]{2}"
               r"(?:\1[0-9a-f]{2}){4}|[0-9a-f]{4}([.-])[0-9a-f]{4}\2[0-9a-f]{4})(?![\w:.-])"), None,
     _P(r"(?i)(?:mac|hwaddr|ether|lladdr)[\s:=-]*")),
    ("MAC", _P(r"(?i)(?:mac|hwaddr|ether|lladdr)[\s:=-]*[0-9a-f]{12}(?![\w])"), None,
     _P(r"(?i)(?:mac|hwaddr|ether|lladdr)[\s:=-]*")),
    ("PHONE", _P(r"(?<![\w+\uff0b])(?:[+\uff0b]\d{1,3}|\(\+\d{1,3}\))%s?(?:\(0?\d{0,5}\)%s?)?\d{1,5}(?:%s?\d{2,}){1,5}(?!\d)"
                 % (_PHONE_SEP, _PHONE_SEP, _PHONE_SEP)), _phone_ok, None),
    ("PHONE", _P(r"(?<!\d:)(?<![\w+.,/])00[ \t]?[1-9]\d{0,2}%s(?:\(0\)%s?)?\d{1,5}(?:%s?\d{2,}){1,5}(?!\d)"
                 % (_PHONE_SEP, _PHONE_SEP, _PHONE_SEP)), _phone_ok, None),
    ("PHONE", _P(r"%s(?:\(0\d{1,5}\)|\(0\)[ \t]?\d{1,5}|0[ \t.]\d{1,5}|0\d{1,5})%s?\d{1,5}+(?:%s?\d{1,5}+){0,12}(?![\d.,]\d)(?!\w)"
                 % (_PHONE_START, _PHONE_SEP, _PHONE_SEP2)), _phone_ok, None),
    ("BIC", _P(r"(?i)(?:bic|swift)(?:[\s-]*code)?[\s:.=-]*\b[A-Z]{4}[ ]?[A-Z]{2}[ ]?[A-Z0-9]{2}(?:[ ]?[A-Z0-9]{3})?\b"),
     _bic_ok, _P(_BIC_LABEL)),
    ("BIC", _P(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b"), _bic_ok, None),
    ("HRB", _P(r"(?i)\b(?:HR[ ]?[AB]|handelsregister[\s-]*[AB])[\s.:\-]*(?:nr\.?[\s.:]*)?\d{3,6}(?:[ ]?[A-Z]\b)?"),
     None, _P(r"(?i)handelsregister[\s-]*(?=[AB])")),
    ("TAX", _P(r"(?<![\d/])\d{2,3}/\d{3,4}/\d{4,5}(?![\d/])|(?<![\d/])\d{5}/\d{5}(?![\d/])"), None, None),
    ("TAX", _P(r"%s\d[\d /.-]{7,15}\d" % _TAX_LABEL), None, _P(_TAX_LABEL)),
    ("URL", _P(r"(?i)(?<![\w.-])(?:\*?\.)?%s(?![^\W_])(?:/(?:\[\.\]|[^\s<>\"'`)\]])*)?" % _HOST), _domain_ok, None),
]

_PRIORITY = {kind: i for i, kind in enumerate(dict.fromkeys(k for k, _, _, _ in PATTERNS))}
_FLAVOR_TAIL = re.compile(r"\.(?:[a-z0-9-]*\d[a-z0-9-]*|[a-z0-9-]+\.[a-z0-9-]+)(?![\w])", re.IGNORECASE)
_SCHEME_START = re.compile(r"(?i)[a-z]+://")
_HOST_NUMBER_IP = re.compile(r"\d{1,4}[ \t|]+((?:\d{1,3}\.){3}\d{1,3})")


def _host_number_ip(value: str) -> bool:
    """A number, a cell border or blanks, then a dotted quad whose four parts are octets."""
    m = _HOST_NUMBER_IP.fullmatch(value)
    return bool(m) and all(int(x) <= 255 for x in m.group(1).split("."))
_IBAN_SHAPE = re.compile(r"\b[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]{4}){2,7}(?: ?[A-Za-z0-9]{1,3})?\b")
"""The strict shape of an IBAN, for the rule that digits inside it are never a phone number: the loose pattern
above would swallow any spaced number between two letters."""

_TAG_ATTRS = r"""(?:\s(?:(?>"[^"<]*")|(?>'[^'<]*')|[^<>])*+)?"""
_MARKUP_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<![^<>]{0,200}>|</?[A-Za-z][A-Za-z0-9:-]*%s/?>" % _TAG_ATTRS, re.S)
"""Markup a viewer does not show. A tag is a tag name followed by attributes or the closing bracket: an address
or a URL in angle brackets (<user@host>) is not markup."""
_SPACED_CHAR = r"(?:[^\W_]|[.:/@+-])"
_SPACED_RUN_RE = re.compile(r"(?<![^\W_])(?:%s {1,3}){5,}%s(?![^\W_])" % (_SPACED_CHAR, _SPACED_CHAR))
"""Six or more characters each followed by one to three spaces: the way pdftotext writes letter spaced text."""


_CELL_SEP_RE = re.compile(r"[ \t]*[*_`~]{0,2}[ \t]*\|[ \t]*[*_`~]{0,2}[ \t]*")
"""The border between two Markdown table cells, emphasis or code marks next to it included: a value typed over
two cells (an area code and a number, the groups of an IBAN in boxes, one half in bold) reads as one value
across it."""


def _view(text: str) -> tuple[str, list[int] | None]:
    """`text` with markup inside a value taken out, letter spaced runs closed up and cell borders between two
    values read as one space, and the position in `text` of every view character. (text, None) when nothing
    changes."""
    edits: list[tuple[int, int, str]] = [(m.start(), m.end(), "") for m in _MARKUP_RE.finditer(text)]
    for m in _SPACED_RUN_RE.finditer(text):
        a = m.start()
        edits.extend((a + g.start(), a + g.end(), "") for g in re.finditer(r" {1,3}", m.group(0)))
    for m in _CELL_SEP_RE.finditer(text):
        a, b = m.span()
        if 0 < a and b < len(text) and text[a - 1].isalnum() and text[b].isalnum():
            edits.append((a, b, " "))
    if not edits:
        return text, None
    edits.sort()
    out: list[str] = []
    positions: list[int] = []
    last = 0
    for a, b, rep in edits:
        if a < last:
            continue
        out.append(text[last:a])
        positions.extend(range(last, a))
        out.append(rep)
        positions.extend([a] * len(rep))
        last = b
    out.append(text[last:])
    positions.extend(range(last, len(text)))
    return "".join(out), positions


# --------------------------------------------------------------------------- the code view (T12)

CODE_SUFFIXES = (".tf", ".tfvars", ".py", ".sh", ".js", ".yaml", ".yml", ".json")
"""Files read in the code view: a dotted reference (`module.lb.listener_port`, `self.client.name`) is code there."""
_CODE_TLDS = frozenset({"com", "de", "org", "net", "io", "cloud", "eu"} |
                       {d.rsplit(".", 1)[-1] for d in ALLOW_DOMAINS if "." in d})
"""The last labels a bare host in code may end with to count as a web address: a short built-in list and the
public suffixes of `rules/allow-domains.txt`."""
_CODE_LEFT = frozenset("=([.")


def is_code_file(path) -> bool:
    return path is not None and Path(str(path)).suffix.lower() in CODE_SUFFIXES


def code_url_kept(text: str, start: int, end: int) -> bool:
    """Whether a url hit at text[start:end] stays a web address in the code view. One with a scheme always does.
    A bare host does only when its last label is in `_CODE_TLDS` and it does not sit right of `=`, `(`, `[` or
    `.` in its line (blanks between them aside): there it is a reference of the code. A host in quotes is kept."""
    value = text[start:end]
    if "://" in value:
        return True
    last = _plain_host(value.split("/")[0]).rsplit(".", 1)[-1]
    if last not in _CODE_TLDS:
        return False
    left = text[text.rfind("\n", 0, start) + 1:start].rstrip(" \t")
    return not (left and left[-1] in _CODE_LEFT)


def code_view(text: str, spans: list[Span]) -> list[Span]:
    """`spans` of `text` with the url hits the code view does not keep taken out; every other class stays."""
    return [s for s in spans if s.cls != "url" or code_url_kept(text, s.start, s.end)]


def find_structured(text: str, code: bool = False) -> list[Span]:
    """All structured-data spans in `text`, sorted by position, never overlapping. A value that is broken by
    markup (a comment or an empty tag pair inside it) or spread out letter by letter is found through a view
    of the text and reported at its place in `text`, markup included. `code` reads the text in the code view."""
    spans = _find_structured(text)
    return code_view(text, spans) if code else spans


def _find_structured(text: str) -> list[Span]:
    view, positions = _view(text)
    spans = _find(view)
    if positions is None:
        return spans
    out: list[Span] = []
    for s in spans:
        start = positions[s.start]
        end = positions[s.end - 1] + 1
        out.append(Span(start=start, end=end, cls=s.cls))
    # a value that lies wholly inside markup (an address in an href, an IP in a comment) is gone from the view:
    # the text as written is searched as well and what the view did not find is added
    for s in _find(text):
        if not any(a.start < s.end and s.start < a.end for a in out):
            out.append(s)
    out.sort(key=lambda s: (s.start, s.end))
    return out


def _find(text: str) -> list[Span]:
    found: list[tuple[int, int, str]] = []
    for kind, rx, ok, label in PATTERNS:
        pos = 0
        while True:
            m = rx.search(text, pos)
            if m is None:
                break
            pos = m.end() if m.end() > m.start() else m.start() + 1
            value = m.group(0)
            start, end = m.start(), m.end()
            if ok is not None:
                verdict = ok(value)
                if verdict is False or verdict is None:
                    continue
                if verdict is not True:
                    end = start + int(verdict)      # the checker cut the value short, the search goes on there
                    pos = end
            if label is not None:
                # a label that was matched to prove the value is not part of the value
                lab = label.match(value)
                if lab:
                    start += lab.end()
            if kind == "PHONE":
                while end > start and text[end - 1] in " \t\n./- ‐‑–":
                    end -= 1
            if end > start:
                found.append((start, end, kind))
    # digits inside something shaped like an IBAN are never a phone number, even when the IBAN is invalid
    iban_shaped = [(m.start(), m.end()) for m in _IBAN_SHAPE.finditer(text)]
    found = [t for t in found if not (t[2] == "PHONE" and any(a <= t[0] and t[1] <= b for a, b in iban_shaped))]
    # the size, date and clock columns of a listing, a file name and the zone offset of a time are not a number
    found = [t for t in found if not (t[2] == "PHONE" and _listing_part(text, t[0], t[1]))]
    # a dotted number announced as a section of a document is a section, not an address: service
    # descriptions and contracts are full of "section 3.1.4.2" and every one of them parses as an IP
    found = [t for t in found if not (t[2] == "IP" and _SECTION_REF.search(text[max(0, t[0] - 28):t[0]])
                                       and not (_WEAK_SECTION_RE.search(text[max(0, t[0] - 28):t[0]])
                                                and max(_octets(text[t[0]:t[1]]) or [0]) >= 100))]
    # a bare host that a dot and a label with a digit (or two more labels) follow is the head of a flavor id,
    # not a host: rds.pg.c6.large.4 is no address under .pg (pptx-table-services of the red team of 2026-10-07)
    found = [t for t in found if not (t[2] == "URL" and _FLAVOR_TAIL.match(text, t[1])
                                       and not _SCHEME_START.match(text, t[0]))]
    # the number at the end of a host name in the cell before an IP (srv-db-01 | 10.0.0.5) is no phone number:
    # the address is an IP and the number belongs to the host (xlsx-host-number-before-ip)
    found = [t for t in found if not (t[2] == "PHONE" and _host_number_ip(text[t[0]:t[1]]))]
    # longer first, then priority, then position; keep a span only when it overlaps nothing kept so far
    found.sort(key=lambda t: (-(t[1] - t[0]), _PRIORITY[t[2]], t[0]))
    kept: list[tuple[int, int, str]] = []
    for s, e, k in found:
        if all(e <= ks or s >= ke for ks, ke, _ in kept):
            kept.append((s, e, k))
    kept.sort()
    return [Span(start=s, end=e, cls=k.lower()) for s, e, k in kept]
