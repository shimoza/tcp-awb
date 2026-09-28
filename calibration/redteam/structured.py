"""Red team, dimension structured: structured data in every written shape, in plain text and Markdown.

Every value is invented: mail at .example, domains under .example (and invented labels under real TLDs where
the TLD itself is the point), IPv4 from 203.0.113.0/24, IPv6 from 2001:db8::/32, phone numbers with 030 or 040
and 555 or 1234 digits, IBANs built to pass mod 97, VAT ids DE plus nine digits.
"""
import base64
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

MARK = "stage two of the invented plan"


def txt(name, text):
    def build(inbox):
        p = inbox / name
        p.write_text(text, encoding="utf-8")
        return p
    return build


def case(id_, cls, carrier, values, name, text, note=None):
    body = "%s\n\n%s\n" % (text, MARK)
    c = {"id": id_, "cls": cls, "carrier": carrier, "values": values, "visible": [MARK],
         "build": txt(name, body)}
    if note:
        c["note"] = note
    return c


def iban(country, bban):
    s = bban + country + "00"
    n = int("".join(str(int(c, 36)) for c in s))
    return "%s%02d%s" % (country, 98 - n % 97, bban)


def grouped(v):
    return " ".join(v[i:i + 4] for i in range(0, len(v), 4))


IBAN_DE = iban("DE", "555055505550555068")          # DE83 5550 5550 5550 5550 68
IBAN_LT = iban("LT", "5550555055505550")            # a country not in _IBAN_LENGTH

# the values of the classes no pattern claims (np-* cases), built from pieces so that the commit gate does not
# read this file as a leak (tests/test_gate.py does the same)
PRIV = "PRI" + "VATE"
NP_HEX = "3f2a9c8b7d6e5f4a" + "3b2c1d0e9f8a7b6c"
NP_UUID = "7c9e6679-7425-40de" + "-944b-e07fc1f90ae7"
NP_ID20 = "QWXZ7VKD2M" + "4PLN9RT3AB"
NP_KEY42 = "qW3xT9vLp2mN8k" + "R4sD6fG1hJ5bV7" + "cX0zA2eS4rT6yU"
NP_SESSION = "k7f3q9x2m8p4w1r6" + "t0y5u3i8o2a7s9d4"
NP_BEARER = "q9m2k7x4p1w8r3t6y0u" + "5i2o7a4s1d8f3g6h9j2"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


NP_JWT = _b64url(b'{"alg":"HS256"}') + "." + _b64url(b'{"sub":"tberg","stage":2}') + "." + NP_BEARER + "kl"
IBAN_COUNTRIES = {cc: iban(cc, ("5550" * 10)[:ln - 6] + "%02d" % (ord(cc[0]) % 100)) for cc, ln in (
    ("AT", 20), ("BE", 16), ("CH", 21), ("CZ", 24), ("DE", 22), ("DK", 18), ("ES", 24), ("FI", 18), ("FR", 27),
    ("GB", 22), ("GR", 27), ("HU", 28), ("IE", 22), ("IT", 27), ("LI", 21), ("LU", 20), ("NL", 18), ("NO", 15),
    ("PL", 28), ("PT", 25), ("RO", 24), ("SE", 24), ("SK", 24))}

MAIL = "tberg@mailhost.example"
DOM = "mailhost.example"
IP4 = "203.0.113.9"
IP6 = "2001:db8::7"
MAC = "00:1a:2b:3c:4d:5e"
BIC = "QWXZDEFF"
VAT = "DE123456789"
HRB = "123456"

CASES = [
    # ------------------------------------------------------------------ mail
    case("mail-at-dot-paren", "mail", "txt body, (at) and (dot) spelled", ["tberg (at) mailhost (dot) example"],
         "m1.txt", "write to tberg (at) mailhost (dot) example for the offer"),
    case("mail-at-dot-bracket", "mail", "txt body, [at] and [dot]", ["tberg[at]mailhost[dot]example"],
         "m2.txt", "write to tberg[at]mailhost[dot]example for the offer"),
    case("mail-at-spelled-upper", "mail", "md body, AT and DOT spelled out", ["tberg AT mailhost DOT example"],
         "m3.md", "contact: tberg AT mailhost DOT example"),
    case("mail-spaces-around-at", "mail", "txt body, spaces around @", ["tberg @ mailhost.example", "tberg @"],
         "m4.txt", "write to tberg @ mailhost.example for the offer",
         "the domain is replaced, the local part with the lone @ stays"),
    case("mail-percent40", "mail", "txt body, %40 for @", ["tberg%40mailhost.example", MAIL],
         "m5.txt", "write to tberg%40mailhost.example for the offer"),
    case("mail-mailto-encoded", "mail", "md link, mailto with %40", ["tberg%40mailhost.example", MAIL],
         "m6.md", "[mail](mailto:tberg%40mailhost.example?subject=offer)"),
    case("mail-quoted-local", "mail", "txt body, quoted local part", ['"t berg"@mailhost.example', '"t berg"@'],
         "m7.txt", 'write to "t berg"@mailhost.example for the offer',
         "the domain is replaced, the quoted local part stays"),
    case("mail-idn-domain", "mail", "txt body, umlaut in the domain", ["tberg@mailhöst.example"],
         "m8.txt", "write to tberg@mailhöst.example for the offer"),
    case("mail-ipv6-literal", "mail", "txt body, address at an IPv6 literal", ["tberg@[IPv6:2001:db8::7]"],
         "m9.txt", "write to tberg@[IPv6:2001:db8::7] for the offer"),
    case("mail-upper", "mail", "txt body, upper case", ["TBERG@MAILHOST.EXAMPLE"],
         "m10.txt", "write to TBERG@MAILHOST.EXAMPLE for the offer"),
    case("mail-split-line", "mail", "txt body, address broken after the @", ["tberg@\nmailhost.example", "tberg@"],
         "m11.txt", "write to tberg@\nmailhost.example for the offer",
         "the domain on the second line is replaced, the local part with the @ stays"),
    case("mail-fullwidth-at", "mail", "txt body, fullwidth @ (U+FF20)", ["tberg＠mailhost.example", "tberg＠"],
         "m12.txt", "write to tberg＠mailhost.example for the offer"),
    case("mail-underscore-host", "mail", "txt body, underscore in the host", ["tberg@mail_host.example"],
         "m13.txt", "write to tberg@mail_host.example for the offer"),
    # ------------------------------------------------------------------ url and domain
    case("url-hxxp", "url", "txt body, defanged hxxp", ["hxxp://mailhost.example/offer"],
         "u1.txt", "see hxxp://mailhost.example/offer for it"),
    case("url-bracket-dot", "url", "txt body, defanged [.]", ["mailhost[.]example"],
         "u2.txt", "see mailhost[.]example for it"),
    case("url-hxxps-bracket-dot", "url", "txt body, hxxps with [.]", ["hxxps://mailhost[.]example/offer"],
         "u3.txt", "see hxxps://mailhost[.]example/offer for it"),
    case("url-dot-spelled", "url", "txt body, (dot) spelled", ["mailhost(dot)example"],
         "u4.txt", "see mailhost(dot)example for it"),
    case("url-trailing-dot", "url", "txt body, trailing dot", ["mailhost.example."],
         "u5.txt", "the host is mailhost.example. today"),
    case("url-upper", "url", "txt body, upper case", ["MAILHOST.EXAMPLE"],
         "u6.txt", "the host is MAILHOST.EXAMPLE today"),
    case("url-punycode", "url", "txt body, punycode label", ["xn--qwxzv-8ka.example"],
         "u7.txt", "the host is xn--qwxzv-8ka.example today"),
    case("url-userinfo-password", "url", "txt body, user and password in the url", ["https://admin:s3cret@mailhost.example/"],
         "u8.txt", "see https://admin:s3cret@mailhost.example/ for it"),
    case("url-port", "url", "txt body, host with port", ["mailhost.example:8443"],
         "u9.txt", "the host is mailhost.example:8443 today"),
    case("url-ipv6-host", "url", "txt body, IPv6 host in brackets", ["https://[2001:db8::7]:8443/"],
         "u10.txt", "see https://[2001:db8::7]:8443/ for it"),
    case("url-customer-domain-inside", "url", "txt body, customer domain inside a longer host",
         ["backup." + fx.CUSTOMER_DOMAIN, fx.CUSTOMER_DOMAIN], "u11.txt", "the host is backup.%s today" % fx.CUSTOMER_DOMAIN),
    case("url-tlds-not-listed", "url", "txt body, TLDs not in _TLDS",
         ["qwxzv-portal.shop", "qwxzv-portal.app", "qwxzv-portal.dev", "qwxzv-portal.xyz", "qwxzv-portal.me",
          "qwxzv-portal.tv", "www.qwxzv-portal.shop"],
         "u12.txt", "hosts: qwxzv-portal.shop, qwxzv-portal.app, qwxzv-portal.dev, qwxzv-portal.xyz, "
                    "qwxzv-portal.me, qwxzv-portal.tv and www.qwxzv-portal.shop"),
    case("url-path-only", "url", "txt body, domain with a path", ["mailhost.example/offer/2026"],
         "u13.txt", "see mailhost.example/offer/2026 for it"),
    case("url-wildcard", "url", "txt body, wildcard host", ["*.mailhost.example"],
         "u14.txt", "the certificate covers *.mailhost.example today"),
    case("url-leading-dot", "url", "txt body, leading dot (no_proxy, cookie domain)", [".mailhost.example"],
         "u15.txt", "no_proxy=.mailhost.example"),
    case("url-underscore-label", "url", "txt body, underscore in a label", ["mail_host.example"],
         "u16.txt", "the host is mail_host.example today"),
    case("url-dot-leader", "url", "txt body, one dot leader (U+2024) for the dot", ["mailhost․example"],
         "u17.txt", "the host is mailhost․example today"),
    # ------------------------------------------------------------------ ip
    case("ip4-leading-zeros", "ip", "txt body, zero padded octets", ["203.000.113.007"],
         "i1.txt", "host 203.000.113.007 answers"),
    case("ip4-leading-zero-last", "ip", "txt body, one leading zero in the last octet", ["203.0.113.09"],
         "i2.txt", "host 203.0.113.09 answers"),
    case("ip4-numeric-forms", "ip", "txt body, decimal, octal and hex forms", ["3405803785", "0313.0.0161.011", "0xCB007109"],
         "i3.txt", "host 3405803785 or 0313.0.0161.011 or 0xCB007109 answers"),
    case("ip4-slash32", "ip", "txt body, /32", ["203.0.113.9/32"],
         "i4.txt", "route 203.0.113.9/32 is set"),
    case("ip4-in-url", "ip", "txt body, in a url with port", ["http://203.0.113.9:8080/x", IP4],
         "i5.txt", "see http://203.0.113.9:8080/x for it"),
    case("ip4-range", "ip", "txt body, a range", ["203.0.113.9-203.0.113.20", IP4, "203.0.113.20"],
         "i6.txt", "pool 203.0.113.9-203.0.113.20 is free"),
    case("ip4-port", "ip", "txt body, with port", ["203.0.113.9:8080", IP4],
         "i7.txt", "host 203.0.113.9:8080 answers"),
    case("ip4-mapped-ipv6", "ip", "txt body, IPv4-mapped IPv6", ["::ffff:203.0.113.9", IP4],
         "i8.txt", "host ::ffff:203.0.113.9 answers"),
    case("ip4-defanged", "ip", "txt body, defanged [.]", ["203[.]0[.]113[.]9"],
         "i9.txt", "host 203[.]0[.]113[.]9 answers"),
    case("ip4-dashed-hostname", "ip", "txt body, dashed octets in a host name", ["ip-203-0-113-9"],
         "i10.txt", "host ip-203-0-113-9 answers"),
    case("ip6-upper", "ip", "txt body, upper case", ["2001:DB8::7"],
         "i11.txt", "host 2001:DB8::7 answers"),
    case("ip6-compressed-end", "ip", "txt body, compressed at the end", ["2001:db8::"],
         "i12.txt", "prefix 2001:db8:: is ours"),
    case("ip6-zone-id", "ip", "txt body, with zone id", ["2001:db8::7%eth0", IP6],
         "i13.txt", "host 2001:db8::7%eth0 answers"),
    {"id": "ip6-link-local-stays", "cls": "ip", "carrier": "txt body, link local must stay", "values": [],
     "visible": [MARK, "fe80::1%eth0"], "build": txt("i14.txt", "host fe80::1%eth0 answers\n\n" + MARK + "\n"),
     "note": "no value: the address must stay, so it is a marker; UNSEEN would mean it was replaced"},
    case("ip6-label-glued", "ip", "txt body, label glued with a colon", ["ip:2001:db8::7", IP6],
         "i15.txt", "host ip:2001:db8::7 answers"),
    # ------------------------------------------------------------------ mac
    case("mac-cisco-dotted", "mac", "txt body, Cisco dotted", ["001a.2b3c.4d5e"],
         "c1.txt", "port 001a.2b3c.4d5e is up"),
    case("mac-no-separators", "mac", "txt body, no separators", ["001A2B3C4D5E"],
         "c2.txt", "port 001A2B3C4D5E is up"),
    case("mac-upper", "mac", "txt body, upper case", ["00:1A:2B:3C:4D:5E"],
         "c3.txt", "port 00:1A:2B:3C:4D:5E is up"),
    case("mac-in-url", "mac", "txt body, in a url", ["http://mailhost.example/?mac=00:1a:2b:3c:4d:5e", MAC],
         "c4.txt", "see http://mailhost.example/?mac=00:1a:2b:3c:4d:5e for it"),
    case("mac-label", "mac", "txt body, with label", ["00-1a-2b-3c-4d-5e"],
         "c5.txt", "MAC: 00-1a-2b-3c-4d-5e is up"),
    case("mac-label-glued", "mac", "txt body, label glued with a colon", ["mac:00:1a:2b:3c:4d:5e", MAC],
         "c6.txt", "port mac:00:1a:2b:3c:4d:5e is up"),
    case("mac-spaces", "mac", "txt body, spaces as separators", ["00 1a 2b 3c 4d 5e"],
         "c7.txt", "port 00 1a 2b 3c 4d 5e is up"),
    # ------------------------------------------------------------------ iban
    case("iban-dashes", "iban", "txt body, dashes", ["-".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)])],
         "b1.txt", "IBAN %s" % "-".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)])),
    case("iban-dots", "iban", "txt body, dots", [".".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)])],
         "b2.txt", "IBAN %s" % ".".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)])),
    case("iban-split-lines", "iban", "txt body, wrapped over two lines", [grouped(IBAN_DE)[:19] + "\n" + grouped(IBAN_DE)[20:]],
         "b3.txt", "IBAN %s\n%s" % (grouped(IBAN_DE)[:19], grouped(IBAN_DE)[20:])),
    case("iban-label-glued", "iban", "txt body, IBAN label glued", ["IBAN" + IBAN_DE, IBAN_DE],
         "b4.txt", "konto IBAN%s heute" % IBAN_DE),
    case("iban-label-colon-glued", "iban", "txt body, IBAN: glued", ["IBAN:" + IBAN_DE, IBAN_DE],
         "b5.txt", "konto IBAN:%s heute" % IBAN_DE),
    case("iban-lower-dashes", "iban", "txt body, lower case with dashes", ["-".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)]).lower()],
         "b6.txt", "iban %s" % "-".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)]).lower()),
    case("iban-all-countries", "iban", "txt body, one IBAN per country of the table",
         [grouped(v) for v in IBAN_COUNTRIES.values()],
         "b7.txt", "\n".join("IBAN %s" % grouped(v) for v in IBAN_COUNTRIES.values())),
    case("iban-country-not-listed", "iban", "txt body, a country not in the table (LT)", [grouped(IBAN_LT)],
         "b8.txt", "IBAN %s" % grouped(IBAN_LT)),
    case("iban-typo", "iban", "txt body, one digit wrong (fails mod 97)", [grouped(IBAN_DE[:-1] + "9")],
         "b9.txt", "IBAN %s" % grouped(IBAN_DE[:-1] + "9")),
    case("iban-double-space", "iban", "txt body, two spaces after the country group", ["DE83  " + grouped(IBAN_DE)[5:]],
         "b10.txt", "IBAN DE83  %s" % grouped(IBAN_DE)[5:]),
    case("iban-tab", "iban", "txt body, tab separated column", [grouped(IBAN_DE).replace(" ", "\t")],
         "b11.txt", "konto\t%s\tbank" % grouped(IBAN_DE).replace(" ", "\t")),
    case("iban-irregular-groups", "iban", "txt body, one group of three", [IBAN_DE[:4] + " " + IBAN_DE[4:8] + " " + IBAN_DE[8:12] + " " + IBAN_DE[12:16] + " " + IBAN_DE[16:19] + " " + IBAN_DE[19:]],
         "b12.txt", "IBAN %s %s %s %s %s %s" % (IBAN_DE[:4], IBAN_DE[4:8], IBAN_DE[8:12], IBAN_DE[12:16], IBAN_DE[16:19], IBAN_DE[19:])),
    case("iban-nb-hyphens", "iban", "txt body, non-breaking hyphens (U+2011)", ["‑".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)])],
         "b13.txt", "IBAN %s" % "‑".join([IBAN_DE[i:i + 4] for i in range(0, 22, 4)])),
    # ------------------------------------------------------------------ bic
    case("bic-lower", "bic", "txt body, lower case with label", ["qwxzdeff"],
         "s1.txt", "bic qwxzdeff heute"),
    case("bic-label-code", "bic", "txt body, BIC-Code: label", [BIC],
         "s2.txt", "BIC-Code: %s heute" % BIC),
    case("bic-label-swift-code", "bic", "txt body, swift-code: label", [BIC],
         "s3.txt", "swift-code: %s heute" % BIC),
    case("bic-label-glued", "bic", "txt body, BIC: glued", [BIC],
         "s4.txt", "konto BIC:%s heute" % BIC),
    case("bic-eleven", "bic", "txt body, 11 characters without label", [BIC + "500"],
         "s5.txt", "code %s500 heute" % BIC),
    case("bic-digit", "bic", "txt body, with a digit, no label", ["QWXZDE2F"],
         "s6.txt", "code QWXZDE2F heute"),
    case("bic-bare-eight", "bic", "txt body, 8 letters, no label, no digit", [BIC],
         "s7.txt", "code %s heute" % BIC),
    case("bic-spaced-groups", "bic", "txt body, written in groups", ["QWXZ DE FF"],
         "s8.txt", "BIC QWXZ DE FF heute"),
    # ------------------------------------------------------------------ vat
    case("vat-spaced-groups", "vat", "txt body, spaces between groups", ["DE 123 456 789"],
         "v1.txt", "USt-IdNr.: DE 123 456 789"),
    case("vat-dots", "vat", "txt body, dots", ["DE123.456.789"],
         "v2.txt", "USt-IdNr.: DE123.456.789"),
    case("vat-dash", "vat", "txt body, a dash after DE", ["DE-123456789"],
         "v3.txt", "USt-IdNr.: DE-123456789"),
    case("vat-lower", "vat", "txt body, lower case", ["de123456789"],
         "v4.txt", "ust-idnr.: de123456789"),
    case("vat-label-ustidnr", "vat", "txt body, USt-IdNr label", [VAT],
         "v5.txt", "USt-IdNr.: %s" % VAT),
    case("vat-other-countries", "vat", "txt body, countries not in the pattern",
         ["SE123456789001", "IE1234567T", "GB123456789", "PT123456789", "FI12345678", "HU12345678", "SK1234567890"],
         "v6.txt", "vat ids: SE123456789001, IE1234567T, GB123456789, PT123456789, FI12345678, HU12345678, SK1234567890"),
    case("vat-at-grouped", "vat", "txt body, Austrian id in groups", ["ATU 1234 5678"],
         "v7.txt", "UID: ATU 1234 5678"),
    case("vat-fr-grouped", "vat", "txt body, French id with a space after the key", ["FR12 345678901"],
         "v8.txt", "tva: FR12 345678901"),
    # ------------------------------------------------------------------ phone
    case("phone-paren-area", "phone", "txt body, area code in parentheses", ["(030) 555 1234"],
         "p1.txt", "call (030) 555 1234 today"),
    case("phone-dots", "phone", "txt body, dots as separators", ["030.555.1234"],
         "p2.txt", "call 030.555.1234 today"),
    case("phone-plus-dots", "phone", "txt body, +49.30.", ["+49.30.5551234"],
         "p3.txt", "call +49.30.5551234 today"),
    case("phone-extension-dash0", "phone", "txt body, extension -0", ["030 5551234-0"],
         "p4.txt", "call 030 5551234-0 today"),
    case("phone-0049-paren", "phone", "txt body, 0049 with (0)", ["0049 (0)30 5551234"],
         "p5.txt", "call 0049 (0)30 5551234 today"),
    case("phone-pairs", "phone", "txt body, national number in pairs", ["030 12 34 56 78"],
         "p6.txt", "call 030 12 34 56 78 today"),
    case("phone-tel-fon", "phone", "txt body, Tel.: and Fon prefixes", ["030 5551234", "040 5551234"],
         "p7.txt", "Tel.: 030 5551234\nFon 040 5551234"),
    case("phone-split-line", "phone", "txt body, split over lines", ["030\n5551234"],
         "p8.txt", "call 030\n5551234 today"),
    case("phone-00-dashes", "phone", "txt body, 00 with dashes", ["00-49-30-5551234"],
         "p9.txt", "call 00-49-30-5551234 today"),
    case("phone-00-space-in-cc", "phone", "txt body, space inside the country code", ["00 49 30 5551234"],
         "p10.txt", "call 00 49 30 5551234 today"),
    case("phone-0049-slash", "phone", "txt body, 0049 with slashes", ["0049/30/5551234"],
         "p11.txt", "call 0049/30/5551234 today"),
    case("phone-letters", "phone", "txt body, a number with letters", ["0800-FLOWERS"],
         "p12.txt", "call 0800-FLOWERS today"),
    case("phone-austrian", "phone", "txt body, Vienna national format", ["01 555 1234"],
         "p13.txt", "call 01 555 1234 today"),
    case("phone-swiss", "phone", "txt body, Swiss national format", ["044 555 12 34"],
         "p14.txt", "call 044 555 12 34 today"),
    case("phone-fax", "phone", "txt body, a fax number", ["030 5551235"],
         "p15.txt", "Fax: 030 5551235 today"),
    case("phone-cc-in-paren", "phone", "txt body, (+49)", ["(+49) 30 5551234"],
         "p16.txt", "call (+49) 30 5551234 today"),
    case("phone-area-in-paren-after-cc", "phone", "txt body, +49 (30)", ["+49 (30) 5551234"],
         "p17.txt", "call +49 (30) 5551234 today"),
    case("phone-plus-spaced-dash", "phone", "txt body, +49 30 - number", ["+49 30 - 5551234"],
         "p18.txt", "call +49 30 - 5551234 today"),
    case("phone-plus-spaced-slash", "phone", "txt body, +49 30 / number", ["+49 30 / 5551234"],
         "p19.txt", "call +49 30 / 5551234 today"),
    case("phone-national-spaced-dash", "phone", "txt body, 030 - number", ["030 - 5551234"],
         "p20.txt", "call 030 - 5551234 today"),
    case("phone-en-dash", "phone", "txt body, en dash between groups", ["030 – 5551234"],
         "p21.txt", "call 030 – 5551234 today"),
    case("phone-nb-hyphen", "phone", "txt body, non-breaking hyphens (U+2011)", ["+49‑30‑5551234"],
         "p22.txt", "call +49‑30‑5551234 today"),
    case("phone-tel-dot-glued", "phone", "txt body, Tel. glued to the number", ["030 5551234"],
         "p23.txt", "Tel.030 5551234 today"),
    case("phone-fullwidth-plus", "phone", "txt body, fullwidth plus (U+FF0B)", ["＋49 30 5551234"],
         "p24.txt", "call ＋49 30 5551234 today"),
    case("phone-mobile-pairs", "phone", "txt body, mobile with pairs after the first group", ["0171 555 12 34"],
         "p25.txt", "call 0171 555 12 34 today"),
    # ------------------------------------------------------------------ hrb
    case("hrb-dash", "hrb", "txt body, HRB-number", ["HRB-" + HRB],
         "h1.txt", "register HRB-%s today" % HRB),
    case("hrb-nr", "hrb", "txt body, HRB Nr.", ["HRB Nr. " + HRB],
         "h2.txt", "register HRB Nr. %s today" % HRB),
    case("hrb-lower", "hrb", "txt body, lower case", ["hrb " + HRB],
         "h3.txt", "register hrb %s today" % HRB),
    case("hrb-hra", "hrb", "txt body, HRA", ["HRA " + HRB],
         "h4.txt", "register HRA %s today" % HRB),
    case("hrb-court-after", "hrb", "txt body, court name after it", ["HRB " + HRB],
         "h5.txt", "register HRB %s Amtsgericht %s today" % (HRB, fx.PLACE_FORMS[0])),
    case("hrb-colon", "hrb", "txt body, HRB: number", ["HRB: " + HRB],
         "h6.txt", "register HRB: %s today" % HRB),
    case("hrb-spaced-digits", "hrb", "txt body, digits in two groups", ["HRB 123 456"],
         "h7.txt", "register HRB 123 456 today"),
    case("hrb-handelsregister", "hrb", "txt body, Handelsregister B spelled", ["Handelsregister B " + HRB],
         "h8.txt", "register Handelsregister B %s today" % HRB),
    # ------------------------------------------------------------------ tax
    case("tax-steuernummer-spaces", "tax", "txt body, Steuernummer with spaces", ["12 345 67890"],
         "t1.txt", "Steuernummer 12 345 67890"),
    case("tax-steuer-nr-label", "tax", "txt body, Steuer-Nr. label with spaces", ["12 345 67890"],
         "t2.txt", "Steuer-Nr. 12 345 67890"),
    case("tax-st-nr-no-dots", "tax", "txt body, St-Nr label without dots", ["12 345 67890"],
         "t3.txt", "St-Nr 12 345 67890"),
    case("tax-steuernr-label", "tax", "txt body, Steuernr. label", ["12 345 67890"],
         "t4.txt", "Steuernr. 12 345 67890"),
    case("tax-no-label-spaces", "tax", "txt body, no label, spaces", ["12 345 67890"],
         "t5.txt", "nummer 12 345 67890"),
    case("tax-steuer-id-label", "tax", "txt body, Steuer-ID label, eleven digits", ["12 345 678 901"],
         "t6.txt", "Steuer-ID 12 345 678 901"),
    case("tax-steuer-id-no-label", "tax", "txt body, eleven digits without label", ["12345678901"],
         "t7.txt", "nummer 12345678901"),
    case("tax-steuer-id-long-label", "tax", "txt body, steuerliche Identifikationsnummer label", ["12345678901"],
         "t8.txt", "steuerliche Identifikationsnummer: 12345678901"),
    case("tax-steuer-id-prefix-trap", "tax", "txt body, Steuer-Identifikationsnummer label", ["12345678901"],
         "t9.txt", "Steuer-Identifikationsnummer 12345678901"),
    case("tax-unified-13", "tax", "txt body, 13 digit unified format with label", ["1121081508150"],
         "t10.txt", "Steuernummer 1121081508150"),
    case("tax-unified-13-stnr", "tax", "txt body, 13 digits with StNr label", ["1121081508150"],
         "t11.txt", "StNr 1121081508150"),
    case("tax-dashes", "tax", "txt body, dashes", ["12-345-67890"],
         "t12.txt", "Steuernummer 12-345-67890"),
    case("tax-bw-format", "tax", "txt body, Baden-Wuerttemberg 5/5 format", ["12345/67890"],
         "t13.txt", "Steuernummer 12345/67890"),
    case("tax-hessen-spaces", "tax", "txt body, Hessen 3/3/5 with spaces", ["013 815 08153"],
         "t14.txt", "Steuernummer 013 815 08153"),
    # ------------------------------------------------------------------ not promised: no pattern claims these
    case("np-card", "other", "txt body, card number", ["4111 1111 1111 1111"],
         "n1.txt", "card 4111 1111 1111 1111 expires soon"),
    case("np-svnr", "other", "txt body, Sozialversicherungsnummer", ["12 010190 B 123"],
         "n2.txt", "svnr 12 010190 B 123"),
    case("np-id-card", "other", "txt body, id card and passport numbers", ["L01X00T47", "C01X0006H"],
         "n3.txt", "ausweis L01X00T47, pass C01X0006H"),
    case("np-plate", "other", "txt body, license plate", ["B-QX 1234"],
         "n4.txt", "kennzeichen B-QX 1234"),
    case("np-konto-blz", "other", "txt body, account number and BLZ", ["1234567890", "37040044"],
         "n5.txt", "konto 1234567890 blz 37040044"),
    case("np-coordinates", "other", "txt body, coordinates", ["52.5200, 13.4050"],
         "n6.txt", "site at 52.5200, 13.4050"),
    case("np-birthdate", "other", "txt body, date of birth", ["01.02.1990"],
         "n7.txt", "geboren am 01.02.1990"),
    case("np-platform-ids", "other", "txt body, 32 hex project id and a UUID",
         [NP_HEX, NP_UUID],
         "n8.txt", "project id %s, tenant %s" % (NP_HEX, NP_UUID)),
    case("np-access-keys", "other", "txt body, access key id and secret",
         [NP_ID20, NP_KEY42],
         "n9.txt", "ak=%s\nsk=%s" % (NP_ID20, NP_KEY42)),
    case("np-tokens", "other", "txt body, session and bearer tokens",
         [NP_SESSION, NP_BEARER],
         "n10.txt", "sessionid=%s\nauthorization: bearer %s" % (NP_SESSION, NP_BEARER)),
    case("np-config-password", "other", "txt body, passwords in a config file", ["Tr0ub4dor&3", "s3cret-pw-9"],
         "n11.txt", "[db]\npassword = Tr0ub4dor&3\n[obs]\nsecret = s3cret-pw-9"),
    case("np-jwt", "other", "txt body, a JWT (header and payload are base64)",
         [NP_JWT],
         "n12.txt", "token %s" % NP_JWT),
]


_PEM_B64 = base64.encodebytes(random.Random(7).randbytes(600)).decode().replace("\n", "")   # invented bytes, the same every run
_PEM_LINES = [_PEM_B64[i:i + 64] for i in range(0, len(_PEM_B64), 64)]


def _pem(inbox):
    body = ("-----BEGIN %s KEY-----\n" % PRIV + "\n".join(_PEM_LINES) + "\n-----END %s KEY-----\n\n" % PRIV
            + MARK + "\n")
    p = inbox / "n13.txt"
    p.write_text(body, encoding="utf-8")
    return p
CASES.append({"id": "np-private-key", "cls": "other", "carrier": "txt body, a private key block (random bytes)",
              "values": [_PEM_LINES[1]], "visible": [MARK], "build": _pem,
              "note": "the value is one base64 line of the block, set when the file is built"})


# ---------------------------------------------------------------------- round 2: boundaries of the escapes
ROUND2 = [
    # mail
    case("r2-mail-fullwidth-letter", "mail", "txt body, one fullwidth letter in the host (U+FF4D)", ["tberg@ｍailhost.example"],
         "m20.txt", "write to tberg@ｍailhost.example for the offer"),
    case("r2-mail-umlaut-local", "mail", "txt body, umlaut in the local part", ["tbärg@mailhost.example", "tbärg@"],
         "m21.txt", "write to tbärg@mailhost.example for the offer",
         "the domain is replaced, the local part with the @ stays"),
    case("r2-mail-at-domain-only", "mail", "txt body, (at) with a real domain", ["tberg (at) mailhost.example", "tberg (at)"],
         "m22.txt", "write to tberg (at) mailhost.example for the offer",
         "boundary of mail-at-dot-paren: the domain is replaced, the local part with (at) stays"),
    case("r2-md-escaped-mail", "mail", "md body, Markdown backslash escapes", ["tberg\\@mailhost\\.example", "tberg@mailhost.example"],
         "m23.md", "write to tberg\\@mailhost\\.example for the offer",
         "a Markdown viewer shows the address without the backslashes"),
    # url
    case("r2-url-wildcard-https", "url", "txt body, wildcard host in a url", ["https://*.mailhost.example/"],
         "u20.txt", "see https://*.mailhost.example/ for it", "boundary of url-wildcard"),
    case("r2-url-tlds-batch2", "url", "txt body, more TLDs not in _TLDS",
         ["qwxzv-portal.email", "qwxzv-portal.systems", "qwxzv-portal.digital", "qwxzv-portal.tech", "qwxzv-portal.ai",
          "qwxzv-portal.li", "qwxzv-portal.lt", "qwxzv-portal.si", "qwxzv-portal.hr", "qwxzv-portal.jp", "qwxzv-portal.cn",
          "qwxzv-portal.in", "qwxzv-portal.br", "qwxzv-portal.au", "qwxzv-portal.gov", "qwxzv-portal.edu",
          "qwxzv-portal.swiss", "qwxzv-portal.wien", "qwxzv-portal.koeln", "qwxzv-portal.cloud", "qwxzv-portal.co"],
         "u21.txt", "hosts: " + ", ".join("qwxzv-portal." + t for t in (
             "email", "systems", "digital", "tech", "ai", "li", "lt", "si", "hr", "jp", "cn", "in", "br", "au", "gov",
             "edu", "swiss", "wien", "koeln", "cloud", "co"))),
    case("r2-md-escaped-domain", "url", "md body, Markdown backslash escape of the dot", ["mailhost\\.example", "mailhost.example"],
         "u22.md", "the host is mailhost\\.example today", "a Markdown viewer shows the dot"),
    case("r2-url-dash-after-tld", "url", "txt body, a suffix after the TLD", ["mailhost.example-old"],
         "u23.txt", "the host mailhost.example-old is retired"),
    # ip
    case("r2-ip6-full-zeros", "ip", "txt body, uncompressed with leading zeros", ["2001:0db8:0000:0000:0000:0000:0000:0007"],
         "i20.txt", "host 2001:0db8:0000:0000:0000:0000:0000:0007 answers"),
    case("r2-ip6-embedded-ipv4", "ip", "txt body, IPv6 with an embedded IPv4", ["2001:db8::203.0.113.9"],
         "i21.txt", "host 2001:db8::203.0.113.9 answers"),
    case("r2-ip6-label-space", "ip", "txt body, label with colon and space", [IP6],
         "i22.txt", "host ip: 2001:db8::7 answers", "boundary of ip6-label-glued"),
    case("r2-md-escaped-ip", "ip", "md body, Markdown backslash escapes", ["203\\.0\\.113\\.9", IP4],
         "i23.md", "host 203\\.0\\.113\\.9 answers"),
    case("r2-ip4-leading-zero-mid", "ip", "txt body, zero padded middle octet", ["203.00.113.9"],
         "i24.txt", "host 203.00.113.9 answers"),
    # mac
    case("r2-mac-label-space", "mac", "txt body, label with colon and space", [MAC],
         "c20.txt", "port mac: 00:1a:2b:3c:4d:5e is up", "boundary of mac-label-glued"),
    case("r2-mac-cisco-dashes", "mac", "txt body, three groups with dashes", ["001a-2b3c-4d5e"],
         "c21.txt", "port 001a-2b3c-4d5e is up"),
    # iban
    case("r2-iban-space-after-country", "iban", "txt body, space after the country code", ["DE 83 " + grouped(IBAN_DE)[5:]],
         "b20.txt", "IBAN DE 83 %s" % grouped(IBAN_DE)[5:]),
    case("r2-iban-nbsp", "iban", "txt body, no-break spaces between groups", [grouped(IBAN_DE).replace(" ", " ")],
         "b21.txt", "IBAN %s" % grouped(IBAN_DE).replace(" ", " "), "expected caught: normalize folds the space"),
    case("r2-iban-wrap-after-five", "iban", "txt body, only the last group on the next line", [grouped(IBAN_DE)[:24] + "\n" + grouped(IBAN_DE)[25:]],
         "b22.txt", "IBAN %s\n%s" % (grouped(IBAN_DE)[:24], grouped(IBAN_DE)[25:])),
    case("r2-iban-md-table", "iban", "md table cell", [grouped(IBAN_DE)],
         "b23.md", "| konto | %s |\n|---|---|" % grouped(IBAN_DE), "expected caught"),
    # bic
    case("r2-bic-word-between", "bic", "txt body, a word between label and value", [BIC],
         "s20.txt", "BIC code: %s heute" % BIC, "boundary of bic-label-code"),
    case("r2-bic-country-not-listed", "bic", "txt body, GR and RO are in the IBAN table but not in _BIC_COUNTRIES",
         ["QWXZGRFF", "QWXZROFF"], "s21.txt", "BIC QWXZGRFF\nBIC QWXZROFF"),
    # vat
    case("r2-vat-space-in-digits", "vat", "txt body, spaces inside the digits only", ["DE123 456 789"],
         "v20.txt", "USt-IdNr.: DE123 456 789"),
    # phone
    case("r2-phone-first-group-two", "phone", "txt body, first subscriber group of two digits", ["030 12 345678"],
         "p30.txt", "call 030 12 345678 today", "boundary of phone-pairs"),
    case("r2-phone-plus-newline", "phone", "txt body, +49 with a line break after the area code", ["+49 30\n5551234"],
         "p31.txt", "call +49 30\n5551234 today", "expected caught: \\s takes the line break"),
    case("r2-phone-nbsp", "phone", "txt body, no-break spaces", ["+49 30 5551234"],
         "p32.txt", "call +49 30 5551234 today", "expected caught"),
    case("r2-phone-austria-plus", "phone", "txt body, Vienna with +43", ["+43 1 555 1234"],
         "p33.txt", "call +43 1 555 1234 today", "expected caught"),
    case("r2-phone-paren-zero", "phone", "txt body, (0)30 without country code", ["(0)30 5551234"],
         "p34.txt", "call (0)30 5551234 today"),
    case("r2-phone-paren-area-glued", "phone", "txt body, (030) glued to the number", ["(030)5551234"],
         "p35.txt", "call (030)5551234 today"),
    case("r2-phone-dash-groups", "phone", "txt body, dashes between every group", ["030-555-12-34"],
         "p36.txt", "call 030-555-12-34 today", "partial expected: the last group stays"),
    case("r2-md-escaped-phone", "phone", "md body, Markdown backslash escape of the dash", ["030\\-5551234", "030-5551234"],
         "p37.md", "call 030\\-5551234 today"),
    case("r2-phone-plus-dash-space", "phone", "txt body, +49 30 -number (one space)", ["+49 30 -5551234"],
         "p38.txt", "call +49 30 -5551234 today", "boundary of phone-plus-spaced-dash"),
    # hrb
    case("r2-hrb-nr-no-dot", "hrb", "txt body, HRB Nr without dot", ["HRB Nr " + HRB],
         "h20.txt", "register HRB Nr %s today" % HRB),
    case("r2-hrb-hyphen-nr", "hrb", "txt body, HRB-Nr.", ["HRB-Nr. " + HRB],
         "h21.txt", "register HRB-Nr. %s today" % HRB),
    case("r2-hrb-court-before", "hrb", "txt body, court in parentheses after", ["HRB " + HRB],
         "h22.txt", "register HRB %s (Amtsgericht %s) today" % (HRB, fx.PLACE_FORMS[0]), "expected caught"),
    # tax
    case("r2-tax-steuer-space-nr", "tax", "txt body, Steuer Nr. label with a space", ["12 345 67890"],
         "t20.txt", "Steuer Nr. 12 345 67890"),
    case("r2-tax-steuer-idnr", "tax", "txt body, Steuer-IdNr. label", ["12345678901"],
         "t21.txt", "Steuer-IdNr. 12345678901", "expected caught through the idnr alternative"),
    case("r2-tax-steuer-id-nr", "tax", "txt body, Steuer-Id-Nr. label", ["12345678901"],
         "t22.txt", "Steuer-Id-Nr. 12345678901"),
    case("r2-tax-tin", "tax", "txt body, TIN label", ["12345678901"],
         "t23.txt", "TIN 12345678901"),
    case("r2-tax-st-dot-nr", "tax", "txt body, St.Nr. label", ["12 345 67890"],
         "t24.txt", "St.Nr. 12 345 67890", "expected caught"),
    case("r2-tax-nrw-format", "tax", "txt body, NRW 3/4/4 with slashes, no label", ["133/8150/8159"],
         "t25.txt", "nummer 133/8150/8159", "expected caught"),
]
CASES.extend(ROUND2)


# ---------------------------------------------------------------------- round 3: Markdown carriers and a few more shapes
def _named_file(inbox):
    p = inbox / ("rechnung-%s-tel-030-5551234.txt" % IBAN_DE)
    p.write_text("the invoice text\n\n" + MARK + "\n", encoding="utf-8")
    return p


ROUND3 = [
    case("r3-md-comment-in-domain", "url", "md body, html comment inside the domain", ["mailhost<!-- x -->.example", "mailhost.example"],
         "u30.md", "the host is mailhost<!-- x -->.example today", "a Markdown viewer hides the comment"),
    case("r3-md-comment-in-iban", "iban", "md body, html comment inside the IBAN", [grouped(IBAN_DE)[:9] + "<!-- x -->" + grouped(IBAN_DE)[9:], grouped(IBAN_DE)],
         "b30.md", "IBAN %s<!-- x -->%s" % (grouped(IBAN_DE)[:9], grouped(IBAN_DE)[9:])),
    case("r3-md-comment-in-phone", "phone", "md body, html comment inside the number", ["+49 30 <!-- x -->5551234", "+49 30 5551234"],
         "p40.md", "call +49 30 <!-- x -->5551234 today"),
    case("r3-iban-zero-width", "iban", "txt body, zero width spaces inside", [grouped(IBAN_DE).replace(" ", "​")],
         "b31.txt", "IBAN %s" % grouped(IBAN_DE).replace(" ", "​"), "expected caught: normalize drops them"),
    case("r3-md-entity-dot", "url", "md body, &#46; for the dot", ["mailhost&#46;example", "mailhost.example"],
         "u31.md", "the host is mailhost&#46;example today", "expected caught: normalize decodes entities"),
    case("r3-md-autolink-mail", "mail", "md body, autolink in angle brackets", [MAIL],
         "m30.md", "write to <%s> for the offer" % MAIL, "expected caught"),
    case("r3-md-link-tel", "phone", "md link with a tel: target", ["+49305551234", "+49 30 5551234"],
         "p41.md", "[call +49 30 5551234](tel:+49305551234)", "expected caught"),
    case("r3-md-link-tld-not-listed", "url", "md link to a TLD not in _TLDS", ["https://qwxzv-portal.shop/", "qwxzv-portal.shop"],
         "u32.md", "[portal](https://qwxzv-portal.shop/)", "the url pattern does not use the TLD list, the bare domain does"),
    case("r3-md-code-span-ip", "ip", "md code span", [IP4],
         "i30.md", "host `%s` answers" % IP4, "expected caught"),
    case("r3-phone-plus-paren-slash", "phone", "txt body, +49 (0)30 / 555 1234", ["+49 (0)30 / 555 1234"],
         "p42.txt", "call +49 (0)30 / 555 1234 today"),
    case("r3-mail-defanged-domain", "mail", "txt body, [.] in the domain of an address", ["tberg@mailhost[.]example"],
         "m31.txt", "write to tberg@mailhost[.]example for the offer"),
    case("r3-hrb-md-table", "hrb", "md table cell", ["HRB " + HRB],
         "h30.md", "| register | HRB %s |\n|---|---|" % HRB, "expected caught"),
    {"id": "r3-filename-values", "cls": "iban", "carrier": "file name with an IBAN and a phone number",
     "values": [IBAN_DE, "030-5551234"], "visible": [MARK], "build": _named_file,
     "note": "the file name is scanned and never used for an output; the report must not carry it"},
]
CASES.extend(ROUND3)


# ---------------------------------------------------------------------- round 4: look-alikes, inline markup, more phone shapes
ROUND4 = [
    case("r4-md-empty-tag-domain", "url", "md body, empty inline tag inside the domain", ["mailhost<b></b>.example", "mailhost.example"],
         "u40.md", "the host is mailhost<b></b>.example today", "a Markdown viewer renders the tag as nothing"),
    case("r4-md-empty-tag-phone", "phone", "md body, empty inline tag inside the number", ["+49 30 <b></b>5551234", "+49 30 5551234"],
         "p50.md", "call +49 30 <b></b>5551234 today"),
    case("r4-iban-md-bold-group", "iban", "md body, one group in bold", ["DE83 **5550** 5550 5550 5550 68", grouped(IBAN_DE)],
         "b40.md", "IBAN DE83 **5550** 5550 5550 5550 68"),
    case("r4-url-cyrillic-o", "url", "txt body, Cyrillic o in the domain", ["mailhоst.example"],
         "u41.txt", "the host is mailhоst.example today", "the matcher folds look-alikes for names, the patterns do not"),
    case("r4-ip4-fullwidth-digits", "ip", "txt body, fullwidth digits", ["２０３.０.１１３.９"],
         "i40.txt", "host ２０３.０.１１３.９ answers"),
    case("r4-phone-cc-no-plus", "phone", "txt body, country code without + or 00", ["49 30 5551234"],
         "p51.txt", "call 49 30 5551234 today"),
    case("r4-phone-spaced-trunk-zero", "phone", "txt body, old German spacing 0 30 / 5 55 12 34", ["0 30 / 5 55 12 34"],
         "p52.txt", "call 0 30 / 5 55 12 34 today"),
    case("r4-phone-two-glued-slash", "phone", "txt body, two numbers with a slash between", ["030 5551234/030 5551235", "030 5551235"],
         "p53.txt", "tel/fax 030 5551234/030 5551235 today", "the first is caught, the second stands after a slash"),
    case("r4-iban-spread-letters", "iban", "txt body, every character spaced", [" ".join(IBAN_DE)],
         "b41.txt", "IBAN %s" % " ".join(IBAN_DE)),
    case("r4-mail-md-bold-local", "mail", "md body, bold local part", ["**tberg**@mailhost.example", "**tberg**@"],
         "m40.md", "write to **tberg**@mailhost.example for the offer"),
]
CASES.extend(ROUND4)


# ---------------------------------------------------------------------- round 5: encoded carriers (expected caught) and last boundaries
_B64_IBAN = base64.b64encode(("IBAN %s" % grouped(IBAN_DE)).encode()).decode()
_HEX_PHONE = "call +49 30 5551234 now".encode().hex()
ROUND5 = [
    case("r5-b64-iban", "iban", "txt body, base64 of a line with the IBAN", [_B64_IBAN, grouped(IBAN_DE)],
         "b50.txt", "blob %s end" % _B64_IBAN, "expected caught: the block is expanded before matching"),
    case("r5-hex-phone", "phone", "txt body, hex of a line with the number", [_HEX_PHONE, "+49 30 5551234"],
         "p60.txt", "blob %s end" % _HEX_PHONE, "expected caught"),
    case("r5-md-data-uri", "iban", "md image with a data: uri carrying the IBAN", [_B64_IBAN, grouped(IBAN_DE)],
         "b51.md", "![x](data:text/plain;base64,%s)" % _B64_IBAN, "expected caught"),
    case("r5-json-escape-at", "mail", "txt body, \\u0040 for the @", ["tberg\\u0040mailhost.example", MAIL],
         "m50.txt", "write to tberg\\u0040mailhost.example for the offer", "expected caught"),
    case("r5-percent-dot", "url", "txt body, %2E for the dot", ["mailhost%2Eexample", DOM],
         "u50.txt", "the host is mailhost%2Eexample today", "expected caught"),
    case("r5-ip-nr-label", "ip", "txt body, IP-Nr. label in front", [IP4],
         "i50.txt", "server IP-Nr. %s answers" % IP4, "the section reference rule drops a dotted number after Nr."),
    case("r5-ip-no-label", "ip", "txt body, no. label in front", [IP4],
         "i51.txt", "server no. %s answers" % IP4),
    case("r5-url-at-prefixed", "url", "txt body, @ in front of the domain", ["@mailhost.example"],
         "u51.txt", "reach us @mailhost.example today"),
    case("r5-phone-plus-in-parens-whole", "phone", "txt body, whole number in parentheses", ["+49 30 5551234"],
         "p61.txt", "call (+49 30 5551234) today", "expected caught"),
    case("r5-iban-lower-spaces-label-glued", "iban", "txt body, iban: glued, lower case, spaces", [grouped(IBAN_DE).lower()],
         "b52.txt", "iban:%s heute" % grouped(IBAN_DE).lower(), "expected caught"),
]
CASES.extend(ROUND5)


# ---------------------------------------------------------------------- round 6: the hex letter-ratio boundary
_HEX_IBAN = ("IBAN %s" % grouped(IBAN_DE)).encode().hex()
_HEX_PROSE = "please call our office at +49 30 5551234 tomorrow morning for the details".encode().hex()
_HEX_IP = ("host %s answers" % IP4).encode().hex().upper()
ROUND6 = [
    case("r6-hex-iban", "iban", "txt body, hex of a line with the IBAN (digits heavy)", [_HEX_IBAN, grouped(IBAN_DE)],
         "b60.txt", "blob %s end" % _HEX_IBAN),
    case("r6-hex-prose-phone", "phone", "txt body, hex of a prose line with the number (letters heavy)", [_HEX_PROSE, "+49 30 5551234"],
         "p70.txt", "blob %s end" % _HEX_PROSE, "expected caught: more than half letters"),
    case("r6-hex-ip-upper", "ip", "txt body, upper case hex of a short line with the address", [_HEX_IP, IP4],
         "i60.txt", "blob %s end" % _HEX_IP),
]
CASES.extend(ROUND6)


# What tests/test_redteam_pack.py accepts besides the fixture forms. DERIVED: strings built from a fixture form
# (a typo, an encoding, a transliteration). INVENTED: structured values this module builds, invented but valid
# in shape (documentation ranges, fake digit families). A value may carry one of them whole or in part.
INVENTED_LABELS = ("qwxzv-portal",)      # an invented host label under real top level domains (the TLD is the point)
DERIVED = ()
INVENTED = (
    MAIL, "tbärg@mailhost.example", '"t berg"@mailhost.example', DOM, "qwxzv-portal", "xn--qwxzv-8ka",
    IP4, "203.0.113.20", "203.000.113.007", "203.0.113.09", "203.00.113.9", "3405803785", "0313.0.0161.011",
    "0xCB007109", IP6, "2001:0db8:0000:0000:0000:0000:0000:0007", MAC, BIC, "QWXZDE2F", "QWXZGRFF", "QWXZROFF",
    VAT, "SE123456789001", "IE1234567T", "GB123456789", "PT123456789", "FI12345678", "HU12345678", "SK1234567890",
    "ATU 1234 5678", "FR12 345678901", HRB,
    "030 5551234", "+49 30 5551234", "040 5551234", "030 5551235", "030 12 34 56 78", "01 555 1234", "044 555 12 34",
    "0171 555 12 34", "+43 1 555 1234", "0800-FLOWERS",
    "12 345 67890", "12345678901", "1121081508150", "013 815 08153", "133/8150/8159", "12345/67890",
    IBAN_DE, IBAN_LT, IBAN_DE[:-1] + "9",
    # the classes no pattern claims (np-* cases): documentation examples and invented ids, keys and tokens
    "4111 1111 1111 1111", "12 010190 B 123", "L01X00T47", "C01X0006H", "B-QX 1234", "1234567890", "37040044",
    "52.5200, 13.4050", "01.02.1990", NP_HEX, NP_UUID, NP_ID20, NP_KEY42, NP_SESSION, NP_BEARER, "Tr0ub4dor&3",
    "s3cret-pw-9", NP_JWT, _PEM_LINES[1],
) + tuple(IBAN_COUNTRIES.values())
