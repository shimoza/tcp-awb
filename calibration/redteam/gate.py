"""Red-team cases for the commit gate (dimension: gate). Invented values only; registered forms from tests/fixtures.py."""
import base64
import bz2
import json
import math
import sys
import zlib
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

CUST = fx.CUSTOMER_FORMS[0]      # full legal form, three words
SHORT = fx.CUSTOMER_FORMS[1]     # one word, five letters
PERSON = fx.PERSON_FORMS[0]
SURNAME = fx.PERSON_FORMS[1]
ORG = fx.ORG_FORMS[0]            # carries umlauts
PLACE = fx.PLACE_FORMS[0]        # one word, 13 letters


def _entropy(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


# Every planted value is built from pieces (as tests/test_gate.py and the selftest of awb/gate.py do), so that
# the commit gate does not read this file as a leak. The pieces join to the same strings the red team used.
# an invented high-entropy value: 23 distinct characters, entropy 4.5 bits, so a miss is never the value's fault
SECRET_A = "Vb7Qk2Zn9Lp"
SECRET_B = "4Rw8Xm3Ct6Yd"
SECRET = SECRET_A + SECRET_B
assert len(SECRET) >= 16 and _entropy(SECRET) >= 3.5
AK = "Q7M4KX2AB3" + "NP9ZTW5RHY"                       # 20 capitals and digits
SK = "k7Qz2Lp9Wx4Rv8" + "Nb3Mc6Yd1Tf5H" + "g0Js7Ka2Le4Pq"   # 40 letters and digits, both cases
assert len(AK) == 20 and len(SK) == 40 and _entropy(SK) >= 4.0
HEX32 = "4f1c9a7e2b6d40f8" + "a3c5e9b1d7f2a6c0"
UUID = "4f1c9a7e-2b6d-40f8" + "-a3c5-e9b1d7f2a6c0"
TENANT = "482" + "13"
B62 = "Zx8Kq3Lw7Rt2Vn5Bm9Y" + "c4Hd6Jf1Gs0Pa3Ue7Wi9Qm"   # 38 chars, letters and digits
SNAKE_A = "k7q2zp9lw4rx8vn3"                            # two halves of a lower-case alnum value
SNAKE_B = "bm6yc0dh5t1"
PRIV = "PRI" + "VATE"                                   # the word of every key armour
OTC = "leg" + "acy"                                     # the prefix of the blocked folders of tests/blocklist.txt
HOME = "/ho" + "me/"                                    # the home folder of a Linux user
USERS = "/Us" + "ers/"                                  # the home folder of a macOS user
WINUSERS = "C:\\Us" + "ers\\"                           # the home folder of a Windows user


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


JWT_HEAD = _b64url(b'{"alg":"HS256","typ":"JWT"}')
JWT_BODY = _b64url(b'{"sub":"builder","iat":1}')
JWT = JWT_HEAD + "." + JWT_BODY + "." + B62
assert "-" not in JWT, "the dotted-reference case needs a JWT without a hyphen"

PEM_BODY = base64.b64encode(bytes(range(48, 112))).decode()          # 88 chars, invented bytes, not a key
PEM = "-----BEGIN RSA %s KEY-----\n%s\n%s\n-----END RSA %s KEY-----\n" % (PRIV, PEM_BODY, PEM_BODY[:40], PRIV)
KEY32 = base64.b64encode(bytes(range(200, 232))).decode()            # 44 chars, invented
KEY64 = base64.b64encode(bytes(range(100, 164))).decode()            # 88 chars, invented

FILLER = "line of filler text number %d with nothing in it\n"


def _text_after_8k(tail: bytes) -> bytes:
    head = "".join(FILLER % i for i in range(200)).encode()
    assert len(head) > 8192
    return head + tail


def _pdf(stream: bytes, flate: bool) -> bytes:
    body = zlib.compress(stream) if flate else stream
    filt = b" /Filter /FlateDecode" if flate else b""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d%s >>\nstream\n" % (len(body), filt) + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


PDF_TEXT = ("BT /F1 12 Tf 72 700 Td (kunde %s) Tj ET" % CUST).encode()
PDF_HEX = ("BT /F1 12 Tf 72 700 Td <%s> Tj ET" % ("kunde %s" % CUST).encode().hex()).encode()
BZ2 = bz2.compress(("kunde %s heute\n" % CUST).encode() * 3)
IPYNB = json.dumps({"cells": [{"cell_type": "code", "source": ["print(x)"], "outputs": [
    {"output_type": "stream", "name": "stdout", "text": ["kunde \u001b[1m%s\u001b[0m%s\n" % (PLACE[:8], PLACE[8:])]}]}],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, indent=1)
BIG = ("".join(FILLER % i for i in range(250000)) + "kunde %s heute\n" % CUST)   # about 12 MB

CASES = [
    # ----------------------------------------------------------------- secret: shapes _ASSIGN_RE misses
    {"id": "s-flag-eq", "cls": "secret", "carrier": "cli flag --password=value in a shell script", "path": "dump.sh",
     "content": "mysqldump --user=app --password=%s app > dump.sql\n" % SECRET},
    {"id": "s-flag-space", "cls": "secret", "carrier": "cli flag -p value", "path": "run.sh",
     "content": "sshpass -p %s ssh app@db.example\n" % SECRET},
    {"id": "s-mysql-p-glued", "cls": "secret", "carrier": "mysql -p<value> glued", "path": "run.sh",
     "content": "mysql -uapp -p%s app\n" % SECRET},
    {"id": "s-java-D", "cls": "secret", "carrier": "java -Ddb.password=value", "path": "start.sh",
     "content": "java -Ddb.password=%s -jar app.jar\n" % SECRET},
    {"id": "s-word-space", "cls": "secret", "carrier": "'password value' without = or :", "path": "smtp.conf",
     "content": "host mail.example\nuser builder\npassword %s\n" % SECRET},
    {"id": "s-netrc", "cls": "secret", "carrier": ".netrc line", "path": ".netrc",
     "content": "machine api.example login builder password %s\n" % SECRET},
    {"id": "s-pgpass", "cls": "secret", "carrier": ".pgpass line", "path": ".pgpass",
     "content": "db.example:5432:app:builder:%s\n" % SECRET},
    {"id": "s-htpasswd", "cls": "secret", "carrier": "htpasswd apr1 hash", "path": ".htpasswd",
     "content": "builder:$apr1$Zx8Kq3Lw$Rt2Vn5Bm9Yc4Hd6Jf1Gs0P\n"},
    {"id": "s-basic-auth", "cls": "secret", "carrier": "Authorization: Basic base64(user:pass)", "path": "req.http",
     "content": "GET /v1 HTTP/1.1\nAuthorization: Basic %s\n" % base64.b64encode(("builder:" + SECRET).encode()).decode()},
    {"id": "s-bearer-jwt", "cls": "secret", "carrier": "Authorization: Bearer JWT", "path": "req.http",
     "content": "GET /v1 HTTP/1.1\nAuthorization: Bearer %s\n" % JWT},
    {"id": "s-url-userinfo", "cls": "secret", "carrier": "DATABASE_URL with the password in the userinfo", "path": ".env",
     "content": "DATABASE_URL=postgres://builder:%s@db.example:5432/app\n" % SECRET},
    {"id": "s-git-credentials", "cls": "secret", "carrier": ".git-credentials url", "path": ".git-credentials",
     "content": "https://builder:%s@git.example\n" % SECRET},
    {"id": "s-kubeconfig", "cls": "secret", "carrier": "kubeconfig client-key-data base64 PEM", "path": "kubeconfig.yaml",
     "content": "users:\n- name: builder\n  user:\n    client-certificate-data: %s\n    client-key-data: %s\n" % (
         KEY64, base64.b64encode(PEM.encode()).decode())},
    {"id": "s-docker-auth", "cls": "secret", "carrier": "docker config.json auth entry", "path": "config.json",
     "content": json.dumps({"auths": {"registry.example": {"auth": base64.b64encode(("builder:" + SECRET).encode()).decode()}}}, indent=1)},
    {"id": "s-tfvars", "cls": "secret", "carrier": "terraform tfvars (control, expected caught)", "path": "prod.tfvars",
     "content": 'region = "eu-de"\ndb_password = "%s"\n' % SECRET},
    {"id": "s-hcl-heredoc", "cls": "secret", "carrier": "HCL heredoc value on the next line", "path": "main.tf",
     "content": 'password = <<EOT\n%s\nEOT\n' % SECRET},
    {"id": "s-yaml-block", "cls": "secret", "carrier": "YAML block scalar, value on the next line", "path": "values.yaml",
     "content": "db:\n  password: |\n    %s\n" % SECRET},
    {"id": "s-yaml-nextline", "cls": "secret", "carrier": "YAML key with the value on the next line", "path": "values.yaml",
     "content": "db:\n  password:\n    %s\n" % SECRET},
    {"id": "s-json-nextline", "cls": "secret", "carrier": "JSON key on one line, value on the next", "path": "conf.json",
     "content": '{\n  "password":\n    "%s"\n}\n' % SECRET},
    {"id": "s-spaces-unquoted", "cls": "secret", "carrier": "unquoted value with spaces (by design low entropy words)", "path": "app.ini",
     "content": "password = Vb7Qk2Zn9 Lp4Rw8Xm3 Ct6Yd Qz\n"},
    {"id": "s-de-passwort", "cls": "secret", "carrier": "German key Passwort", "path": "app.ini",
     "content": "Passwort=%s\n" % SECRET},
    {"id": "s-de-kennwort", "cls": "secret", "carrier": "German key Kennwort", "path": "app.ini",
     "content": "Kennwort: %s\n" % SECRET},
    {"id": "s-de-geheimnis", "cls": "secret", "carrier": "German key Geheimnis", "path": "app.ini",
     "content": 'Geheimnis = "%s"\n' % SECRET},
    {"id": "s-fr-motdepasse", "cls": "secret", "carrier": "French key mot_de_passe", "path": "app.ini",
     "content": 'mot_de_passe = "%s"\n' % SECRET},
    {"id": "s-passphrase", "cls": "secret", "carrier": "PASSPHRASE key", "path": ".env",
     "content": "PASSPHRASE=%s\n" % SECRET},
    {"id": "s-credentials", "cls": "secret", "carrier": "credentials key", "path": "app.ini",
     "content": 'credentials = "%s"\n' % SECRET},
    {"id": "s-auth", "cls": "secret", "carrier": "auth key", "path": "app.ini",
     "content": 'auth = "%s"\n' % SECRET},
    {"id": "s-bearer-key", "cls": "secret", "carrier": "bearer key with a JWT", "path": "app.ini",
     "content": 'bearer = "%s-x"\n' % JWT},
    {"id": "s-client-secret", "cls": "secret", "carrier": "client_secret next to client_id (control)", "path": "app.ini",
     "content": 'client_id = "app-01"\nclient_secret = "%s"\n' % SECRET},
    {"id": "s-backslash-split", "cls": "secret", "carrier": "shell value split with a backslash line continuation", "path": "run.sh",
     "content": "PASSWORD=%s\\\n%s\n" % (SECRET_A, SECRET_B)},
    {"id": "s-concat", "cls": "secret", "carrier": "string concatenation in code", "path": "conf.py",
     "content": 'password = "%s" + "%s"\n' % (SECRET_A, SECRET_B)},
    {"id": "s-b64-line", "cls": "secret", "carrier": "base64 of a key=value line", "path": "conf.b64",
     "content": base64.b64encode(("password=%s\n" % SECRET).encode()).decode() + "\n"},
    {"id": "s-percent", "cls": "secret", "carrier": "percent-encoded value", "path": ".env",
     "content": "password=%s%%26%s\n" % (SECRET[:9], SECRET[9:])},
    {"id": "s-argparse-default", "cls": "secret", "carrier": "argparse default", "path": "cli.py",
     "content": 'parser.add_argument("--password", default="%s")\n' % SECRET},
    {"id": "s-docstring", "cls": "secret", "carrier": "docstring prose", "path": "conf.py",
     "content": '"""Connect with the password %s on the test box."""\n' % SECRET},
    {"id": "s-comment", "cls": "secret", "carrier": "comment line (control)", "path": "conf.py",
     "content": "# password: %s\n" % SECRET},
    {"id": "s-heredoc", "cls": "secret", "carrier": "shell here-document (control)", "path": "setup.sh",
     "content": "cat > /etc/app.conf <<EOF\npassword = %s\nEOF\n" % SECRET},
    {"id": "s-db-pass", "cls": "secret", "carrier": "DB_PASS key", "path": ".env",
     "content": "DB_PASS=%s\n" % SECRET},
    {"id": "s-db-pw", "cls": "secret", "carrier": "DB_PW key", "path": ".env",
     "content": "DB_PW=%s\n" % SECRET},
    {"id": "s-passWord-camel", "cls": "secret", "carrier": "PassWord camel case", "path": "app.ini",
     "content": "PassWord=%s\n" % SECRET},
    {"id": "s-pass-word-snake", "cls": "secret", "carrier": "pass_word snake", "path": "app.ini",
     "content": "pass_word=%s\n" % SECRET},
    {"id": "s-2fa-secret", "cls": "secret", "carrier": "key starting with a digit", "path": ".env",
     "content": "2FA_SECRET=%s\n" % SECRET},
    {"id": "s-xml-element", "cls": "secret", "carrier": "XML element <password>", "path": "settings.xml",
     "content": "<server><id>repo</id><username>builder</username><password>%s</password></server>\n" % SECRET},
    {"id": "s-xml-keyvalue", "cls": "secret", "carrier": "XML key/value attributes", "path": "web.config",
     "content": '<add key="password" value="%s" />\n' % SECRET},
    {"id": "s-quoted-bang", "cls": "secret", "carrier": "quoted value starting with !", "path": "app.ini",
     "content": 'password = "!%s"\n' % SECRET},
    {"id": "s-quoted-at", "cls": "secret", "carrier": "quoted value starting with @", "path": "app.ini",
     "content": 'password = "@%s"\n' % SECRET},
    {"id": "s-paren-inside", "cls": "secret", "carrier": "quoted value with parentheses inside", "path": "app.ini",
     "content": 'password = "%s(%s)%s"\n' % (SECRET[:6], SECRET[6:9], SECRET[9:])},
    {"id": "s-comma-unquoted", "cls": "secret", "carrier": "unquoted value with a comma", "path": ".env",
     "content": "password=%s,%s\n" % (SECRET[:9], SECRET[9:])},
    {"id": "s-snake-lower", "cls": "secret", "carrier": "lower-case alnum value with one underscore", "path": "app.ini",
     "content": "secret = %s_%s\n" % (SNAKE_A, SNAKE_B)},
    {"id": "s-jwt-dotted", "cls": "secret", "carrier": "token = JWT without a hyphen", "path": "app.ini",
     "content": "token = %s\n" % JWT},
    {"id": "s-yaml-anchor", "cls": "secret", "carrier": "YAML anchor before the value", "path": "values.yaml",
     "content": "password: &pw %s\n" % SECRET},
    {"id": "s-yaml-tag", "cls": "secret", "carrier": "YAML !!str tag before the value", "path": "values.yaml",
     "content": "password: !!str %s\n" % SECRET},
    {"id": "s-azure-accountkey", "cls": "secret", "carrier": "Azure storage connection string AccountKey", "path": ".env",
     "content": "STORAGE=DefaultEndpointsProtocol=https;AccountName=stor01;AccountKey=%s;EndpointSuffix=core.windows.net\n" % KEY64},
    {"id": "s-wireguard", "cls": "secret", "carrier": "WireGuard PrivateKey =", "path": "wg0.conf",
     "content": "[Interface]\nPrivateKey = %s\nAddress = 10.0.0.2/32\n" % KEY32},
    {"id": "s-md-table", "cls": "secret", "carrier": "markdown table of credentials", "path": "handover.md",
     "content": "| user | password |\n|---|---|\n| builder | %s |\n" % SECRET},
    {"id": "s-csv", "cls": "secret", "carrier": "csv with a password column", "path": "users.csv",
     "content": "user,password\nbuilder,%s\n" % SECRET},
    {"id": "s-azure-sas", "cls": "secret", "carrier": "Azure SAS url sig=", "path": "notes.md",
     "content": "https://stor01.blob.core.windows.net/c/x.bin?sv=2024-01-01&ss=b&sig=%s%%3D\n" % B62},
    # ----------------------------------------------------------------- key pairs
    {"id": "k-sk-3-lines-later", "cls": "secret", "carrier": "key id, two blank lines, secret", "path": "creds.txt",
     "content": "%s\n\n\n%s\n" % (AK, SK)},
    {"id": "k-sk-with-slash", "cls": "secret", "carrier": "secret with a slash and a plus", "path": "creds.txt",
     "content": "%s\n%s\n" % (AK, SK[:12] + "/" + SK[13:30] + "+" + SK[31:])},
    {"id": "k-ak-lower", "cls": "secret", "carrier": "lower-case key id then the secret", "path": "creds.txt",
     "content": "%s %s\n" % (AK.lower(), SK)},
    {"id": "k-sk-alone", "cls": "secret", "carrier": "secret of 40 chars alone, id in another file", "path": "creds.txt",
     "content": "the second half is\n%s\n" % SK},
    {"id": "k-json-generic-keys", "cls": "secret", "carrier": "pair in JSON under generic keys (control)", "path": "creds.json",
     "content": '{"id": "%s", "value": "%s"}\n' % (AK, SK)},
    {"id": "k-pair-named", "cls": "secret", "carrier": "ak: / sk: (control)", "path": "creds.yaml",
     "content": "ak: %s\nsk: %s\n" % (AK, SK)},
    {"id": "k-sk-before-ak", "cls": "secret", "carrier": "secret on the line before the key id", "path": "creds.txt",
     "content": "%s\n%s\n" % (SK, AK)},
    # ----------------------------------------------------------------- token prefixes
    {"id": "t-ghp-control", "cls": "token", "carrier": "ghp_ (control)", "path": "n.md", "content": "ghp_%s\n" % B62[:36]},
    {"id": "t-gho", "cls": "token", "carrier": "GitHub gho_", "path": "n.md", "content": "gho_%s\n" % B62[:36]},
    {"id": "t-ghs", "cls": "token", "carrier": "GitHub ghs_", "path": "n.md", "content": "ghs_%s\n" % B62[:36]},
    {"id": "t-xapp", "cls": "token", "carrier": "Slack xapp-", "path": "n.md",
     "content": "xa" + "pp-1-A0Q7M4KX2-1234567890123-%s\n" % B62},
    {"id": "t-xoxe", "cls": "token", "carrier": "Slack xoxe-", "path": "n.md", "content": "xoxe-1-%s\n" % B62},
    {"id": "t-slack-webhook", "cls": "token", "carrier": "Slack webhook url", "path": "n.md",
     "content": "https://hooks.slack.com/services/T0Q7M4KX2/B0AB3NP9Z/%s\n" % B62[:24]},
    {"id": "t-glpat", "cls": "token", "carrier": "GitLab glpat-", "path": "n.md", "content": "glpat-%s\n" % B62[:20]},
    {"id": "t-aiza", "cls": "token", "carrier": "Google AIza", "path": "n.md", "content": "AIzaSy%s\n" % B62[:33]},
    {"id": "t-sendgrid", "cls": "token", "carrier": "SendGrid SG.", "path": "n.md",
     "content": "SG.%s.%s\n" % (B62[:22], (B62 + B62)[:43])},
    {"id": "t-npm", "cls": "token", "carrier": "npm_", "path": ".npmrc", "content": "//registry.npmjs.org/:_authToken=npm_%s\n" % B62[:36]},
    {"id": "t-pypi", "cls": "token", "carrier": "pypi-", "path": ".pypirc",
     "content": "[pypi]\nusername = __token__\npassword = pypi-AgEIcHlwaS5vcmcCJ%s\n" % (B62 + B62)},
    {"id": "t-hf", "cls": "token", "carrier": "hf_ prefix", "path": "n.md", "content": "hf_%s\n" % B62[:34]},
    {"id": "t-stripe", "cls": "token", "carrier": "Stripe sk_live_", "path": "n.md", "content": "sk_live_%s\n" % B62[:24]},
    {"id": "t-telegram", "cls": "token", "carrier": "Telegram bot token", "path": "n.md",
     "content": "bot 1234567890:AAF%s\n" % B62[:32]},
    {"id": "t-discord", "cls": "token", "carrier": "Discord bot token", "path": "n.md",
     "content": "MTIzNDU2Nzg5MDEyMzQ1Njc4.Gabcde.%s\n" % B62},
    {"id": "t-twilio-sk", "cls": "token", "carrier": "Twilio SK + 32 hex", "path": "n.md", "content": "SK%s\n" % HEX32},
    {"id": "t-jwt-bare", "cls": "token", "carrier": "bare JWT in prose", "path": "n.md", "content": "use %s here\n" % JWT},
    {"id": "t-asia", "cls": "token", "carrier": "AWS temporary key id ASIA", "path": "n.md", "content": "id ASIA%s\n" % AK[:16]},
    {"id": "t-sk-ant", "cls": "token", "carrier": "Anthropic sk-ant- (control)", "path": "n.md",
     "content": "sk-ant-api03-%s-%sAA\n" % (B62, B62[:6])},
    {"id": "t-age-key", "cls": "private-key", "carrier": "age secret key", "path": "keys.txt",
     "content": "AGE-SECRET-" + "KEY-1QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7LQPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L\n"},
    # ----------------------------------------------------------------- private keys
    {"id": "p-ppk", "cls": "private-key", "carrier": "PuTTY .ppk v3", "path": "id.ppk",
     "content": ("PuTTY-User-" + "Key-File-3: ssh-ed25519\nEncryption: none\nComment: builder\nPublic-Lines: 2\n%s\n%s\nPrivate-"
                 + "Lines: 1\n%s\nPrivate-MAC: %s\n") % (KEY32, KEY32, KEY32, "0123456789abcdef" * 4)},
    {"id": "p-body-only", "cls": "private-key", "carrier": "PEM body without armour lines", "path": "id_rsa",
     "content": "%s\n%s\n%s\n" % (PEM_BODY[:64], PEM_BODY[:64], PEM_BODY[64:])},
    {"id": "p-b64-pem", "cls": "private-key", "carrier": "base64 of a whole PEM", "path": "key.b64",
     "content": base64.b64encode(PEM.encode()).decode() + "\n"},
    {"id": "p-jwk", "cls": "private-key", "carrier": "JWK with d", "path": "jwk.json",
     "content": json.dumps({"kty": "OKP", "crv": "Ed25519", "x": _b64url(bytes(range(32))), "d": _b64url(bytes(range(100, 132)))})},
    {"id": "p-oneline-backslash-n", "cls": "private-key", "carrier": "SSH key on one line with backslash-n (control)", "path": "vars.yml",
     "content": 'key: "%s"\n' % PEM.replace("\n", "\\n")},
    {"id": "p-armour-spaces", "cls": "private-key", "carrier": "armour with spaces inside the dashes", "path": "id_rsa",
     "content": "----- BEGIN RSA %s KEY -----\n%s\n----- END RSA %s KEY -----\n" % (PRIV, PEM_BODY, PRIV)},
    {"id": "p-ssh2-armour", "cls": "private-key", "carrier": "ssh.com SSH2 armour (four dashes, spaces)", "path": "id_ssh2",
     "content": "---- BEGIN SSH2 ENCRYPTED %s KEY ----\nComment: builder\n%s\n---- END SSH2 ENCRYPTED %s KEY ----\n" % (PRIV, PEM_BODY, PRIV)},
    {"id": "p-armour-tab", "cls": "private-key", "carrier": "armour with a tab", "path": "id_rsa",
     "content": "-----BEGIN RSA\t%s KEY-----\n%s\n" % (PRIV, PEM_BODY)},
    {"id": "p-armour-lower", "cls": "private-key", "carrier": "armour in lower case", "path": "id_rsa",
     "content": "-----begin rsa %s key-----\n%s\n-----end rsa %s key-----\n" % (PRIV.lower(), PEM_BODY, PRIV.lower())},
    {"id": "p-armour-crlf", "cls": "private-key", "carrier": "Windows line endings (control)", "path": "id_rsa",
     "content": PEM.replace("\n", "\r\n")},
    {"id": "p-armour-endash", "cls": "private-key", "carrier": "dashes autocorrected to en dashes", "path": "notes.md",
     "content": "\u2013\u2013\u2013\u2013\u2013BEGIN RSA %s KEY\u2013\u2013\u2013\u2013\u2013\n%s\n" % (PRIV, PEM_BODY)},
    {"id": "p-armour-4dashes", "cls": "private-key", "carrier": "four dashes only", "path": "id_rsa",
     "content": "----BEGIN RSA %s KEY----\n%s\n" % (PRIV, PEM_BODY)},
    {"id": "p-armour-split", "cls": "private-key", "carrier": "armour wrapped over two lines", "path": "mail.txt",
     "content": "-----BEGIN RSA %s\nKEY-----\n%s\n" % (PRIV, PEM_BODY)},
    {"id": "p-armour-double-space", "cls": "private-key", "carrier": "two spaces in the armour", "path": "id_rsa",
     "content": "-----BEGIN RSA  %s KEY-----\n%s\n" % (PRIV, PEM_BODY)},
    # ----------------------------------------------------------------- home paths
    {"id": "h-users-mac", "cls": "homepath", "carrier": "/Users/name (macOS)", "path": "n.md",
     "content": "saved to %sbuilder/Documents/offer.docx\n" % USERS},
    {"id": "h-users-win", "cls": "homepath", "carrier": "C:\\Users\\name", "path": "n.md",
     "content": "saved to %sbuilder\\Documents\\offer.docx\n" % WINUSERS},
    {"id": "h-root", "cls": "homepath", "carrier": "/root", "path": "n.md", "content": "key in /ro" + "ot/.ssh/id_ed25519\n"},
    {"id": "h-tilde-user", "cls": "homepath", "carrier": "~name", "path": "n.md", "content": "log in ~buil" + "der/awb.log\n"},
    {"id": "h-no-leading-slash", "cls": "homepath", "carrier": "home/name as tar lists it", "path": "n.md",
     "content": "home/builder/.ssh/id_rsa\nhome/builder/awb.log\n"},
    {"id": "h-percent", "cls": "homepath", "carrier": "percent-encoded in a url query", "path": "n.md",
     "content": "https://viewer.example/?path=%2Fho" + "me%2Fbuilder%2Fawb.log\n"},
    {"id": "h-file-url", "cls": "homepath", "carrier": "file:// url (control)", "path": "n.md",
     "content": "open file://%sbuilder/awb.log\n" % HOME},
    {"id": "h-dashed-inside-word", "cls": "homepath", "carrier": "-home-name inside a longer dashed word", "path": "n.md",
     "content": "archive backup-home-builder-2026.tgz\n"},
    {"id": "h-split-line", "cls": "homepath", "carrier": "path split by a line break", "path": "n.md",
     "content": "log under /home/\nbuilder/awb.log\n"},
    {"id": "h-b64", "cls": "homepath", "carrier": "inside a base64 block", "path": "n.md",
     "content": base64.b64encode(b"tail -f /ho" + b"me/builder/awb.log\n").decode() + "\n"},
    {"id": "h-json-escaped-slash", "cls": "homepath", "carrier": "JSON with escaped slashes (php json_encode)", "path": "log.json",
     "content": '{"log": "\\/home\\/builder\\/awb.log"}\n'},
    {"id": "h-ssh-url", "cls": "homepath", "carrier": "ssh:// url path", "path": "n.md",
     "content": "git clone ssh://git.example/home/builder/repo.git\n"},
    {"id": "h-double-slash", "cls": "homepath", "carrier": "/home//name", "path": "n.md", "content": "cd /ho" + "me//builder\n"},
    {"id": "h-upper", "cls": "homepath", "carrier": "/HOME/NAME upper case", "path": "n.md", "content": "cd /HOME/BUILDER\n"},
    # ----------------------------------------------------------------- the blocklist (tests/blocklist.txt, invented entries)
    {"id": "o-underscore", "cls": "blocklist", "carrier": "the blocked shared folder with an underscore (import)", "path": "x.py",
     "content": "from %s_shared import client\n" % OTC},
    {"id": "o-space", "cls": "blocklist", "carrier": "the blocked shared folder with a space", "path": "n.md",
     "content": "copied from the %s shared layer\n" % OTC},
    {"id": "o-mixed-case", "cls": "blocklist", "carrier": "the blocked shared folder in mixed case (control)", "path": "n.md",
     "content": "see %s-Shared/index.md\n" % OTC.capitalize()},
    {"id": "o-customer-lower", "cls": "blocklist", "carrier": "client1 in lower case", "path": "n.md",
     "content": "host client1.example\n"},
    {"id": "o-customer-letter-o", "cls": "blocklist", "carrier": "CLIENTO1 letter O", "path": "n.md", "content": "for CLIENTO1 today\n"},
    {"id": "o-ptck-bare", "cls": "blocklist", "carrier": "TICKT without digits", "path": "n.md", "content": "ticket TICKT and TICKT-1\n"},
    {"id": "o-detect-lower", "cls": "blocklist", "carrier": "markr_only lower case", "path": "n.md", "content": "mode markr_only\n"},
    {"id": "o-tilde-inside-word", "cls": "blocklist", "carrier": "a blocked folder after a tilde inside a longer word (control)", "path": "n.md",
     "content": "see x~/%s-foo/x\n" % OTC},
    {"id": "o-dot-slash", "cls": "blocklist", "carrier": "the blocked shared folder after ./ (control)", "path": "n.md",
     "content": "cp ./%s-shared/x .\n" % OTC},
    {"id": "o-url", "cls": "blocklist", "carrier": "scaffold name in a url (control)", "path": "n.md",
     "content": "https://git.example/%s-shared.git\n" % OTC},
    {"id": "o-json-escape-dash", "cls": "blocklist", "carrier": "json \\u002d for the dash", "path": "x.json",
     "content": '{"src": "%s\\u002dshared/index.md"}\n' % OTC},
    {"id": "o-html-entity-dash", "cls": "blocklist", "carrier": "html entity for the dash", "path": "x.html",
     "content": "<p>%s&#45;shared/index.md</p>\n" % OTC},
    {"id": "o-zero-width", "cls": "blocklist", "carrier": "zero width space before the dash", "path": "n.md",
     "content": "see %s\u200b-shared/index.md\n" % OTC},
    {"id": "o-en-dash", "cls": "blocklist", "carrier": "en dash", "path": "n.md", "content": "see %s\u2013shared/index.md\n" % OTC},
    {"id": "o-docs-folder", "cls": "blocklist", "carrier": "a blocked docs folder without ~/", "path": "n.md",
     "content": "see %s-docs/index.md\n" % OTC},
    {"id": "o-scaffold-script", "cls": "blocklist", "carrier": "the scaffold script name", "path": "n.md",
     "content": "run new-%s-project.sh foo\n" % OTC},
    {"id": "o-home-var", "cls": "blocklist", "carrier": "$HOME before a blocked folder", "path": "run.sh",
     "content": "cat $HOME/%s-foo/config.yaml\n" % OTC},
    {"id": "o-tilde-user", "cls": "blocklist", "carrier": "~user before a blocked folder", "path": "run.sh",
     "content": "cat ~buil" + "der/%s-foo/config.yaml\n" % OTC},
    {"id": "o-tilde-underscore", "cls": "blocklist", "carrier": "~/ before a blocked folder with an underscore", "path": "run.sh",
     "content": "cat ~/%s_foo/config.yaml\n" % OTC},
    {"id": "o-memory-files", "cls": "blocklist", "carrier": "blocked file names", "path": "n.md",
     "content": "see memory/%s-services/00-index.md and %s-api-endpoints.md\n" % (OTC, OTC)},
    # ----------------------------------------------------------------- identifiers
    {"id": "i-0x-hex", "cls": "identifier", "carrier": "0x-prefixed 32 hex", "path": "n.md", "content": "id 0x%s\n" % HEX32},
    {"id": "i-hex-spaced", "cls": "identifier", "carrier": "32 hex in spaced groups", "path": "n.md",
     "content": "id %s\n" % " ".join(HEX32[i:i + 4] for i in range(0, 32, 4))},
    {"id": "i-hex-dash-split", "cls": "identifier", "carrier": "32 hex with one dash", "path": "n.md",
     "content": "id %s-%s\n" % (HEX32[:8], HEX32[8:])},
    {"id": "i-uuid-braces", "cls": "identifier", "carrier": "uuid in braces upper (control)", "path": "n.md",
     "content": "id {%s}\n" % UUID.upper()},
    {"id": "i-uuid-after-dash", "cls": "identifier", "carrier": "uuid after a dash (ecs-<uuid>)", "path": "n.md",
     "content": "server ecs-%s\n" % UUID},
    {"id": "i-uuid-before-dash", "cls": "identifier", "carrier": "uuid before -suffix", "path": "n.md",
     "content": "volume %s-backup\n" % UUID},
    {"id": "i-project-id", "cls": "identifier", "carrier": "project id <5 digits>", "path": "n.md",
     "content": "project id %s\n" % TENANT},
    {"id": "i-account", "cls": "identifier", "carrier": "account <5 digits>", "path": "n.md",
     "content": "account %s\n" % TENANT},
    {"id": "i-tenant-json", "cls": "identifier", "carrier": "tenant_id in JSON (control)", "path": "x.json",
     "content": '{"tenant_id": "%s"}\n' % TENANT},
    {"id": "i-tenant-json-wide", "cls": "identifier", "carrier": "tenant_id in JSON with wide spacing", "path": "x.json",
     "content": '{"tenant_id"   :   "%s"}\n' % TENANT},
    {"id": "i-tenant-url", "cls": "identifier", "carrier": "tenant/<n> in a url", "path": "n.md",
     "content": "https://console.example/tenant/%s/overview\n" % TENANT},
    {"id": "i-tenants-url", "cls": "identifier", "carrier": "tenants/<n> plural in a url", "path": "n.md",
     "content": "https://console.example/tenants/%s\n" % TENANT},
    {"id": "i-tenant-newline", "cls": "identifier", "carrier": "tenant then the number on the next line", "path": "n.md",
     "content": "tenant\n%s\n" % TENANT},
    {"id": "i-mandant", "cls": "identifier", "carrier": "Mandant <n> (the German word)", "path": "n.md", "content": "Mandant %s\n" % TENANT},
    {"id": "i-domain-name-shape", "cls": "identifier", "carrier": "platform domain name with the number inside", "path": "n.md",
     "content": "domain name OTC-EU-DE-000000000010000%s\n" % TENANT},
    {"id": "i-twilio-ac", "cls": "identifier", "carrier": "AC + 32 hex", "path": "n.md", "content": "sid AC%s\n" % HEX32},
    {"id": "i-hex-control", "cls": "identifier", "carrier": "32 hex (control)", "path": "n.md", "content": "id: %s\n" % HEX32},
    # ----------------------------------------------------------------- names
    {"id": "n-base64", "cls": "name", "carrier": "base64 of the full form", "path": "n.md",
     "content": "tag %s\n" % base64.b64encode(CUST.encode()).decode()},
    {"id": "n-data-uri", "cls": "name", "carrier": "data: uri base64", "path": "x.html",
     "content": '<a href="data:text/plain;base64,%s">x</a>\n' % base64.b64encode(("kunde " + CUST).encode()).decode()},
    {"id": "n-ipynb-ansi", "cls": "name", "carrier": ".ipynb output with an ANSI escape inside the word", "path": "a.ipynb",
     "content": IPYNB},
    {"id": "n-utf16", "cls": "opaque", "carrier": "UTF-16 file (opaque)", "path": "n.txt",
     "content": ("kunde %s\n" % CUST).encode("utf-16")},
    {"id": "n-latin1", "cls": "name", "carrier": "latin-1 file with umlauts (control)", "path": "n.txt",
     "content": ("lieferant %s\n" % ORG).encode("latin-1")},
    {"id": "n-cp437", "cls": "name", "carrier": "cp437 (DOS) file with umlauts", "path": "n.txt",
     "content": ("lieferant %s\n" % ORG).encode("cp437")},
    {"id": "n-python-x-escape", "cls": "name", "carrier": "python \\x escapes", "path": "x.py",
     "content": 'ort = "%s"\n' % "".join("\\x%02x" % ord(c) for c in PLACE)},
    {"id": "n-entity-no-semicolon", "cls": "name", "carrier": "numeric entity without semicolon", "path": "x.html",
     "content": "<p>kunde &#%d%s</p>\n" % (ord(PLACE[0]), PLACE[1:])},
    {"id": "n-qp-soft-break", "cls": "name", "carrier": "quoted-printable soft line break inside the word", "path": "mail.txt",
     "content": "Content-Transfer-Encoding: quoted-printable\n\nkunde %s=\n%s heute\n" % (PLACE[:8], PLACE[8:])},
    {"id": "n-bz2", "cls": "opaque", "carrier": "bzip2 file (no NUL in the first 8 KB?)", "path": "n.txt.bz2", "content": BZ2},
    {"id": "n-pdf-flate", "cls": "opaque", "carrier": "PDF with a FlateDecode stream", "path": "n.pdf", "content": _pdf(PDF_TEXT, True)},
    {"id": "n-pdf-hexstring", "cls": "name", "carrier": "PDF with the text as a hex string", "path": "n.pdf", "content": _pdf(PDF_HEX, False)},
    {"id": "n-pdf-plain", "cls": "name", "carrier": "PDF with a plain text stream (control)", "path": "n.pdf", "content": _pdf(PDF_TEXT, False)},
    {"id": "n-large-file", "cls": "name", "carrier": "12 MB file, the name on the last line", "path": "big.log", "content": BIG},
    {"id": "n-nul-utf16-tail", "cls": "name", "carrier": "ASCII for 9 KB then a UTF-16 tail", "path": "n.log",
     "content": _text_after_8k(("kunde %s\n" % CUST).encode("utf-16-le"))},
    {"id": "n-nul-inside-word", "cls": "name", "carrier": "NUL inside the word after 8 KB", "path": "n.log",
     "content": _text_after_8k(("kunde %s\x00%s\n" % (PLACE[:8], PLACE[8:])).encode())},
    {"id": "n-ansi-log-raw", "cls": "name", "carrier": "terminal log with a real ESC sequence inside the word", "path": "n.log",
     "content": "kunde \x1b[1m%s\x1b[0m%s\n" % (PLACE[:8], PLACE[8:])},
    {"id": "n-gitmodules", "cls": "name", "carrier": ".gitmodules url (control)", "path": ".gitmodules",
     "content": '[submodule "infra"]\n\tpath = infra\n\turl = https://git.example/%s/infra.git\n' % fx.CUSTOMER_DOMAIN.split(".")[0]},
    {"id": "n-gitattributes", "cls": "name", "carrier": ".gitattributes line (control)", "path": ".gitattributes",
     "content": "%s-*.docx filter=lfs diff=lfs merge=lfs -text\n" % SHORT.lower()},
    {"id": "n-hex-utf8", "cls": "name", "carrier": "hex of the utf-8 bytes", "path": "n.md",
     "content": "h %s\n" % ("kunde " + PLACE).encode().hex()},
    {"id": "n-path-glued", "cls": "name", "carrier": "form glued into the file name, content clean", "path": "%slogistik-2026.md" % SHORT.lower(),
     "content": "clean content\n"},
    {"id": "n-rot13", "cls": "name", "carrier": "rot13 (design)", "path": "n.md",
     "content": "kunde %s\n" % PLACE.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
                                                             "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm"))},
]

def _nul_free(build, tries=4000):
    """The first built carrier without a NUL byte in its first 8 KB, None when none of `tries` has one."""
    for i in range(tries):
        data = build(i)
        if b"\x00" not in data[:8192]:
            return data, i
    return None, tries


import random as _random

_rng = _random.Random(11)


def _pad(i: int) -> str:
    return "".join(_rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(20 + i % 60))


PDF_FLATE_NONUL, _pdf_tries = _nul_free(
    lambda i: _pdf(("BT /F1 12 Tf 72 700 Td (kunde %s %s) Tj ET" % (CUST, _pad(i))).encode(), True))
BZ2_NONUL, _bz2_tries = _nul_free(lambda i: bz2.compress(("kunde %s heute %s\n" % (CUST, _pad(i))).encode()))
GREP_COLOR = "kunde \x1b[01;31m\x1b[K%s\x1b[m\x1b[K%s heute\n" % (PLACE[:8], PLACE[8:])

# round 2: the boundary of every escape of round 1, and the shapes next to every catch
ROUND2 = [
    {"id": "s2-single-dash-flag", "cls": "secret", "carrier": "-password=value (one dash)", "path": "run.sh",
     "content": "tool -password=%s\n" % SECRET},
    {"id": "s2-space-before-key-control", "cls": "secret", "carrier": "password=value after a space (control)", "path": "run.sh",
     "content": "tool password=%s\n" % SECRET},
    {"id": "s2-flag-quoted", "cls": "secret", "carrier": "--password=\"value\"", "path": "run.sh",
     "content": 'tool --password="%s"\n' % SECRET},
    {"id": "s2-passwort-compound", "cls": "secret", "carrier": "db_passwort= compound German key", "path": ".env",
     "content": "db_passwort=%s\n" % SECRET},
    {"id": "s2-pass-colon", "cls": "secret", "carrier": "pass: value", "path": "app.yaml", "content": "pass: %s\n" % SECRET},
    {"id": "s2-pw-upper", "cls": "secret", "carrier": "PW=value", "path": ".env", "content": "PW=%s\n" % SECRET},
    {"id": "s2-Password-control", "cls": "secret", "carrier": "Password=value (control)", "path": "app.ini",
     "content": "Password=%s\n" % SECRET},
    {"id": "s2-xml-Password-upper", "cls": "secret", "carrier": "<Password> element", "path": "app.config",
     "content": "<Password>%s</Password>\n" % SECRET},
    {"id": "s2-xml-attr-control", "cls": "secret", "carrier": "password attribute (control)", "path": "users.xml",
     "content": '<user name="builder" password="%s"/>\n' % SECRET},
    {"id": "s2-quoted-bang-end-control", "cls": "secret", "carrier": "quoted value ending with ! (control)", "path": "app.ini",
     "content": 'password = "%s!"\n' % SECRET},
    {"id": "s2-quoted-bracket-start", "cls": "secret", "carrier": "quoted value starting with [", "path": "app.ini",
     "content": 'password = "[%s"\n' % SECRET},
    {"id": "s2-paren-end", "cls": "secret", "carrier": "quoted value ending with )", "path": "app.ini",
     "content": 'password = "%s)"\n' % SECRET},
    {"id": "s2-snake-no-underscore-control", "cls": "secret", "carrier": "lower-case alnum value, no underscore (control)", "path": "app.ini",
     "content": "secret = %s%s\n" % (SNAKE_A, SNAKE_B)},
    {"id": "s2-upper-underscore", "cls": "secret", "carrier": "upper-case alnum value with one underscore", "path": "app.ini",
     "content": "secret = %s_%s\n" % (SNAKE_A.upper(), SNAKE_B.upper())},
    {"id": "s2-jwt-with-dash-control", "cls": "secret", "carrier": "token = JWT with a hyphen (control)", "path": "app.ini",
     "content": "token = %s-x\n" % JWT},
    {"id": "s2-sharedaccesskey-control", "cls": "secret", "carrier": "Azure SharedAccessKey (control)", "path": ".env",
     "content": "SB=Endpoint=sb://x.servicebus.windows.net/;SharedAccessKeyName=root;SharedAccessKey=%s\n" % KEY32},
    {"id": "s2-masterkey", "cls": "secret", "carrier": "MasterKey=", "path": ".env", "content": "MasterKey=%s\n" % KEY64},
    {"id": "s2-signing-key", "cls": "secret", "carrier": "SIGNING_KEY=", "path": ".env", "content": "SIGNING_KEY=%s\n" % SECRET},
    {"id": "s2-private-key-env", "cls": "secret", "carrier": "PRIVATE_KEY= (base64, no armour)", "path": ".env",
     "content": "PRIVATE_KEY=%s\n" % KEY32},
    {"id": "s2-yaml-folded", "cls": "secret", "carrier": "YAML folded scalar >-", "path": "values.yaml",
     "content": "password: >-\n  %s\n" % SECRET},
    {"id": "s2-jdbc-query-control", "cls": "secret", "carrier": "jdbc url with &password= (control)", "path": "app.properties",
     "content": "url=jdbc:postgresql://db.example/app?user=builder&password=%s\n" % SECRET},
    {"id": "s2-env-get-default", "cls": "secret", "carrier": "os.environ.get default", "path": "conf.py",
     "content": 'pw = os.environ.get("DB_PASSWORD", "%s")\n' % SECRET},
    {"id": "s2-triple-quotes", "cls": "secret", "carrier": "python triple-quoted value", "path": "conf.py",
     "content": 'password = """%s"""\n' % SECRET},
    {"id": "s2-nbsp-before-eq", "cls": "secret", "carrier": "no-break space before =", "path": "app.ini",
     "content": "password\u00a0= %s\n" % SECRET},
    {"id": "s2-ak-sk-blank-line", "cls": "secret", "carrier": "key id, one blank line, secret", "path": "creds.txt",
     "content": "%s\n\n%s\n" % (AK, SK)},
    {"id": "s2-csv-credentials-control", "cls": "secret", "carrier": "credentials csv export (control)", "path": "credentials.csv",
     "content": "User Name,Access Key Id,Secret Access Key\nbuilder,%s,%s\n" % (AK, SK)},
    {"id": "s2-bearer-curl", "cls": "secret", "carrier": "curl -H Authorization: Bearer", "path": "run.sh",
     "content": 'curl -H "Authorization: Bearer %s" https://api.example/v1\n' % JWT},
    {"id": "s2-basic-curl", "cls": "secret", "carrier": "curl -u user:pass", "path": "run.sh",
     "content": "curl -u builder:%s https://api.example/v1\n" % SECRET},
    {"id": "s2-redis-url", "cls": "secret", "carrier": "redis url with an empty user", "path": ".env",
     "content": "REDIS_URL=redis://:%s@cache.example:6379/0\n" % SECRET},
    {"id": "s2-set-cookie-control", "cls": "secret", "carrier": "Set-Cookie auth_token (control)", "path": "resp.http",
     "content": "Set-Cookie: auth_token=%s; Path=/\n" % SECRET},
    {"id": "s2-x-api-key-control", "cls": "secret", "carrier": "x-api-key header (control)", "path": "req.http",
     "content": "x-api-key: %s\n" % SECRET},
    {"id": "s2-authorization-token", "cls": "secret", "carrier": "Authorization: Token value", "path": "req.http",
     "content": "Authorization: Token %s\n" % SECRET},
    {"id": "s2-apikey-query-control", "cls": "secret", "carrier": "?apikey= in a url (control)", "path": "n.md",
     "content": "https://api.example/v1?apikey=%s\n" % SECRET},
    {"id": "t2-ghu", "cls": "token", "carrier": "GitHub ghu_ and ghr_", "path": "n.md",
     "content": "ghu_%s\nghr_%s\n" % (B62[:36], B62[1:37])},
    {"id": "t2-ghp-in-url-control", "cls": "token", "carrier": "ghp_ inside a url (control)", "path": "n.md",
     "content": "https://ghp_%s@github.example/x.git\n" % B62[:36]},
    {"id": "t2-google-oauth", "cls": "token", "carrier": "Google ya29. access token", "path": "n.md",
     "content": "ya29.%s\n" % (B62 + B62)},
    {"id": "p2-ssh2-five-dashes-control", "cls": "private-key", "carrier": "SSH2 armour with five dashes, no spaces (control)", "path": "id_ssh2",
     "content": "-----BEGIN SSH2 ENCRYPTED %s KEY-----\n%s\n" % (PRIV, PEM_BODY)},
    {"id": "p2-armour-nbsp", "cls": "private-key", "carrier": "no-break space in the armour (web copy)", "path": "id_rsa",
     "content": "-----BEGIN RSA %s\u00a0KEY-----\n%s\n" % (PRIV, PEM_BODY)},
    {"id": "h2-export-home", "cls": "homepath", "carrier": "/export/home/name", "path": "n.md", "content": "cd /export%sbuilder\n" % HOME},
    {"id": "h2-data-home", "cls": "homepath", "carrier": "/data/home/name", "path": "n.md", "content": "cd /data%sbuilder\n" % HOME},
    {"id": "h2-cygwin", "cls": "homepath", "carrier": "C:\\cygwin64\\home\\name", "path": "n.md",
     "content": "cd C:\\cygwin64\\ho" + "me\\builder\n"},
    {"id": "h2-wsl-unc", "cls": "homepath", "carrier": "\\\\wsl$\\Ubuntu\\home\\name", "path": "n.md",
     "content": "open \\\\wsl$\\Ubuntu\\ho" + "me\\builder\\x\n"},
    {"id": "h2-mnt-c-users", "cls": "homepath", "carrier": "/mnt/c/Users/name (WSL)", "path": "n.md",
     "content": "cp /mnt/c%sbuilder/Documents/x .\n" % USERS},
    {"id": "h2-home-umlaut-user", "cls": "homepath", "carrier": "/home/<umlaut user>", "path": "n.md",
     "content": "cd %s\u00f6laf/x\n" % HOME},
    {"id": "o2-home-brace", "cls": "blocklist", "carrier": "${HOME} before a blocked folder", "path": "run.sh",
     "content": "cat ${HOME}/%s-foo/config.yaml\n" % OTC},
    {"id": "o2-abs-home-blocked-control", "cls": "homepath", "carrier": "/home/<user>/ then a blocked folder (refused as homepath)", "path": "run.sh",
     "content": "cat %sbuilder/%s-foo/config.yaml\n" % (HOME, OTC)},
    {"id": "i2-uuid-after-underscore-control", "cls": "identifier", "carrier": "vm_<uuid> (control)", "path": "n.md",
     "content": "server vm_%s\n" % UUID},
    {"id": "i2-uuid-both-sides-dash", "cls": "identifier", "carrier": "ecs-<uuid>-disk", "path": "n.md",
     "content": "volume ecs-%s-disk\n" % UUID},
    {"id": "i2-project-id-colon", "cls": "identifier", "carrier": "project_id: <n>", "path": "x.yaml",
     "content": "project_id: %s\n" % TENANT},
    {"id": "n2-grep-color", "cls": "name", "carrier": "grep --color output pasted (SGR inside the word)", "path": "n.log",
     "content": GREP_COLOR},
    {"id": "n2-eml-b64-body", "cls": "name", "carrier": ".eml with a base64 body", "path": "mail.eml",
     "content": "From: a@example.org\nSubject: offer\nContent-Transfer-Encoding: base64\n\n%s\n" % base64.b64encode(
         ("kunde %s heute\n" % CUST).encode()).decode()},
    {"id": "n2-python-octal-escape", "cls": "name", "carrier": "python octal escapes", "path": "x.py",
     "content": 'ort = "%s"\n' % "".join("\\%03o" % ord(c) for c in PLACE)},
    {"id": "n2-utf7", "cls": "name", "carrier": "UTF-7 text with umlauts", "path": "n.txt",
     "content": ("lieferant %s\n" % ORG).encode("utf-7")},
    {"id": "n2-html-comment-inside-control", "cls": "name", "carrier": "html comment inside the word (control)", "path": "x.html",
     "content": "<p>kunde %s<!-- -->%s</p>\n" % (PLACE[:8], PLACE[8:])},
]
if PDF_FLATE_NONUL is not None:
    ROUND2.append({"id": "n2-pdf-flate-no-nul", "cls": "opaque", "carrier": "PDF with a FlateDecode stream and no NUL byte (%d tries)" % _pdf_tries,
                   "path": "n.pdf", "content": PDF_FLATE_NONUL})
if BZ2_NONUL is not None:
    ROUND2.append({"id": "n2-bz2-no-nul", "cls": "opaque", "carrier": "bzip2 file without a NUL byte (%d tries)" % _bz2_tries,
                   "path": "n.txt.bz2", "content": BZ2_NONUL})
CASES += ROUND2

# round 3: the boundary of the swallowing (an inner key=value inside an outer harmless value), the first-character
# rule inside quotes, placeholder words inside a real value, and controls next to each
ROUND3 = [
    {"id": "s3-yaml-env-inline", "cls": "secret", "carrier": "environment: PASSWORD=value (inner assignment swallowed)", "path": "compose.yaml",
     "content": "services:\n  db:\n    environment: PASSWORD=%s\n" % SECRET},
    {"id": "s3-yaml-env-list-control", "cls": "secret", "carrier": "environment list item - PASSWORD=value (control)", "path": "compose.yaml",
     "content": "services:\n  db:\n    environment:\n      - PASSWORD=%s\n" % SECRET},
    {"id": "s3-cookie-second-control", "cls": "secret", "carrier": "Cookie: a=b; token=value (second pair, control)", "path": "req.http",
     "content": "Cookie: session=abc; token=%s\n" % SECRET},
    {"id": "s3-cookie-first", "cls": "secret", "carrier": "Cookie: token=value (first pair swallowed)", "path": "req.http",
     "content": "Cookie: token=%s; theme=dark\n" % SECRET},
    {"id": "s3-libpq-dsn-control", "cls": "secret", "carrier": "libpq keyword string DSN=user=x password=value (control)", "path": ".env",
     "content": "DSN=user=builder password=%s host=db.example\n" % SECRET},
    {"id": "s3-exec-start", "cls": "secret", "carrier": "systemd ExecStart=/usr/bin/tool --password=value", "path": "app.service",
     "content": "[Service]\nExecStart=/usr/bin/tool --password=%s\n" % SECRET},
    {"id": "s3-database-url-query", "cls": "secret", "carrier": "DATABASE_URL=...?user=x&password=value", "path": ".env",
     "content": "DATABASE_URL=postgres://db.example/app?user=builder&password=%s\n" % SECRET},
    {"id": "s3-json-url-query", "cls": "secret", "carrier": "\"url\": \"https://...?apikey=value\" (quoted outer value)", "path": "conf.json",
     "content": '{"url": "https://api.example/v1?apikey=%s"}\n' % SECRET},
    {"id": "s3-docker-run-e-control", "cls": "secret", "carrier": "docker run -e PASSWORD=value (control)", "path": "run.sh",
     "content": "docker run -e PASSWORD=%s img\n" % SECRET},
    {"id": "s3-dockerfile-env-space", "cls": "secret", "carrier": "Dockerfile ENV PASSWORD value (old syntax)", "path": "Dockerfile",
     "content": "ENV PASSWORD %s\n" % SECRET},
    {"id": "s3-quoted-dollar-start", "cls": "secret", "carrier": "quoted value starting with $", "path": ".env",
     "content": 'PASSWORD="$%s"\n' % SECRET},
    {"id": "s3-quoted-brace-start", "cls": "secret", "carrier": "quoted value starting with {", "path": ".env",
     "content": 'PASSWORD="{%s}"\n' % SECRET},
    {"id": "s3-placeholder-word-inside", "cls": "secret", "carrier": "real value with 'sample' inside", "path": "app.ini",
     "content": 'password = "Vb7Qk2Zn9SampleLp4Rw8Xm3"\n'},
    {"id": "s3-your-inside", "cls": "secret", "carrier": "real value with 'your' inside", "path": "app.ini",
     "content": 'password = "Vb7QYourk2Zn9Lp4Rw8Xm3Ct"\n'},
    {"id": "s3-powershell-securestring", "cls": "secret", "carrier": "PowerShell ConvertTo-SecureString \"value\"", "path": "run.ps1",
     "content": '$pass' + 'word = ConvertTo-SecureString "%s" -AsPlainText -Force\n' % SECRET},
    {"id": "s3-yaml-flow-map-control", "cls": "secret", "carrier": "YAML flow map {password: value} (control)", "path": "x.yaml",
     "content": "db: {password: %s}\n" % SECRET},
    {"id": "h3-json-unicode-slash", "cls": "homepath", "carrier": "json \\u002f for the slashes", "path": "log.json",
     "content": '{"log": "\\u002fho' + 'me\\u002fbuilder\\u002fawb.log"}\n'},
    {"id": "i3-uuid-urn-control", "cls": "identifier", "carrier": "urn:uuid: (control)", "path": "n.md", "content": "urn:uuid:%s\n" % UUID},
    {"id": "n3-grep-color-whole-word-control", "cls": "name", "carrier": "SGR around the whole word (control)", "path": "n.log",
     "content": "kunde \x1b[01;31m\x1b[K%s\x1b[m\x1b[K heute\n" % PLACE},
    {"id": "n3-overstrike", "cls": "name", "carrier": "man page bold by overstrike (X backspace X)", "path": "n.txt",
     "content": "kunde %s heute\n" % "".join(c + "\x08" + c for c in PLACE)},
]
CASES += ROUND3

SMALL_CAPS = str.maketrans("abcdefghijklmnoprstuvwz", "\u1d00\u0299\u1d04\u1d05\u1d07\ua730\u0262\u029c\u026a\u1d0a\u1d0b\u029f\u1d0d\u0274\u1d0f\u1d18\u0280\ua731\u1d1b\u1d1c\u1d20\u1d21\u1d22")
MATH_BOLD = {c: chr(0x1D400 + i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
MATH_BOLD.update({c: chr(0x1D41A + i) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")})

# round 4: shapes next to the round-3 findings and the remaining disguise angles
ROUND4 = [
    {"id": "s4-json-nested-inline-control", "cls": "secret", "carrier": "{\"db\": {\"password\": \"v\"}} one line (control)", "path": "c.json",
     "content": '{"db": {"password": "%s"}}\n' % SECRET},
    {"id": "s4-toml-inline-table-control", "cls": "secret", "carrier": "TOML inline table (control)", "path": "c.toml",
     "content": 'db = { password = "%s" }\n' % SECRET},
    {"id": "s4-yaml-flow-map-space-control", "cls": "secret", "carrier": "YAML flow map with a space after { (control)", "path": "x.yaml",
     "content": "db: { password: %s }\n" % SECRET},
    {"id": "s4-sql-identified-by", "cls": "secret", "carrier": "SQL CREATE USER ... IDENTIFIED BY 'value'", "path": "init.sql",
     "content": "CREATE USER builder IDENTIFIED BY '%s';\n" % SECRET},
    {"id": "s4-base64-secret-control", "cls": "secret", "carrier": "base64 value with / + = (control)", "path": "app.ini",
     "content": "secret = %s\n" % KEY32},
    {"id": "n4-pdf-octal-escape", "cls": "name", "carrier": "PDF string with octal escapes for some letters", "path": "n.pdf",
     "content": _pdf(("BT /F1 12 Tf 72 700 Td (kunde %s) Tj ET" % "".join("\\%03o" % ord(c) if i % 2 else c for i, c in enumerate(PLACE))).encode(), False)},
    {"id": "n4-rtf-unicode-escape", "cls": "name", "carrier": "RTF \\uN? escape for the umlaut", "path": "brief.rtf",
     "content": "{\\rtf1\\ansi lieferant %s}\n" % ORG.replace("\u00f6", "\\u246?").replace("\u00e4", "\\u228?")},
    {"id": "n4-combining-marks-control", "cls": "name", "carrier": "combining low line under every letter (control)", "path": "n.md",
     "content": "kunde %s\n" % "".join(c + "\u0332" for c in PLACE)},
    {"id": "n4-math-bold-control", "cls": "name", "carrier": "mathematical bold letters (control)", "path": "n.md",
     "content": "kunde %s\n" % "".join(MATH_BOLD.get(c, c) for c in PLACE)},
    {"id": "n4-small-caps", "cls": "name", "carrier": "Unicode small capital letters", "path": "n.md",
     "content": "kunde %s\n" % PLACE.lower().translate(SMALL_CAPS)},
]
CASES += ROUND4

# round 5: confirming round, boundaries next to the catches of rounds 1 to 4
ROUND5 = [
    {"id": "s5-ak-sk-nested-yaml", "cls": "secret", "carrier": "nested YAML: id and secret two lines apart under 'key:'", "path": "creds.yaml",
     "content": "credentials:\n  access:\n    key: %s\n  secret:\n    key: %s\n" % (AK, SK)},
    {"id": "s5-ak-sk-labels-no-sep-control", "cls": "secret", "carrier": "Access Key Id <id> / Secret Access Key <sk> without separators (control)", "path": "creds.txt",
     "content": "Access Key Id %s\nSecret Access Key %s\n" % (AK, SK)},
    {"id": "s5-camel-pair-control", "cls": "secret", "carrier": "accessKeyId / secretAccessKey JSON (control)", "path": "creds.json",
     "content": '{"accessKeyId": "%s", "secretAccessKey": "%s"}\n' % (AK, SK)},
    {"id": "i5-tenant-two-spaces-control", "cls": "identifier", "carrier": "\"tenant_id\":  \"<n>\" two spaces (control)", "path": "x.json",
     "content": '{"tenant_id":  "%s"}\n' % TENANT},
    {"id": "n5-path-percent-control", "cls": "name", "carrier": "percent-encoded form in the file name (control)", "path": "%s.md" % CUST.replace(" ", "%20"),
     "content": "clean content\n"},
    {"id": "n5-utf16-be-tail-control", "cls": "name", "carrier": "ASCII for 9 KB then a UTF-16 BE tail (control)", "path": "n.log",
     "content": _text_after_8k(("kunde %s\n" % CUST).encode("utf-16-be"))},
    {"id": "h5-users-mac-file-url", "cls": "homepath", "carrier": "file:///Users/name", "path": "n.md",
     "content": "open file://%sbuilder/Documents/offer.docx\n" % USERS},
    {"id": "s5-tab-separated", "cls": "secret", "carrier": "password<TAB>value", "path": "creds.tsv",
     "content": "password\t%s\n" % SECRET},
    {"id": "n5-rtf-hex-control", "cls": "name", "carrier": "RTF \\'hh escape for the umlaut (control)", "path": "brief.rtf",
     "content": "{\\rtf1\\ansi lieferant %s}\n" % ORG.replace("\u00f6", "\\'f6").replace("\u00e4", "\\'e4")},
    {"id": "n5-json-u-escape-control", "cls": "name", "carrier": "json \\u escape for the first letter (control)", "path": "x.json",
     "content": '{"ort": "\\u%04x%s"}\n' % (ord(PLACE[0]), PLACE[1:])},
]
CASES += ROUND5

if __name__ == "__main__":
    # a look at the byte carriers that decide between opaque and text, values never printed
    for c in CASES:
        if isinstance(c["content"], bytes):
            print("%-22s %8d bytes  NUL in first 8 KB: %s" % (c["id"], len(c["content"]), b"\x00" in c["content"][:8192]))
    print(len(CASES), "cases")
