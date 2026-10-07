# Interfaces of the first release

Every module below is built against this file. A builder may add private helpers, never change a public
signature without a note in `build/PROGRESS.md`. Runtime code: standard library plus the poppler command line
tools. Test code may use python-docx, openpyxl, reportlab (all installed in `.venv`).

Ground rules for every module:

- What must never enter a commit beyond names, secrets and home paths is the owner's own list: the local blocklist
  of `awb/gate.py` (`/etc/awb/blocklist.txt`, `~/.config/awb/blocklist.txt`), never part of the repository. The
  test names live in `tests/fixtures.py` and are invented, the test blocklist in `tests/blocklist.txt`.
- Error messages, log lines and reports on the public side never carry a registered form, a matched value or a
  line of the register. They carry codes, classes, counts, file ids, positions.
- A failed read is `unreadable` or `failed`, never `ok` with empty text.
- Every module has tests in `tests/test_<module>.py` that would fail if the module was replaced by a stub.

## Already written (read them): `awb/config.py`, `awb/codes.py`, `awb/register.py`, `awb/normalize.py`, `awb/matcher.py`

Places (`awb/config.py`): `~/tcp-awb` the repository, `~/tcp-shared` what sessions share (project register,
ledger, outbox), `~/tcp-vault` the intake side (register, inbox, originals, private reports, log), projects as
`~/tcp-<code>`. Tests point all three at a temporary folder through `AWB_SHARED`, `AWB_VAULT`, `AWB_PROJECTS`.

## `awb/register.py`

The register is a TSV file with a fixed header and one line per written form:

```
code	kind	form	added	status
CUST-Q7M4	CUST	<full legal name>	2026-09-22	active
CUST-Q7M4	CUST	<short form>	2026-09-22	active
CUST-Q7M4-PERS-1	PERS	<first and last name>	2026-09-22	active
```

```python
@dataclass(frozen=True)
class Entry: code: str; kind: str; form: str; added: str; status: str   # status: active | retired

class RegisterError(Exception): ...   # message carries line number and reason, NEVER the line content

def load(path: Path) -> list[Entry]
    # strict: header exactly as above; every other line has exactly 5 tab-separated fields; no blank lines
    # inside; no comment lines (a line starting with # is an error); code matches codes.PLACEHOLDER_RE;
    # kind == codes.kind_of(code) for top-level codes, or the sub kind for sub codes; form non-empty, stripped,
    # no tab, no newline, no '(' ')' '[' ']' '|' '#' (free text and notes are refused on purpose);
    # added is an ISO date; status in {active, retired}; the same (code, form) twice is an error.
    # Missing file -> empty list. Unreadable file -> RegisterError.
def save(path: Path, entries: list[Entry]) -> None       # atomic write, file mode 0o600, parent 0o700
def add(path: Path, code: str, kind: str, form: str, added: str | None = None) -> Entry
def retire(path: Path, code: str) -> int                 # marks all forms of the code retired, returns count
def codes(entries) -> set[str]
def next_code(entries, kind: str) -> str                 # codes.new_code with the existing set
def forms_for_matching(entries) -> list[tuple[str, str]]
    # (variant, code) for ACTIVE entries only. Variants generated per form: the form itself; umlauts
    # transliterated both ways (ä->ae and ä->a, ß->ss); genitive (+s, +'s, +’s) when the form does not end
    # in s; the form with any whitespace run collapsed. Sorted longest variant first. Retired forms are not
    # matched (they are kept in the register only so that the code is never reused).
```

## `awb/normalize.py`

```python
@dataclass
class Normalized:
    text: str
    to_original: list[int]     # to_original[i] = index in the original text of normalized character i
                               # (for an inserted character: the index of the nearest preceding original char)

def normalize(text: str) -> Normalized
    # NFC; remove U+00AD, U+200B, U+200C, U+200D, U+FEFF; U+00A0 and other Unicode spaces -> ' ';
    # rejoin hyphenation at line end ("Logis-\ntik" -> "Logistik") only when both sides are lowercase
    # letters or the right side is lowercase; decode HTML entities (&auml; &#246; &#xF6;) and
    # URL-encoding (%C3%B6) into the character; CRLF -> LF. Positions are tracked through every change.
def original_span(n: Normalized, start: int, end: int) -> tuple[int, int]
```

## `awb/matcher.py`

```python
@dataclass
class Span: start: int; end: int; cls: str; code: str | None = None; form: str | None = None
    # positions in the NORMALIZED text; cls is 'name' for register hits, or a DATA_KIND lowercase for patterns

class Matcher:
    def __init__(self, forms: list[tuple[str, str]]): ...   # from register.forms_for_matching
    def find(self, text: str) -> list[Span]
        # case-insensitive; boundary = not preceded and not followed by a letter or digit (underscore, dot,
        # slash, hyphen, quote, bracket all count as boundaries, so file names and identifiers match);
        # any whitespace run inside a form matches any whitespace run (including a line break); longest
        # variant first; no overlapping spans; also matches a form glued as CamelCase or lowercase inside
        # an identifier when the form has at least 5 letters (for example a lower-case glued name followed by -backup, or a CamelCase name followed by digits):
        # a lower-case, space-free variant of each such form is searched without the boundary rule.
    def replace(self, text: str, spans: list[Span], token) -> str   # token(span) -> str, applied right to left
def public(spans: list[Span]) -> list[dict]     # [{"start":..,"length":..,"cls":..}] and nothing else
```

## `awb/patterns.py`

```python
PATTERNS: list[tuple[str, re.Pattern, Callable[[str], bool | int] | None, re.Pattern | None]]
    # (kind, pattern, check, label). kinds (from codes.DATA_KINDS): MAIL, URL, IP (v4 with optional /cidr, and v6),
    # MAC, IBAN (any separators, length and mod 97 per country), BIC, VAT (thirty countries), PHONE (international,
    # 00, national), HRB, TAX. The check returns True, False or the end of the value inside the match (an IBAN is
    # cut at the length its country announces). The label (IBAN:, BIC-Code:, mac, Steuernummer) proves the value and
    # is not part of the span. Since 2026-09-27 the shapes of calibration/redteam-2026-09-27.md are in.
KEEP_IP: list of prefixes never replaced: 169.254., 100.125., 100.64., 0.0.0.0, 127., 10., 172.16-31., 192.168., 255., ::1, fe80:,
         198.18., 198.19., 224.
ALLOW_DOMAINS: loaded from rules/allow-domains.txt (well-known vendor documentation hosts only; never a customer)
_TLD_SET: every country code and the common generic names; _FILE_SUFFIX_TLDS (py, sh, md, tf, so, ai, zip ...) need
         a host of two labels in front, and a short capitalised word with two letters behind a dot (Tel.Nr) is no host
def find_structured(text: str) -> list[Span]   # cls = kind.lower(); URL/domain spans skip ALLOW_DOMAINS;
                                               # bare domains (a host name under a known top-level domain) count as URL;
                                               # runs over a view (_view) with markup inside a value taken out, letter
                                               # spaced runs closed up and the border between two table cells read as a
                                               # space, and reports every span at its place in `text`
```

## `awb/extract/__init__.py` and `awb/extract/{office,pdf,mail,archive,text}.py`

```python
@dataclass
class Extraction:
    path: Path
    kind: str          # text, html, csv, json, docx, xlsx, pptx, odt, ods, odp, pdf, eml, mbox, msg, zip, tar, image, unknown
    state: str         # ok | unreadable | unsupported | failed
    text: str          # what goes into the sanitised Markdown output (body, tables as Markdown tables)
    detect_text: str   # everything detection must see: text + headers/footers + comments + metadata +
                       # hidden sheets/columns + formulas + defined names + sheet names + member names
    meta: dict         # title, author, creator, last_modified_by, company, subject, dates, page_count ...
    notes: list[str]   # human notes without values: "page 3 has no extractable text", "2 comments dropped"
    children: list["Extraction"]   # members of an archive

def sniff(path: Path) -> str        # by content: %PDF, PK.. (then inner names: word/, xl/, ppt/, mimetype),
                                    # OLE header D0CF11E0 (-> msg/doc/xls: unsupported), PNG/JPEG/GIF (image),
                                    # gzip/tar, UTF-16 BOM, else text if decodable else unknown
def extract(path: Path, depth: int = 0) -> Extraction   # dispatch; archives recurse up to depth 3
```

- `office.py`: read the zip directly (no python-docx at runtime). docx: paragraphs with runs joined, tables as
  Markdown rows, headers, footers, footnotes, comments (with author), core.xml and app.xml properties,
  tracked changes text (w:del and w:ins both go to detect_text). xlsx: every sheet incl. hidden, every cell
  incl. hidden columns, shared strings, inline strings, formula text AND cached values, comments with author,
  defined names, sheet names, headers/footers. pptx: slides, notes, comments. odt/ods/odp: content.xml,
  styles.xml (headers/footers), meta.xml.
- `pdf.py`: `pdftotext -layout` for text, `pdfinfo` for metadata, per-page text via `pdftotext -f n -l n`;
  a page with fewer than 40 characters of text is noted as "page n has no extractable text, review the
  original", and a file whose pages ALL have none is state `unreadable`. Missing poppler -> `failed`.
- `mail.py`: `.eml` via the standard library `email` package: all headers (From, To, Cc, Bcc, Reply-To,
  Subject with RFC 2047 decoding, Message-ID, Received, X-Originating-IP), body parts decoded (base64,
  quoted-printable, a charset label corrected when it does not fit the bytes), the preamble and the epilogue
  of a multipart, text/html stripped to text, attachments and inline uuencoded blocks extracted to a
  temporary folder and passed through `extract` as children. `.mbox` (also found by content) -> kind `mbox`,
  split at its "From " lines, every message a child of kind `eml`. `.msg` -> state `unsupported` with a note.
- `archive.py`: zip and tar(.gz): member names go to detect_text (names carry names), members extracted to
  a private temporary folder (mode 700, removed afterwards) and passed through `extract` as children.
  A zip bomb guard: stop above 200 members or 200 MB.
- `text.py`: encoding by BOM, then UTF-8, then UTF-16, then Latin-1; html tags stripped; a base64 block of
  40+ characters inside a text file is decoded and its text appended to detect_text with a note.
- Images: state `unreadable`, note "image, no text layer, review the original". Never scanned as text.

## `awb/intake.py` and `awb/report.py`

```python
@dataclass
class IntakeResult:
    customer: str; outputs: list[Path]; public_report: Path; private_report: Path
    blocked: bool; candidates: int; states: dict[str, str]   # file id -> state

def run(files: list[Path], customer: str, p: config.Paths, *, force: bool = False) -> IntakeResult
```

Steps, in this order:

1. `customer` is an existing CUST code, or `new`, which creates a code with `register.next_code`.
2. Every file gets a file id `F-<4 base32>`; the ORIGINAL FILE NAME is treated as text: it is scanned and
   the output file is named by the file id, never by the original name.
3. `extract` (archives and mail attachments recursively). Per extraction: `normalize(detect_text)`, then
   `Matcher.find` over the register forms plus `find_structured`; the same value gets the same token within
   one run (a dict value -> token, kept only in the private report).
4. Candidates: `unknown_candidates(detect_text, spans)` returns strings that look like unregistered names:
   capitalised words followed by a company form (GmbH, AG, KG, SE, mbB, Ltd, Inc, e.V., OHG, GbR, S.A.,
   B.V., Kanzlei), two or more capitalised words in a row that are not in `rules/stop-words.txt` and not
   sentence starts, a word after "Kunde:", "Customer:", "Auftraggeber:", "Firma:", "Mandant:", and the
   local part of an e-mail address that looks like a person. Candidates go to the PRIVATE report only.
5. If there are candidates and not `force`: `blocked=True`, only the private report is written, no output.
   He then registers or dismisses them (`awb register add`) and runs again.
6. Replace: register spans by their code, structured spans by a DATA token, in `text` (output) using the
   same spans re-found in the output text. Write `<shared>/outbox/<CUST>/<file id>.md` with a small header (file id,
   kind, state, notes) and the sanitised text. Markdown only.
7. Final check of every output file: `Matcher.find` + `find_structured` over it must return nothing. If not,
   delete the output, state `failed`, and say so in both reports. This is the guarantee.
8. Originals: moved (not copied) to `<vault>/originals/<CUST>/<file id><original suffix>`, only after step 7 passed. A mapping file id
   -> original name goes to the PRIVATE report only.
9. Reports. Public (`<shared>/outbox/<CUST>/intake-report.md`): date, customer code, per file id: kind, state,
   counts per class replaced, notes; totals; the sentence "N candidates were reviewed" without the candidates.
   Private (`<vault>/reports/<CUST>/<date>-<time>.md`, mode 600): everything, including original names,
   matched values, tokens, positions, candidates, dropped items and the reason.

`unknown_candidates(text: str, known: list[Span]) -> list[str]` lives in `intake.py`.

## `awb/check.py`

```python
def check_text(text: str, register_path: Path) -> list[dict]   # matcher.public(...) plus structured spans, positions in normalized text
def check_file(path: Path, register_path: Path) -> list[dict]
```
CLI `awb check FILE...` prints JSON lines `{"file":..,"start":..,"length":..,"cls":..}` and exits 1 on any hit.
It never prints the matched value or the code.

## `awb/gate.py`

```python
@dataclass
class Finding: file: str; line: int; cls: str    # cls: name | secret | private-key | token | homepath | blocklist | identifier

def scan_files(files: list[Path], register_path: Path | None) -> list[Finding]
    # name: via Matcher when a register exists (positions turned into line numbers)
    # secret: a key-like assignment (keyword ak/sk/secret/password/pass/pw/passphrase/token/api_key/access_key/
    #         private_key/auth/credentials, the German words, followed by : or =, also after a -- or -D flag,
    #         inside a longer value, as an XML element or key/value pair, or with the value on the next line)
    #         whose value is 16+ chars with entropy >= 3.5 bits per char and is not a placeholder or a reference;
    #         a password in a URL userinfo, after curl -u, in .pgpass, .netrc, a credential table, an
    #         Authorization header, a .htpasswd hash, a bare key pair within two lines
    # private-key: the armour line (also ---- BEGIN ... ----), a PuTTY key file, a JWK with "d", an age key,
    #         the base64 of a private key armour
    # token: known prefixes (GitHub gh[oprsu]_ github_pat_, GitLab, Slack xox*/xapp/webhook, Google, SendGrid,
    #         npm, PyPI, Hugging Face, Stripe, Telegram, Discord, Twilio, JWT, AKIA/ASIA/HPUA), a digit inside
    # homepath: the Linux, macOS and Windows home spellings, the super-user home, ~user/, a tar listing line,
    #         -home-<user>-
    # blocklist: what a line of the owner's local blocklist matches (gate.BLOCKLIST_FILES, AWB_BLOCKLIST for tests;
    #         one regular expression per line, flags in scoped form only)
    # identifier: a 32-hex id (also 0x), a UUID (also glued with a dash), a 5 or 6 digit number after 'tenant',
    #         'domain', 'project', 'account' or 'Mandant'
    # every detector runs over the text, its normalised form and what its base64 and hex blocks decode to
def staged_files(repo: Path) -> list[Path]
def scan_message(path: Path, matcher) -> list[Finding]   # a commit message file, label "commit message"
def selftest(register_path: Path | None) -> list[str]   # plants one case per class in a temp folder, expects exactly that class,
                                                       # then a clean file expects nothing, then a commit message; returns failures
```
CLI `awb gate [--staged] [--pushed [--remote NAME]] [--message FILE] [--selftest] [PATH...]`: prints
`cls  file:line` per finding, exit 1 on findings. `hooks/pre-commit`, `hooks/commit-msg` and `hooks/pre-push`
(executable, sh) run the gate and refuse the commit or push on exit 1; `install_hook` writes the same three.
`rules/allow-domains.txt`, `rules/stop-words.txt` are data files read by patterns and intake.

## `awb/projects.py`

```python
PROJECT_KINDS = ("engagement", "lab", "topic", "code")
@dataclass
class Project: code: str; kind: str; customer: str; platform: str; path: str; memory_key: str; state: str; created: str
def load(p: config.Paths) -> list[Project]      # projects.tsv, strict like the register, header fixed
def spawn(p: config.Paths, kind: str, goal: str, customer: str | None, register_path: Path, tags: list[str] = ()) -> Project
    # refuses: unknown kind; customer given but not a CUST code in the register; goal or tags with a name hit
    #   (check.check_text) or with a blocklist hit; a folder that already exists.
    # creates <projects_root>/<tcp-code>/ with SCOPE.md (code, kind, customer code or none, tags, goal, created,
    #   mode sealed), STATE.md (empty state template), OPEN.md, RESOURCES.md, input/, evidence/,
    #   deliverables/, reviews/, a CLAUDE.md of at most 30 lines pointing at the Workbench rules,
    #   `git init`, first commit; registers the project (memory_key = the absolute path with '/' -> '-').
def close(p: config.Paths, code: str) -> Project   # state -> closed (archive later)
```
CLI `awb spawn KIND --goal TEXT [--customer CODE] [--tag T]...`, `awb projects list`.

## `awb/cli.py`

`awb init`, `awb intake [--customer CODE|new] [--force] FILE...`, `awb register add CODE KIND FORM`,
`awb register new KIND` (prints a fresh code), `awb register list` (codes, kinds, counts of forms; the forms
themselves only with `--forms`, this is the vault side), `awb register retire CODE`, `awb check`,
`awb gate`, `awb spawn`, `awb projects list`. Exit codes: 0 ok, 1 findings or blocked, 2 usage or error.

## Additions after the review of 2026-09-22

- `Extraction.scan_text`: raw text that is checked for names only. `awb.extract` adds `INCOMPLETE`,
  `is_incomplete()`, `temp_root()`, `make_temp_dir()`. `office.extract_office(path, kind, depth=0)`.
- `normalize` also decodes RFC 2047 words, JSON and RTF escapes, quoted-printable runs and double percent encoding,
  and removes every default-ignorable code point. `Normalized.to_original_end` maps the end of each character.
- `matcher.fold_text`; the matcher finds forms spread out by any separators, look-alike letters and case changes.
  `Matcher.replace` raises on overlapping spans.
- `register.next_sub_code(entries, parent, kind)`.
- `intake.unknown_candidates(text, known, rules=...)`, `intake.STRONG_RULES` (used by spawn), `report.render_minimal`.
- `check`: a path that carries a form is printed as `file N`; class `path`; `--no-register`.
- `gate`: printed paths are masked; `--no-register`, `--register`, `--repo`; `GateError`, `DETECTORS`, `CLASSES`;
  class `opaque` for files it cannot read. The gate fails closed when no register can be loaded.
- `cli.SafeParser` never echoes an argument. `projects.spawn` refuses without a register and refuses a goal that
  looks like it carries an unregistered name.

# Release 2 (decided 2026-09-22): seal, vault encryption, client hooks, knowledge base, ledger, career, writing checker, review

Same ground rules as above. Each module has `main(argv) -> int` and parses its options with `cli.SafeParser`; the
command line reaches it through `cli.DELEGATED` (already wired). Exit codes 0 ok, 1 findings or blocked, 2 error.
Unix socket paths may not exceed 100 characters: tests put sockets under a short folder from
`tempfile.mkdtemp(prefix="awb", dir="/tmp")`, never under pytest's `tmp_path`.

## The seal (his decision: a new system user without sudo on this host)

Two users. The owner (the user who runs `sudo`, `$SUDO_USER`) keeps the vault (`<owner-home>/tcp-vault`, owner
home 750, the work user is not in its group). The work user `awb` (no sudo, not in the groups ubuntu, docker, adm, sudo, lxd) runs every working session.
Its home `<work-home>` holds `tcp-shared/`, `tcp-kb/` and the projects `tcp-<code>/`. The owner is in group `awb` so
that the intake can write the outbox (`tcp-shared/outbox`, mode 2770, group awb). Code for the work user is installed
read-only to `/opt/tcp-awb` (root-owned, own venv, `/usr/local/bin/awb` points at it). Paths come from
`/etc/awb/paths.conf` (see `awb/config.py`).

### `awb/vault.py`: encryption, the daemon, the client

Encryption with gpg symmetric AES256 through subprocess, always with `--batch --pinentry-mode loopback
--no-symkey-cache --homedir <vault>/.gnupg` (a private homedir, mode 700). The passphrase goes through its own pipe
(`--passphrase-fd N`, `pass_fds`), data through stdin and stdout. Never a plaintext temporary file. Functions:

```python
def encrypt_bytes(data: bytes, passphrase: str, homedir: Path) -> bytes
def decrypt_bytes(data: bytes, passphrase: str, homedir: Path) -> bytes   # raises VaultError("wrong passphrase or damaged file")
class VaultError(Exception): ...          # messages carry no value
class VaultLocked(VaultError): ...
class VaultUnavailable(VaultError): ...   # no daemon, socket missing, refused, timeout
```

Daemon `awb vault serve` (run by systemd as the owner; in tests in a thread with short socket paths):

- Two unix sockets. Admin socket `Paths.admin_sock` (mode 600, owner only). Check socket `Paths.check_socket`
  (mode 660, group of the daemon process, folder created if missing). Protocol: one JSON object per line, one
  answer per request, requests up to 4 MB, 10 s read timeout per connection.
- States: `plain` (register.tsv exists unencrypted, no .gpg: works without unlock), `locked` (register.tsv.gpg
  exists, no passphrase in memory), `unlocked`. Status never reveals counts of forms to the check socket.
- Check socket ops: `{"op":"check","text":S}` answers `{"ok":true,"hits":[{"start":..,"length":..,"cls":..}]}` using
  the same logic as `check.check_text` (normalize, matcher over the register, structured patterns), positions in the
  normalized text; never a code, never a form. `{"op":"ping"}` answers `{"ok":true,"state":"plain|locked|unlocked"}`.
  When locked: `{"ok":false,"error":"locked"}`. Rate limit 600 requests per minute per peer uid, then
  `{"ok":false,"error":"rate"}`. Every check call is logged to `<vault>/log/checks-YYYY-MM.tsv` (time, peer uid from
  SO_PEERCRED, text length, hit count). Never the text.
- Admin socket ops (owner only): `unlock` (passphrase; verified by decrypting the register), `lock` (drops the
  passphrase and the register from memory), `status` (state, count of codes and forms), `register_load` (returns the
  register text), `register_save` (text; the daemon validates it with `register` rules, encrypts and writes it
  atomically, reloads its matcher), `seal_file` (path of a plaintext file inside the vault: writes `<path>.gpg`,
  overwrites and removes the plaintext), `open_file` (path of a `.gpg` in the vault: returns its content base64).
- `awb vault unlock` reads the passphrase with getpass (or from stdin with `--stdin`), sends it to the admin socket.
  `awb vault lock`, `awb vault status`. `awb vault encrypt` (one time): asks for a new passphrase twice, encrypts
  register.tsv, keep.tsv, every file under originals/ and reports/, removes the plaintext (overwrite then unlink),
  then tells the daemon to unlock with it.

Client `vault.check_remote(text: str, sock: Path) -> list[dict]` raises `VaultLocked` or `VaultUnavailable`.

Integration (change these existing modules, keep every existing test green):

- `check.check_text(text, register_path)`: if the register file is readable, work locally as now; else if
  `register_path` is None or unreadable, use `check_remote` with `config.paths().check_socket`; if that raises,
  raise `check.CheckUnavailable` (fail closed). `awb check` and `awb gate` exit 2 with "name check unavailable: vault
  locked" or "...: no vault daemon" and never report clean in that case.
- `register.load(path)`: when `path` does not exist but `path.with_suffix(".tsv.gpg")` does, load through the admin
  socket (`register_load`). `register.save` likewise through `register_save`. The intake and `awb register` then work
  unchanged for the owner while the vault is encrypted.
- `intake`: originals and private reports are sealed (`seal_file`) after writing when the vault is encrypted.

### `seal/`: setup and verification (bash, run by the owner with sudo, idempotent)

- `seal/setup.sh [--dry-run] [--mirrors]`: everything described above: user and group, home modes, the owner in
  group awb, move `<owner-home>/tcp-shared` to `<work-home>/tcp-shared` if it exists, create `<work-home>/tcp-kb` (git
  init), install `/opt/tcp-awb` from the repository (since T1: `git archive HEAD` into `releases/<commit>`, venv with
  system site packages, one `.pth` line, the exchange of `src`; see "The deploy" at the end), `/usr/local/bin/awb`,
  write `/etc/awb/paths.conf`, install and start every `seal/*.service` (the vault daemon: User=<owner>, Group=awb,
  RuntimeDirectory=awb, RuntimeDirectoryMode=0750), copy the
  owner's `~/.ssh/authorized_keys` to the work user, install `seal/work-claude/settings.json` and
  `seal/work-claude/CLAUDE.md` into `<work-home>/.claude/`. Home folders are read with `getent passwd`, never
  written into the script. `--mirrors` adds read-only bind mounts of the public doc
  mirrors under `/srv/tcp-mirrors/` with fstab entries. `--dry-run` prints every command and changes nothing, and
  works without root. Refuses to run when a step would touch the owner's vault or password store.
- `seal/verify.sh` (sudo): as the work user each of these must fail: read the register (plain or .gpg), list the
  vault, read the owner's password store, `.claude`, `.ssh`, any folder in the owner's home, `sudo -n true`; the work user is
  in none of the groups above; the check socket answers ping; the work user can write `tcp-shared`; the owner can
  write the outbox. Prints PASS or FAIL per line, exit 1 on any FAIL.
- `seal/work-claude/settings.json`: hooks of the next section with absolute `/usr/local/bin/awb`, permissions deny
  for `mcp__*` (connectors of the account hand raw customer text to a session, T-101), no other change.
- `seal/work-claude/CLAUDE.md`: at most 60 lines: codes only, never repeat a name he types, intake is his, where
  things are, the ledger and career commands, the writing rules in short, English note rule.
- `awb/seal.py`: `awb seal check` runs the parts of verify.sh that need no root (for the current user) and prints PASS
  or FAIL. Tests: `bash -n` on every script, `setup.sh --dry-run` output contains each required step and no command
  touches the owner's vault or password store except to set their modes.

## `awb/hooks.py`: client hooks for working sessions

`awb hook NAME` reads the hook JSON from stdin. Messages never carry a name or a code of the register.

- `prompt` (UserPromptSubmit): check the prompt text through `check.check_text` (remote when the register is not
  readable). A hit: exit 2, stderr "the prompt carries a registered name (N hits). Register new names with `awb
  register` in your own shell and write the code instead." Unavailable check: exit 0 with a JSON
  `additionalContext` warning, so a locked vault never blocks typing. Since the red team of the hooks
  (2026-09-27) the check reads what hides behind a base64 or hex block and under a bidi override, and the prompt
  is also refused when it carries a secret of the gate's classes (`SECRET_CLASSES`: secret, private-key, token),
  counts and classes only.
- `pre-write` (PreToolUse on Write|Edit|MultiEdit|NotebookEdit, added 2026-09-24): exit 2 when the target lies
  inside the knowledge base (relative paths, `~` and variables, links and hard links resolved): facts go in
  through `awb kb add`, `amend` and `retire` only. A tool input the hook cannot read (not an object, a path with
  a NUL byte) blocks too.
- `post-write` (PostToolUse on Write|Edit|MultiEdit|NotebookEdit): check the written file; for files under a
  folder named `deliverables` at any depth (whatever its case, also through a link) also run `writing.check_file`
  (blocking tells only). A hit: exit 2 with class and line numbers. Also exit 2 for a secret, a bidi override, a
  folder on the path that carries a registered name and a file over `MAX_WRITE_BYTES` (4 MB; `MAX_DELIVERABLE_BYTES`,
  1 MB, under deliverables), which is not read at all.
- `stop` (Stop): when `review.status(project)` lists a deliverable without a valid record, exit 2 with "run the
  review for: <file names made of codes>"; when `stop_hook_active` is true in the input, exit 0 (no loop).
- `session-start` (SessionStart): print JSON with `additionalContext`: the project's SCOPE.md, the first 60 lines of
  STATE.md, OPEN.md, counts of expired knowledge entries, days since the last career update when over 90. A
  section is withheld on any hit, on a bidi override and when the check cannot run; a section cut at
  `SECTION_CHARS` is cut at a line end and checked 200 characters past the cut. The project folder's name is shown
  only when it passes the name check ("this project" otherwise).
- Every hook stops itself after `HOOK_BUDGET` seconds (50, under the client's own 60) and fails closed: exit 2 for
  prompt, pre-write and post-write, a warning in the context for session-start.
- `projects.spawn` writes `<project>/.claude/settings.json` with these five hooks, using `sys.executable -m awb hook
  NAME` as the command, so projects get them before the seal exists.

## `awb/kb.py`: the knowledge base (T-30 to T-36)

Folder `Paths.kb` (a git repository). One file per fact: `entries/KB-XXXX.md` with a header of `key: value` lines,
a line `---`, then the statement. Keys: id, scope (tcp|hcs), tags (comma list from `rules/tags.txt`), grade
(live|contract|docs|said|assumed), checked (ISO date), class (availability 30 days | api 180 days | stable 365 days),
expires (computed), source, tried (for a negative, `;`-separated, at least two), retired (ISO date, only when the
entry was withdrawn), why (one line, only then). `INDEX.md` is generated.

- `awb kb add --scope S --tag T... --grade G --class C --source TEXT [--tried TEXT]... [--force-new] STATEMENT|-`
  refuses: no English (Cyrillic letters, or more than 15 % German function words), a name (check_text, remote when
  needed), any register code or project code, an identifier or secret (gate detectors identifier, homepath, secret,
  token), a price (a number with EUR, € or "per hour"), a negative statement ("cannot", "not supported", "does not
  exist", "no ... available", "not possible", "unavailable") without two `--tried`, an unknown tag. Shows the three
  most similar entries (word overlap) and refuses above 0.6 unless `--force-new`.
- `awb kb amend [--checked D] [--force-new] ID STATEMENT|-` replaces the statement of an entry and keeps its id,
  its scope, its tags, its grade, its class, its source and its tried texts. Every check of `add` runs again, a
  negative against the tried texts the entry carries, and the entry itself is left out of the similarity check.
  `checked` moves only when it is given, `expires` follows it.
- `awb kb retire --why TEXT ID` withdraws an entry: the file, its history and its id stay, `find`, `scope`,
  `expired` and INDEX.md stop offering it, `show` still prints it with the date and the reason. The reason goes
  through the text checks of a statement. A retired entry is never compared for near duplicates, so the corrected
  fact can be added, and it cannot be retired or amended again.
- `awb kb find WORDS...` ranked by word overlap, with grade, checked and an EXPIRED flag. `awb kb show ID`.
- `awb kb scope TAG`: a Markdown briefing: facts by grade, negatives with what was tried, expired entries.
- `awb kb expired`: entries past `expires`.
- `rules/tags.txt`: the controlled tag list (ecs, evs, obs, vpc, elb, nat, dns, rds, rds-mssql, dcs, cce, cci, swr,
  iam, kms, cbr, csbs, waf, cfw, dc, er, vpn, eip, ims, bms, deh, gpu, modelarts, llm-gateway, terraform, api,
  pricing, ms-licensing, vdi, windows, linux, backup, dr, migration, network, security, monitoring, logging, sap,
  hcs-general, other).

## `awb/ledger.py` and `awb/career.py` (T-40 to T-46)

Ledger: `<shared>/ledger/YYYY-Www.jsonl`, appended under an exclusive `fcntl` lock. Fields: date, project (tcp code
or "none"), customer (CUST code or "none"), partner (PART code or "none"), tags (from `rules/tags.txt`), kind (proof,
inquiry, pricing, tender, training, tooling, code, other), done, outcome, deliverables (list), open (list).

- `awb ledger add --kind K --done TEXT [--project C] [--customer C] [--partner C] [--tag T]... [--outcome TEXT]
  [--deliverable P]... [--open TEXT]... [--date D]`: every text is name-checked; codes must have the right shape.
- `awb ledger list [--from D] [--to D]`.
- `awb report --by customer|tech|kind|project [--from D] [--to D] [--format md|html]`: Markdown (or a small HTML
  table page) grouped by the key, newest first inside a group, codes only. Written in plain style: no em-dash, no
  comma before "and" or "or".

Career: `<shared>/career.jsonl`. `awb career add --title --role --stack --outcome --cv-line`: refuses any register
code, project code or name (a career entry names no customer at all). `awb career update [--since D]`: a Markdown
draft with CV bullets grouped by theme (tags) and one LinkedIn paragraph, then `writing.check_text` over it; the date
of the run is stored in `<shared>/career-updated`. `awb career due`: days since the last update, exit 1 when over 90.

## `awb/writing.py` (T-50 to T-56)

```python
@dataclass
class Tell: line: int; cls: str; blocking: bool; hint: str   # hint names the rule, never quotes more than one word
def check_text(text: str, mode: str = "doc", scope: str = "tcp") -> tuple[list[Tell], dict]   # (tells, metrics)
def check_file(path: Path, mode: str = "doc", scope: str = "tcp") -> tuple[list[Tell], dict]
```

Blocking classes: `em-dash` (U+2014, and U+2013 used as a dash between spaces), `comma-and-or` (a comma directly
before "and" or "or"), `banned-word` and `banned-phrase` (`rules/banned-words.txt`, `rules/banned-phrases.txt`, whole
words, case-insensitive), `we-in-mail` (we, our, us in mode mail), `commitment` (I will, I'll, we will, I am raising,
I'm raising, will come back, I promise, I commit), `earlier-mail` (as mentioned, as discussed, in my last mail, as
per my previous, as I wrote), `vendor-name` (scope tcp: words of `rules/vendor-names.txt`), `certification` (BSI, ISO
27001, SOC 2, TISAX, KRITIS, C5, IT-Grundschutz), `bold-lead-list` (a run of three or more bullets where more than one
starts with bold). Reported, not blocking: sentence count, median and 90th percentile of words per sentence against
targets from `rules/voice.txt` (median 11, p90 21), words longer than nine letters (count), lists of exactly three
items. Headings, tables and the file name are checked too. `awb write check FILE [--mode mail|doc|chat] [--scope
tcp|hcs]` prints `cls  line  hint` and the metrics, exit 1 on a blocking tell.

Package 6 (T-52, T-53) added five reported classes, each with its line, none blocking: `fragment` (a sentence of
running prose, not a bullet item, of at most 12 words that ends with . ! or ? and holds no finite verb; a one-word
answer is not counted), `comma-splice` (two clauses with a verb each joined by a comma alone; a clause opened by if,
when, because and the like, an opening word, a comma before then, so or but, an aside and a short tail are not),
`verdict-heading` (a heading that states a conclusion), `connector` (a connector of `other_connectors` in
`rules/voice.txt`; banned connectors block as banned words) and `i-rate` (mode mail, from 40 words: I, my and me
below half of his 29.5 per 1000). A finite verb is found by rule (auxiliaries, contractions, a subject pronoun, a
base verb at the start, a verb form of a built-in list after a word that is not a determiner), so the counts are a
signal and never a gate. Front matter between two --- lines at the top is not a heading. New metric keys:
`fragments`, `fragment_share` (percent of sentences), `comma_splices`, `headings`, `verdict_headings`,
`his_connectors_per_1000`, `other_connectors_per_1000`. `Rules` carries `connectors` and `other_connectors`.

```python
KEEP_KINDS = ("number", "identifier", "negation", "code block")
@dataclass
class Kept: kind: str; before: int; after: int; lines_before: list[int]; lines_after: list[int]   # .same
def facts_of(text: str) -> dict[str, list[tuple[str, int]]]      # (value, line) per kind, plus "one"
def keep(before: str, after: str) -> list[Kept]                   # V-15, one Kept per kind
def keep_files(before: Path, after: Path) -> list[Kept]
def keep_lines(results: list[Kept]) -> list[str]                  # kinds, counts and lines, never a value
def heading_text(line: str) -> str
```

`awb write keep BEFORE AFTER` (V-15, T-56): numbers (the number words two to ninety as digits; "one" pairs with a 1
the other text has), identifiers (digits and letters, . / : + _ inside, a region such as eu-de, capitals, CamelCase,
inline code, URLs, mail addresses), negations (by count; "not supported" and "unsupported" are the same) and fenced
code blocks. Numbers and identifiers compare as sets, so a repeat the pass merged is no change. List and heading
numbers are structure and are left out. Plain capitalised words are not compared. Exit 1 on any difference, 2 when
a file cannot be read.

The drafting skill (T-51, U-12) is `seal/work-claude/skills/drafting/SKILL.md`. `seal/setup.sh` installs it with
the other client files (`CLIENT_FILES`) to `~/.claude/skills/drafting/SKILL.md` of the work user (owner root, mode
644) and sets the immutable flag on the file and on both skill folders. `seal/verify.sh` checks the copy and the
flags. The work user's CLAUDE.md sends every text in his name through it.

`awb voice learn DRAFT SENT`: both files are name-checked first. Writes the pair to `<shared>/voice/pairs/<date>-<n>/`
and a report `<shared>/voice/suggestions-<date>.md`: words and phrases he removed (in DRAFT, not in SENT), words he
added, the change in sentence length and in comma-before-and/or rate, and candidate additions to the banned lists
(a removed word that occurs in two or more pairs). Nothing is added to the rules without his edit.

## `awb/review.py` and `workflows/review.js` (V-01 to V-20, first part)

Per deliverable `<project>/deliverables/<name>`, a folder `<project>/reviews/<name>/`:

- `awb review init DELIVERABLE --request FILE --tier 0..3 --budget WORDS [--reader TEXT]`: writes `contract.md` (the
  request as given, reader, the questions to answer as a checklist he fills, out of scope, budget, tier). The request
  file must pass the name check.
- `awb review claims DELIVERABLE`: writes `claims.tsv` (id, line, kind, risk, sentence up to 200 characters,
  evidence, grade, verdict) with every sentence that carries a number, a price, a negation, an identifier shaped like
  a flavor, a version, an API path or a URL. Risk high for negatives, prices, numbers that enter a total,
  identifiers. Existing rows keep their evidence and verdict when the file is written again.
- `awb review l0 DELIVERABLE`: runs `writing.check_file` (mode from the contract), the name check, the word count
  against the budget and the claim list: every high-risk claim needs an evidence path that exists (relative to the
  project) and a verdict of supported, contradicted or unknown; contradicted blocks; unknown blocks from tier 2.
  Prints the result, exit 1 when blocked.
- `awb review pass DELIVERABLE`: runs l0; for tier 3 also requires `lenses/*.json` files whose findings have no
  blocking item without the outcome confirmed-fixed or refuted; writes `record.json` with the sha256 of the
  deliverable, tier, date, l0 result and lens summary. Exit 1 when it cannot pass.
- `review.status(project: Path) -> list[dict]` and `awb review status [PROJECT]`: every file under deliverables/ with
  record state valid, missing or stale (hash differs).
- `workflows/review.js`: a saved Claude Code workflow for tier 2 and 3 (blind verification of high-risk claims with
  open questions and no draft, one lens per platform plus a partner lens with fixed yes/no questions, a refute agent
  for every blocking finding, results as JSON into `reviews/<name>/lenses/`). It cannot be run in tests: check it with
  `node --check` and a unit test that the phases and the output path are in the file.

## `awb/review.py`, the workflows and `awb/calibrate.py` (V-01 to V-20, second part, 2026-09-25)

```python
KB_FILE = "kb.tsv"; SETTLING_GRADES = ("live", "contract"); SEND_TIER = 3; REQUEST_KIND = "request"
CLOSED_OUTCOMES = ("confirmed-fixed", "refuted", "softened")
class Contract: ...; lowered: str = ""                     # his words when he lowered the tier (decision 14)
def init(deliverable, request, tier, budget, reader=None, mode="doc", force=False, register_path=None,
         today=None, lowered=None) -> Path                   # lowered needs a tier below 3, passes the name check
def kb_candidates(sentence, scope="tcp", kb_where=None, today=None, limit=3) -> list[dict]   # V-04
def render_kb(rows, candidates) -> str                        # kb.tsv: id, risk, "KB-XXXX grade checked state"
def write_claims(deliverable, register_path=None, kb_where=None, today=None) -> ClaimsResult   # + kb_path, kb_rows
def l0(deliverable, register_path=None, kb_where=None) -> L0Result
def run_pass(deliverable, register_path=None, today=None, kb_where=None) -> PassResult
def lens_counts(folder, register_path=None) -> list[dict]     # per lens: kind, findings, blocking, open_blocking,
                                                              # open_other, refuted, confirmed_fixed, softened,
                                                              # plausible, dropped, state (V-12)
def claim_counts(path) -> dict; def contract_counts(path, contract) -> dict; def run_counts(folder) -> dict
SEND_OK, SEND_WARN, SEND_REFUSE = "ok", "warn", "refuse"
def send_check(project, file, for_customer, register_path=None) -> tuple[str, str]   # decision 15
```

- Evidence `kb:KB-XXXX` in claims.tsv: the entry must exist, not be retired and not have expired; a high-risk claim
  needs the grade live or contract (V-04). The state column of kb.tsv says settles, lead or expired.
- record.json keeps its old keys and adds `contract` (tier, budget, mode, scope, lowered, questions, ticked),
  `claims` (rows, high, by_verdict, by_grade, kb_evidence), `per_lens`, `run` (agents, tokens from the lens files)
  and `open_points` (unknown claims, open findings that do not block) (V-18).
- `bucket.put` calls `send_check` first: refuse raises BucketError, warn prints one line to standard error. A
  project without a customer (`none`) is internal.
- `workflows/review.js`: the blind verification runs first, then the lenses with its results in their prompts
  (V-11); a lens finding carries quote, true, evidence and replacement and is dropped and counted in `dropped`
  without a quote or claim id and without evidence or a source (V-09); the refute agent answers confirmed, refuted
  or plausible (V-12); the prepare step takes only high-risk rows with an empty verdict (V-14); verify.json carries
  `run` with the agent runs and the tokens spent (V-18). The phases and the files are those of the first part.
- `workflows/request-check.js` (L6, V-16): phases Request check and Record, one agent that reads only contract.md,
  claims.tsv and the text; writes `lenses/request-check.json` with kind "request", the questions (answered, line)
  and findings: an unanswered question and an overclaim are blocking (MISREAD, MISLEADING), extra content is
  major (EXTRA). No answer at all is one blocking finding.
- `awb/calibrate.py` and `calibration/cases.json` (document 06, section 8): `load(path=None)`, `run(path=None) ->
  Report(rows, caught, missed, not_run, clean, false_alarms, ok)`, `main(argv)`; `awb review calibrate [--json]`.
  Script steps writing, names, l0 and keep run in a temporary folder with a register of the invented names of the
  set; the steps workflow and price are listed as not run. `MAX_FALSE_ALARMS = 2`. Exit 1 on a missed case or too
  many false alarms. `score_lenses(project, path=None) -> list[LensRow(id, cls, state, by)]` and `awb review calibrate
  --lenses PROJECT` score a run of the workflows over `calibration/workflow-cases.json` (13 cases: 8 defects for
  the model steps, 5 clean texts built on live entries); `EXTRA_CLASSES` count an EXTRA finding as caught.

## The run of 2026-09-25 afternoon: leftovers, pictures, package 7

- `gate`: `parse_push_lines(text)`, `pushed_commits(repo, updates)`, `_scan_pushed(repo, updates, matcher)`,
  `push_hook_script(...)`; `awb gate --pushed [--repo PATH]` reads the pre-push lines on standard input and scans
  every file of every pushed commit (labels `commit:path`). `install_hook` writes `pre-push` next to `pre-commit`
  (a foreign `pre-push` is refused without `--force`). The Workbench has `hooks/pre-push`. `_detect_keypair`: a key
  id of 20 capitals and digits followed on the same or the next line by 40 letters and digits in both cases is a
  secret.
- `hooks`: `SECTION_CHARS = 5000` per file and `START_CHARS = 12000` for all files of the start context; the
  checks always follow and say what was cut.
- `planted.CASES`: 101 cases (21 before). `patterns`: an IBAN in lower case (the checksum decides) and a phone
  number with the prefix 00 are found.
- `kb`: `SCOPE_FILE = "SCOPE"`, `folder_scope(where)`; `add` refuses a fact whose scope is not the one the base
  holds. `~/tcp-kb` holds tcp (his decision: HCS gets a project of its own).
- `review`: `_FLAVOR_ANY_RE` puts any flavor-shaped name (s9.huge.8, c7t.28xlarge.4.physical) into the claim list,
  `_MASK_RE` a network mask such as /29. `workflows/review.js` takes `args.mirrors` (default /srv/tcp-mirrors) and
  records the output tokens of the turn over its run (`budget.spent()` is shared by everything that runs).
- `awb/images.py`: `pictures(src, kind)`, `hold(p, customer, file_id, src, kind, encrypted=False)`,
  `held(p, customer=None)`, `release(p, customer, file_id, numbers=None)`, `load_manifest`, `folder`,
  `ImageError`; `awb images list [CUST]`, `awb images release CUST FILEID N...|--all`. The intake holds the
  pictures of every file before its original moves; in an encrypted vault each picture is sealed and released
  through `open_file` of the vault daemon; a release checks the hash of the manifest.
- `awb/tcp/sweep.py`: `Handles(path)` with `handle(kind, rid, name)`, `resolve(handle)`, `mask(text)`, `save()`;
  `sweep(lister, today, handles) -> SweepReport(items, unknown, counts)`, `report_lines(rep)`; `awb cloud sweep`.
  Tags `awb-project` and `awb-expiry`; idle is a stopped server, a disk attached to nothing, an address bound to
  nothing. Names are never kept, the handle file holds handle and id only.

## Candidate rules (his decision: treat them as names)

In `intake.unknown_candidates` and `intake.STRONG_RULES`: a capitalised two-word run at a sentence start is a
candidate unless both words are in `rules/stop-words.txt` or in a small list of sentence-opening words kept in
`rules/sentence-openers.txt`; "First von|van|de|zu|vom|zur Last" is a candidate when the first word is not a German
noun by suffix (-ung, -heit, -keit, -schaft, -tion, -ment, -tät, -ismus, -nis, -tur, -ik, -ei, -er) and not in the
stop words; "Last, First" (two capitalised words around a comma, each 3 to 20 letters, not both in the stop words, not
inside a comma list of three or more capitalised words) is a candidate. A keep list in the vault
(`Paths.keep_list`, one phrase per line, `awb register keep PHRASE`, `awb register keep --list` on the vault side)
removes reviewed phrases from future candidates. In `matcher`: a registered form of 3 or 4 letters also matches glued
inside an identifier token when the rest of the token is digits or one of the affixes in `rules/glue-affixes.txt`
(backup, prod, dev, test, stage, vpn, db, srv, web, app, fw, lb, net, vpc, mail, dc, k8s, cluster, data, share, sql,
bkp, log, mon, gw, api, portal, cloud, ext, int). Update the spec tests that asserted the old behaviour.

## Signatures the builders and the integration added (2026-09-22)

Recorded here so that the next builder works against them. Each module keeps `main(argv) -> int`.

- `vault`: `VaultRateLimited(VaultUnavailable)` (check and gate wait up to 90 s on it), `ping(sock) -> str`,
  `admin_call(op, sock=None, **fields) -> dict`, `seal_path(path, passphrase, homedir) -> Path`,
  `plaintext_files(p)`, `encrypt_vault(p, passphrase) -> int`, `Daemon(p=None, *, admin_sock, check_sock,
  rate_limit, rate_window)` with `start`, `stop`, `serve_forever`, `state`, `stopping`.
- `check`: `CheckUnavailable(register.RegisterError)`, `register_source(path) -> (LOCAL|MISSING|REMOTE, entries)`,
  `LocalCheck`, `RemoteCheck(sock, rate_wait=0.0)` with `ping`, `scan`, `name_spans`, `checker(path,
  rate_wait=0.0)`, `remote_socket()`. MISSING is a readable vault folder without any register: `check_text` treats
  it as empty, every command and hook refuses or warns on it.
- `register`: `encrypted_path(path)`, `parse(content)`, `render(entries)`.
- `hooks`: `client_settings(prefix)`, `command_prefix(executable=None)`, `name_hits`, `file_hits`, `project_root`,
  `hook_prompt|hook_post_write|hook_stop|hook_session_start(data) -> int`, `Unavailable`. A usage or internal
  error exits 1, never 2.
- `seal`: `Result`, `owner_home(p, conf=None)`, `forbidden_groups(home)`, `run_checks(p=None, home=None)`.
- `kb`: `Entry` (with `retired`, `why`, `is_retired`), `KBError`, `Refused(KBError)` (`.reasons`, `.similar`),
  `add(statement, *, scope, tags, grade, cls, source, tried=(), checked=None, force_new=False, where=None,
  register_path=None, today=None)`, `amend(kb_id, statement, *, checked=None, force_new=False, where=None,
  register_path=None, today=None)`, `retire(kb_id, why, *, where=None, register_path=None, today=None)`, `find`,
  `expired(where=None, today=None)`, `get`, `load`, `briefing`, `similar`, `screen`, `parse`, `render`,
  `write_index`. `where` is a `config.Paths` or a folder. `find`, `briefing`, `expired` and the index leave a
  retired entry out; `get`, `load` and `show` still return it.
- `ledger`: `Entry`, `LedgerError`, `Refused`, `add(kind, done, *, project, customer, partner, tags, outcome,
  deliverables, open_items, day, p)`, `load`, `group`, `render`, `report(by, start, end, fmt, p) -> (text,
  withheld)`, `ensure_checkable(reg)`, `append_line(path, obj)`.
- `career`: `Entry`, `Update`, `CareerError`, `Refused`, `add(title, role, stack, outcome, cv_line, *, tags=(),
  day=None, p=None)`, `load`, `draft`, `update(p, since, day) -> Update`, `last_update`, `days_since_update`,
  `due(p, day) -> (days, due)`.
- `writing`: `WritingError`, `Refused`, `Rules`, `load_rules(rules_dir=None)`, `learn(draft, sent, p=None, day=None)`,
  `MODES`, `SCOPES`, `BLOCKING`, `REPORTED`, `CLASSES`; package 6: `Kept`, `KEEP_KINDS`, `facts_of`, `keep`,
  `keep_files`, `keep_lines`, `heading_text`.
- `review`: `ReviewError`, `Refused`, `init`, `write_claims`, `l0`, `run_pass`, `status`, `lens_summary`,
  `find_project`, `locate`, `review_dir`, `sha256_file`; `status` rows are `{"file", "state", "tier"}`.
- `intake` (candidate rules): `unknown_candidates(text, known, rules=..., keep=())`, `keep_key`, `load_keep(p)`,
  `keep_phrase(p, phrase) -> (added, count)`.
- Integration: `config.make_dir(path, mode, *, shared=False)` (the shared side of a sealed host is a setgid tree
  that belongs to the work user: a new folder takes the group bits of its setgid parent, an existing setgid folder
  or a folder of another user is left alone; `ensure_layout`, the intake and `report` use it).
  `intake.IntakeResult.unsealed: int = 0`, `intake.vault_encrypted(p)`, `intake.seal(p, path) -> reason | None`:
  in an encrypted vault every original and private report is sealed right after it is written; `awb intake` exits
  2 when a file could not be sealed. `projects.spawn` runs where the register cannot be read (an encrypted vault, the
  work user): names go through the check socket. On the work side a customer code counts when an intake issued it
  (its outbox folder). `awb init` leaves an encrypted register alone.

## Public names added by the reviews of release 2 (2026-09-22), written down 2026-09-24

- `codes.fold_for_codes(text) -> str`: the text the way a reader sees a code in it (NFKC, invisible characters
  dropped, every dash-like character read as a hyphen).
- `codes.count_codes(text) -> tuple[int, int]`: (register codes, project codes), look-alike forms included.
- `codes.project_platform(folder) -> str`: `hcs` for an `hcs-` project folder, `tcp` for every other one.
- `check.CheckRateLimited(CheckUnavailable)`: the vault daemon refused a check for its rate limit; it passes within
  a minute.
- `vault.vault_lock(p, wait=None)`: the exclusive lock on the vault folder for one intake or one encryption.
- `vault.CHECK_CHUNK = 600_000`, `vault.CHECK_OVERLAP = 20_000`: characters per check request and the overlap of
  two neighbouring chunks.
- `hooks.for_this_user() -> bool`: false when the root host file names another work user.
- `hooks.Unavailable(reason, rate=False)`: a check could not run; `rate` marks the rate limit of the daemon.
- `review.bindings(folder) -> dict`: the hashes of contract.md, claims.tsv and every lens file a record is bound to.
- `review.UNREADABLE = "unreadable"`, `review.UNREADABLE_KINDS`: a deliverable of these kinds never gets a record.
- `review.L0Result.sha256`: the hash of exactly the bytes the level-0 check read.
- `writing.rendered(text) -> str`: the prose as a reader sees it once rendered, line by line.
- `patterns.PLATFORM_DOMAINS`: the public hosts of the platform and how many labels may stand in front of each.

## Package 1 (2026-09-24): fixes before the seal

- `seal/work-claude/CLAUDE.md`: the work rules, at most 60 lines (tests/test_work_rules.py, tests/test_seal.py).
- `projects.RULES_FILE`: the work rules. A spawned project imports them, never the developer instructions.
- `projects.spawn` installs the commit gate into the new project (`gate.install_hook`) after its first commit; a
  project whose gate cannot be installed is not created.
- `config.work_user() -> str | None`: the work user of the root host file (`HOST_CONF`), never of `AWB_CONF`.
- `config.is_work_user() -> bool`. For the work user `config.paths()` and `config.host_conf()` read the root host
  file alone: the environment and `AWB_CONF` are left out.
- `gate.INSTALLED_AWB`, `gate.HOOK_MARK`, `gate.hook_fallback() -> str`.
- `gate.staged_blobs(repo) -> list[tuple[str, bytes | None]]`: the staged added or modified files and their content.
- `gate.hook_script(fallback=None, *, kb=False, installed=INSTALLED_AWB) -> str`: the text of a pre-commit hook:
  the self-test of the gate, then `gate --staged`, then with `kb` also `kb verify --staged`. Any failure refuses.
- `gate.install_hook(repo, *, kb=False, force=False, fallback=None, installed=INSTALLED_AWB) -> Path`: writes
  `.git/hooks/pre-commit`; refuses a repository with core.hooksPath and, without `force`, a hook of another tool.
  Command line: `awb gate --install [--kb] [--force] [--repo PATH]`.
- `hooks/pre-commit` of the Workbench repository: the self-test first, then `--staged`.
- `hooks.hook_pre_write(data) -> int`; `hooks.HOOKS` and `hooks.client_settings` carry five hooks.
- `kb.verify_text(text, name, register_path) -> list[str]`: the checks of `add` over one entry file (form, fields,
  expiry, tags, the text checks, negatives), without the near-duplicate scan.
- `kb.verify(files=(), *, staged_repo=None, register_path=None) -> list[tuple[str, str]]`: (file, reason); a staged
  deletion of an entry is a reason. Command line: `awb kb verify [--staged] [--repo PATH] [PATH...]`, exit 0
  clean, 1 problems, 2 error.

## The anteroom (2026-09-24): the bucket chain, the self-test and the second check of the intake, names back

- `awb/planted.py`: `CODE`, `FORM`, `SHORT` (the invented form of tests/fixtures.py SELFTEST_FORM), `CASES` (label,
  text, class the matcher must report, whether the second check must find it too), `entries()`.
- `intake.SECOND_CLASS = "possible-name"`, `intake.skeleton(text) -> str`, `_Engine.second_hits(text) -> list[Span]`
  (whole words folded to their letters and joined, compared with every registered variant; a single lower-case
  word under six letters and a kept phrase are left out), `_Engine.leaks(text)` (final_hits and second_hits).
  Every output and the public report pass `leaks`.
- `intake.selftest() -> list[str]`: every planted case through an engine of its own; `intake.run` refuses with
  IntakeError before reading anything when it fails.
- `awb/obs.py`: `OBSError(message, status, code)`, `Keys(ak, sk)` (values hidden from repr), `keys_from_pass(entry)`,
  `keys_from_reference("pass:<entry>")`, `Listing(objects, prefixes)`, `Client(bucket, keys, region="eu-de",
  endpoint=None)` with `list(prefix, delimiter, page)`, `head`, `get`, `put_bytes`, `put_file`, `copy`, `delete`.
  Native V2 signing; errors carry the status and the OBS code only.
- `awb/bucket.py`: `IN`, `OUT`, `PULLED`, `BucketError`, `settings()`, `client()`, `months(c)`, `project_folders(c)`,
  `find_folder(c, code)`, `ensure_folder(c, project) -> (folder, created)`, `sync(p, c) -> lines`, `pulled(p)`,
  `pull(p, c, code, again=False) -> dict`, `put(p, c, code, file, replace=False, reveal=False) -> (key, stats)`,
  `move(p, c, old, code) -> dict`, `main(argv)`. Refused as the work user.
- `awb/reveal.py`: `RevealError`, `display_forms(entries)`, `reveal_text(text, entries) -> (text, stats)`,
  `session_places(p)`, `owner_side()`, `write_named(text, out, p, replace=False) -> stats`, `main(argv)`.
- `cli.DELEGATED` carries `bucket` and `reveal`.

## Package 3, first part (2026-09-24): prices, the cloud client, tenant jobs

- `awb/jobs.py` (T-60, neutral): `LIST`, `EMPTY`, `UNKNOWN`, `PRESENT`, `GONE`, `EXIT_STOPPED = 3`,
  `DEFAULT_BUDGET = 100.0`, `Listing(state, items, reason)` with `of(items)`, `unknown(reason)`, `known`,
  `describe()` and no truth value, `MISSING`, `dig(obj, *path, default=MISSING)`, `JobStopped(done)`,
  `Job(name, state_dir, budget=, out=, clock=)` with `say`, `elapsed`, `remaining`, `is_done`, `done_count`, `mark`,
  `step(key, fn, cost=0)`, `finish`, `retry(fn, attempts=3, delay=2.0, job=None, retry_on=(Exception,),
  sleep=None)`, `wait_gone(probe, timeout=, interval=5.0, clock=, sleep=None)`.
- `awb/sign.py` (neutral): `ALGORITHM = "SDK-HMAC-SHA256"`, `canonical_uri(path)`, `canonical_query(query)`,
  `string_to_sign(method, url, headers, body, date)`, `sign(method, url, headers, body, ak, sk, now=None) -> headers`.
- `awb/tcp/` holds the modules of one platform; no module outside it imports it (T-88, tested).
- `awb/tcp/price.py` (T-85, T-86): `API`, `API_ENV`, `TERMS`, `RESERVED`, `CALCULATOR_HOURS`, `PriceError`,
  `parse_amount(text)`, `parse_number(text) -> Number(value, places)`, `Record` (from_raw, price(term), summary),
  `is_hourly(unit)`, `Fetch(listing, url, fetched_at, cached_at, count, stray, unreadable)` with `records` and
  `source_line()`, `api_url()`, `check_region(region)`, `fetch(service, region, base=, job=, timeout=, now=)`,
  `select(records, flavor=, os_text=, grep=, ids=)`, `table(records)`, `snapshot_dir(p)`,
  `write_snapshot(p, fetch, region, replace=, today=)`, `load_snapshot(path)`, `latest_snapshot(p, region)`,
  `Change`, `Diff`, `diff(old, new)`, `read_sheet(path)`, `RowResult`, `check_row(row, fetch_region, today=,
  max_age=)`, `selftest(records, today=)`, `SheetReport` (counts, exit_code), `check_sheet(path, fetcher=, today=,
  max_age=)`, `format_report(report, name)`, `main(argv)`.
- `awb/tcp/cloud.py`: `DOMAIN`, `KEYS_ENV`, `ENDPOINT_ENV`, `CloudError(message, status)`, `Response(status, data,
  text)`, `Client(keys, region="eu-de", endpoint=None, timeout=30.0, job=None)` with `request`, `get`,
  `project_id`, `expand(path)`, `list(service, path, key, query=, paging=, limit=, marker_field=, max_pages=)`,
  `settings()`, `mask(value)`, `main(argv)`. Refused as the work user until T-100.
- `cli.DELEGATED` carries `price` (module `tcp.price`) and `cloud` (module `tcp.cloud`).

## The knowledge refresh (2026-09-25): mirrors, the refresh, recheck, the search

- `awb/tcp/mirror.py`: `ORG`, `SD_PAGE`, `ROOT_ENV = "AWB_MIRRORS"`, `SEALED_ROOT`, `DOCS`, `SD`, `MirrorError`,
  `root()`, `owner_side()`, `list_repos(fetch_json=None) -> Listing`, `sync_repo(repo, dest) -> dict`,
  `load_manifest(folder)`, `sync_docs(base, repos, job, only=None) -> counts`, `pdf_text(data)`,
  `sync_sd(base, fetch=None, page=SD_PAGE) -> dict`, `current_sd(base)`, `status(base)`, `main(argv)`.
- `awb/tcp/refresh.py`: `PARTS`, `VERDICTS`, `Part(name, lines, overdue, error)`, `refresh_dir(p, day)`,
  `part_mirrors`, `part_prices`, `method_for(entry)`, `worklist(entries, today)`, `part_knowledge`, `part_projects`,
  `part_defaults(p, base, code_root=None)`, `run(p, parts=, update=, today=, max_state_age=, job=) -> (parts,
  report)`, `read_results(path)`, `rejected_batches(rows, plants)`, `apply(rows, plants=, where=, today=) ->
  (applied, calibration)`, `main(argv)`.
- `awb/kb.py`: `recheck(kb_id, grade=, source=, tried=(), checked=None, ...)`; `amend(..., tried=None)` replaces
  the tried texts when given; `concepts(text)` (a word and its stem as one unit); `synonym_groups(path=None)`,
  `expand(q, groups)`, `SYNONYMS_FILE`, `SYNONYM_WEIGHT`, `IDF_POWER`; `bench(questions, where=None, top=5)`,
  `load_bench(path)`. `find` drops question words, "do" and the platform's names from the query, counts a word
  with its stem once, weighs rare words by `IDF_POWER` and credits a group of `rules/kb-synonyms.txt` once per
  entry when a whole term of it is there.
- `cli.DELEGATED` carries `mirror` (module `tcp.mirror`) and `refresh` (module `tcp.refresh`).

## Package 5 (2026-09-25): the life of a project

- `awb/projects.py`: `spawn(..., tags=(), from_outbox=False)` refuses a tag that is not in rules/tags.txt (after
  the word checks) and moves the customer's outbox files into input/; `RESOURCE_COLUMNS`, `RESOURCE_STATES`,
  `NOT_PROJECTS`, `IDLE_DAYS = 30`, `MAX_STATE_LINES = 150`; `open_items(folder)`, `live_resources(folder)`,
  `close(p, code, force=False)` (refused while items or live resources are left), `unregistered(p)`,
  `record(p, project, done, tags=(), outcome="")` (one ledger entry about a project).
- `awb/sessions.py` (T-62): `LIVE_MINUTES = 30`, `claim(p, code, session, now=None) -> other claim or None`,
  `beat(p, code, session, now=None)`, `owner(p, code, now=None)`, `short(session)`.
- `awb/english.py` (T-48): `notes_file(p)`, `last_reply(transcript)`, `note_of(reply)`, `keep(p, note, day=None)`,
  `capture(p, transcript)`, `load(p, month=None)`, `main(argv)` (`awb english list`).
- `awb/hooks.py`: `DRAFT_HOURS = 2`, `draft_ledger(p, root, now=None)` (T-41), `_project_notes` in session start
  (the claim, idle, the size of STATE.md), `_beat` in the prompt hook, `english.capture` in the stop hook.
- `awb/career.py`: `Update.candidates`, `CANDIDATE_KINDS`, `masked(text)`, `ledger_candidates(p, since)` (T-46);
  the command prints the candidates after the draft on standard error.
- `awb/cli.py`: `awb close CODE [--force]`, `awb projects check`, `awb spawn ... --from-outbox`; the delegated
  `english`.

## The red team (2026-09-27): what eight finders got through, closed

Record: `calibration/redteam-2026-09-27.md`. Public names added or changed:

- `patterns.PATTERNS` entries are 4-tuples now (kind, pattern, check, label), the check may return an int (see the
  module section above); `patterns._view(text) -> (view, positions | None)`.
- `matcher._MARKUP_RE` blanks comments, CDATA, processing instructions, declarations and tags with line breaks or
  long attributes; `_Skeleton.find` lets a key of letters start or end inside a run when the rest is digits or, for a
  long key, a glued word of three letters or more; `_fold_char` folds the digits of every script and four more
  look-alike letters (Cyrillic omega, izhitsa, straight u, Latin z with stroke).
- `normalize`: numeric entities without a semicolon and with any digit count, RTF `\uN`, `\xNN`, a quoted printable
  soft break in a text with other QP marks, punycode labels (`_PUNYCODE_RE`), terminal control sequences
  (`_CSI_RE`); `normalize.strip_invisible(text) -> str`.
- `intake._Engine.sanitize` strips the invisible characters of the output, hit or no hit.
- `extract.text`: `MIN_LINE_BLOCK = 24`, `_QUOTE_PREFIX_RE`, `_HEX_DUMP_RE`; `_decoded_blocks` yields 6-tuples
  (start, end, decoded or None, multiline, kind, block) and yields hex blocks that decode to binary; `_decode_hex`
  takes text that is mostly digits; `noscript` text stays in `strip_html`; kind "svg" is read like html.
- `extract.sniff`: a PDF header within the first 1024 bytes, "svg" (`TEXT_KINDS`), HEIF, EMF and WMF as "image".
- `extract.pdf.extract(path, depth=0)`; `keyed_strings(path) -> list[str]` ("key: value" lines of annotation
  contents and subjects, form field values, outline titles and scripts, into `detect_text`); embedded files saved
  with pdfdetach and read as children with `meta["attachment"]` (the saved name) and a note.
- `images.PICTURE_KINDS = ("image", "svg")`, `IMAGE_EXTS` with emz, wmz, heic, heif, avif, ico.
- `planted.CASES`: 180 cases (101 before).

## Arithmetic and migrations (2026-09-30)

`awb/calc.py` (core, R-005): `evaluate(expression, names=None) -> Decimal` (plain arithmetic only, CalcError
otherwise, the message never echoes the expression), `bind(lets) -> dict`, `text(value, places=None) -> str`,
`record(project, expression, lets, result, label="") -> Record` (appends to `<project>/calc/calc.tsv` under a lock,
ids K-1, K-2 ...), `load(project)`, `find(project, id)`, `appears_in(result, sentence) -> bool`. `awb/review.py`
accepts `calc:K-N` as evidence and requires it for a high-risk sentence whose number was computed (`_ARITH_RE`).

`awb/tcp/migrate.py`: `PHASES` (discover, map, estimate, plan, review; worker files, shell or none),
`init(project, source)`, `load_state(project)` (done phases whose artifacts changed turn stale with every later
phase), `next_phase(state)`, `done(project, name) -> list[str]` (the gate; empty means recorded),
`parse_azure_size(size) -> dict | None`, `inventory(project, file, table=None) -> dict`,
`flavors_from_records(records) -> dict[class, list[Flavor]]`, `pick(flavors, vcpu, ram)`,
`map_rows(inventory, classes) -> list[dict]`, `write_mapping`, `load_mapping`,
`estimate(project, fetch_region, term="PAYG", hours=720) -> dict`, `os_tier(os_text)`. The phase files are in
`plugin/skills/azure-to-tcp/phases/`, the workers in `plugin/agents/`.

## The key service (2026-10-01, T-100)

`awb/tcp/keys.py`: `store_entries(root=None)`, `collect(entries, reader)`, `Service(admin_path, call_path,
endpoint=None, paths_fn=config.paths, log_dir=None)` with `start()`, `stop()`, `serve_forever()`;
`fill(body, secrets)`, `request(sock, obj)`, `unlock(sock=None, entries=None, reader=pass_reader)`, `main(argv)`.
Admin socket ops: ping, status, load, lock. Call socket ops: ping, tenants, call. `awb/tcp/cloud.py` gained
`call` and `tenants`, which go through the service for every user. The unit is `seal/awb-keyd.service`.

## The deploy (2026-10-07, T1)

`awb/deploy.py`, `awb deploy` (DELEGATED, refused for the work user). `main(argv, *, root=OPT, etc=Path("/etc"),
run_dir=Path("/run"), runner=subprocess.run, geteuid=os.geteuid, **more)`; `more` fills the other fields of `Host`
(`environ`, `module_file`, `getpwnam`, `getgrouplist`, `clock`, `sleep`, `cwd`, `bin`, `daemons`). Hidden
subcommands, run by root as the owner and refused as root: `plan --json [options]`, `archive COMMIT` (the `git
archive` stream on standard output), `unlock`, `status --json [--probe SOCKET]...`. The root path imports neither
`getpass` nor `socket` nor `awb.vault` nor `awb.tcp.keys`: every function that does is named `owner_*`.

- Units: `templates(tree) -> {unit: {"kind", "files", "dropins"}}` from `seal/*.service`, `seal/web/*.service`,
  `seal/web/*.socket` and `seal/web/*.d/*.conf`; no unit name in the module. `unit_map(tree, installed=None,
  root=OPT) -> UnitMap` with `units`, `imap`, `argvs`, `entries`, `closures`, `reached` and `effects(changed) ->
  Effects` (`units`, `templates`, `sync`, `unmapped`, `every`). `import_map(tree)` follows every import of the
  package at module and function level; `DYNAMIC` names the targets of importlib calls (key: file and function),
  `COMMAND_ONLY` the modules no unit imports, with a reason each; `RULES` the files outside the package. The two
  daemons are found by their command (`DAEMON_COMMANDS`).
- Trees: `DirTree(folder)`, `GitTree(repo, commit, runner)`.
- The plan: `owner_plan(opts, repo, host, tree=None) -> dict` (one JSON object); `validate_plan(plan,
  allowed_units)` (commit ids of 40 hex characters, units from the template set of the installed release, fixed
  actions, kinds and states, boolean flags, printable text).
- Root: `entry_checks(host) -> Caller`, `take_lock(run_dir) -> fd` (`O_CREAT` 0600, `flock`, never unlinked, not
  inherited), `vault_busy(vault)`, `Run(host, caller, plan, opts)` with the steps `start_journal`, `extract`,
  `install`, `daemons`, `services`, `wait`, `unlock`, `read_status`, `sync`, `final_check`, `finish`;
  `flip(root, release)` (the exchange of the flip back), `retain(releases, keep)`, `check_archive(tar)`,
  `check_tree(folder)`, `status_lines(journal, status, show)`.
- The drop: `child_env(pw, environ)` (built: `HOME`, `USER`, `LOGNAME`, `SHELL`, `PATH` the secure default,
  `XDG_RUNTIME_DIR`, `LANG` and `TERM` of the caller, `GPG_TTY`), `drop(pw, getgrouplist)` (`user`, `group`,
  `extra_groups`), `as_user(runner, cmd, pw, environ, cwd, ...)`.
- The journal `/opt/tcp-awb/DEPLOYED` (root, 644): `state` (`running`, `done`), `target`, `previous`, `started`,
  `finished`, `boot`, `flip` (the monotonic clock in microseconds, written before setup.sh runs), `units` (unit:
  `action`, `kind`, `result`), `units_from`, `pending`, `running` (unit: the release it was last started on,
  kept by the retention). `/opt/tcp-awb/deploy.log`: one line per run.
- The releases: `/opt/tcp-awb/releases/<commit>/` (and `pre-deploy`, the plain folder of before T1), `src` a
  relative symlink to one of them, `venv/lib/python3.X/site-packages/awb.pth` with the one line `/opt/tcp-awb/src`,
  `/usr/local/bin/awb` the wrapper of T3. `awb/__init__.py` pins `__path__` to its real folder as its first
  statements, so a running process keeps importing from the release it started on.
- `seal/setup.sh --update [--units-only] [--mirrors DIR...]` from a release folder; `AWB_SETUP_OPT` and
  `AWB_SETUP_BIN` move the install root and the bin link for the tests (refused as root; only the code step runs).
  `seal/web/install.sh [--domain HOST] [--only UNIT...]`.
