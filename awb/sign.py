"""Request signing for cloud API gateways with an access key and a secret key: SDK-HMAC-SHA256.

The canonical request is six lines: the method; the canonical URI (the path unquoted, every segment
percent-encoded again, always ending in a slash); the canonical query (keys and values percent-encoded, sorted by
key, the values of one key sorted); the canonical headers (lower-case name, colon, trimmed value, one per line,
sorted); the signed header names joined by semicolons; the hex SHA-256 of the body. The string to sign is the
algorithm name, the X-Sdk-Date value and the hex SHA-256 of the canonical request, one per line. The signature is
the hex HMAC-SHA256 of that string under the secret key.

Neutral: the algorithm is the same for more than one platform, so it lives in the core and a platform package
imports it, never the other way round. Proven live against the identity service of T Cloud Public (TCP).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.parse

ALGORITHM = "SDK-HMAC-SHA256"
DATE_FORMAT = "%Y%m%dT%H%M%SZ"
_SAFE = "-_.~"


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe=_SAFE)


def canonical_uri(path: str) -> str:
    """Every segment of `path` unquoted, then percent-encoded; a trailing slash always."""
    uri = "/".join(_quote(urllib.parse.unquote(seg)) for seg in (path or "/").split("/"))
    return uri if uri.endswith("/") else uri + "/"


def canonical_query(query: str) -> str:
    """`k=v` pairs percent-encoded, sorted by key and, within one key, by value; blank values kept."""
    groups: dict[str, list[str]] = {}
    for k, v in urllib.parse.parse_qsl(query or "", keep_blank_values=True):
        groups.setdefault(k, []).append(v)
    return "&".join("%s=%s" % (_quote(k), _quote(v)) for k in sorted(groups) for v in sorted(groups[k]))


def string_to_sign(method: str, url: str, headers: dict[str, str], body: bytes, date: str) -> tuple[str, str]:
    """(string to sign, signed header names) for a request whose headers already carry host and x-sdk-date."""
    parts = urllib.parse.urlsplit(url)
    lower = {k.lower(): str(v).strip() for k, v in headers.items()}
    signed = sorted(lower)
    canonical = "\n".join([
        method.upper(),
        canonical_uri(parts.path),
        canonical_query(parts.query),
        "".join("%s:%s\n" % (k, lower[k]) for k in signed),
        ";".join(signed),
        hashlib.sha256(body or b"").hexdigest(),
    ])
    return "\n".join([ALGORITHM, date, hashlib.sha256(canonical.encode("utf-8")).hexdigest()]), ";".join(signed)


def sign(method: str, url: str, headers: dict[str, str] | None, body: bytes | None, ak: str, sk: str, *,
         now: datetime.datetime | None = None) -> dict[str, str]:
    """The headers to send: `headers` plus Host, X-Sdk-Date and Authorization. Every header given is signed."""
    date = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(DATE_FORMAT)
    out = {k: v for k, v in (headers or {}).items() if k.lower() not in ("host", "x-sdk-date", "authorization")}
    out["Host"] = urllib.parse.urlsplit(url).netloc
    out["X-Sdk-Date"] = date
    sts, signed = string_to_sign(method, url, out, body or b"", date)
    signature = hmac.new(sk.encode("utf-8"), sts.encode("utf-8"), hashlib.sha256).hexdigest()
    out["Authorization"] = "%s Access=%s, SignedHeaders=%s, Signature=%s" % (ALGORITHM, ak, signed, signature)
    return out
