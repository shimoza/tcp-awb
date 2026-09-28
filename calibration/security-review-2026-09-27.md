# Adversarial review of the security core, 2026-09-27

Five finders on Fable 5.1, one per area, each with scripts on throw-away Workbenches and daemons on sockets
under a scratch folder; every finding with a shown script went to three skeptics with different lenses
(correctness, the threat model, the promise), a majority decides. The threat model they read:
`seal/README.md`, the seal section of `INTERFACES.md` and `BRIEF-SEC.md` of the run (the owner with sudo, the
work user `awb` running every session, a hostile file coming in through the intake, another user of the host).
The seal is not set up on this host yet; findings marked "sealed" hold only once it is.

## Findings

| id | area | severity | skeptics | what | sealed | fix |
|---|---|---|---|---|---|---|
| SEC-SEAL-1 | the seal | high | 2 of 3 | `config.make_dir(shared=True)` follows a symlink the work user can plant in the outbox (it owns the shared tree, the outbox has no sticky bit) and chmods the link's target to 2770; a link to the owner's home opens it to the work user | yes | done: `config.make_dir` refuses a link where a folder should be or on the way to it (the shared root is resolved), and chmods through a directory handle opened with `O_NOFOLLOW`; the outbox gets the sticky bit (3770) |
| SEC-SEAL-2 | the seal | medium | 3 of 3 | `report.write_replace` opens its temp file without `O_EXCL` or `O_NOFOLLOW`; a planted symlink turns the public report into an owner-level overwrite of any file the owner can write | yes | done: `report.write_replace` opens a temp file with a new random name, `O_EXCL` and `O_NOFOLLOW`, and refuses a link on the way to the folder |
| sec-readers-1 | the readers | high | 3 of 3 | `pdfdetach -saveall` honours `../` in an embedded file's name and wrote outside the temp folder with the owner's rights (code added the same day) | no | done, a8dd048: each file saved by number to a path the reader chooses and checked to lie in the folder |
| sec-readers-2 | the readers | medium | 3 of 3 | held pictures had no total budget: a 4 MB office file with two hundred 19 MB pictures took four gigabytes of memory | no | done, a8dd048: `MAX_TOTAL_IMAGE_BYTES` read from the container, not from the declared sizes |
| sec-readers-3 | the readers | medium | 1 of 3 | a hard link given to the intake is moved and chmoded like an owned file; the other name keeps the inode | no | refuted: only the owner can make that link; noted |
| sec-vault-1 | the vault | medium | 3 of 3 | the check socket's rate limit counts requests, not candidates: one 4 MB request confirms hundreds of thousands of guessed names (membership only, no prefix oracle) | yes | done: `MAX_CHECK_CHARS` per request (700,000; "send it in pieces") and `RATE_BYTES` per window and uid (16 MiB) next to the request count |
| sec-vault-2 | the vault | medium | 3 of 3 | a check scan has no CPU budget; eight concurrent 4 MB checks stall the owner's admin socket five thousandfold | yes | done: `MAX_CONCURRENT_SCANS` (2) with `SCAN_WAIT` (5 s), a third scan gets `rate`; the admin socket answers meanwhile |
| sec-vault-3 | the vault | low | 3 of 3 | deeply nested JSON on the check socket raises RecursionError past the handler's catch, a traceback in the journal (no value in it) | yes | done: `RecursionError` is caught with `ValueError` on both sides of the socket |
| sec-vault-4 | the vault | low | 3 of 3 | `open_file` on the admin socket returns the plaintext of `register.tsv.gpg`, a second door beside `register_load` (owner only) | no | done: `open_file` and `seal_file` refuse the register in both forms |
| sec-private-1 | the private side | high | 2 of 3 | a released picture reaches the outbox with its metadata (EXIF, PNG text chunks, an SVG description) unscrubbed and never name checked | no | done: `images.release` scrubs JPEG APP1 to APP13, APP15 and COM segments and PNG tEXt, zTXt, iTXt and eXIf chunks, then name checks the text of an svg and the strings of every other format; a hit keeps the picture on the vault side |
| sec-private-2 | the private side | low | 3 of 3 | `awb images` has no owner-side guard and shows a traceback with the vault path to the work user | yes | done: `awb images` refuses the work user without a path; an operating system error prints its type only |
| sec-private-3 | the private side | low | 1 of 3 | free text of a goal, a ledger line, a career field, a knowledge entry or an English note takes an unregistered name | no | refuted: the register decides what a name is; noted |
| sec-session-1 | the session | low | 2 of 3 | the pre-write guard of the knowledge base resolves symlinks but not hard links | no | done: `hook_pre_write` compares device and inode with the files of the knowledge base when the target has more than one link |
| sec-session-2 | the session | low | 1 of 3 | a hook payload of an unexpected shape (a prompt that is a list) passes rather than blocks | no | done: a tool input that is not an object or a path with a NUL byte blocks |
| sec-session-3 | the session | low | 0 of 3 | the `mcp__*` deny bounds ingestion, not egress | no | refuted: out of the seal's scope, to be stated in the README |
| sec-session-4 | the session | low | 1 of 3 | the commit gate is a session-owned control (`--no-verify`, `core.hooksPath`) | no | refuted: a guard rail for honest sessions, stated in the README; push stays off |

Notes worth keeping: the daemon's unit had no `LimitCORE=0`, so a core dump of the running daemon could hold the
passphrase and the decrypted register (a guess, not shown; the unit forbids core dumps now); the passphrase itself goes to gpg through its own pipe
and never through an argument or the environment; the rate limit is shared across connections per uid and a slow
request dies at the read timeout; `register_save` and a check never hand out a half-written register; the archive,
mail and office readers flatten every member name and write with `O_EXCL`; every error path of the readers carries
a type name and a count, never a value; the `--reveal` temporary file is mode 600 in a 700 folder and removed on an
error; the work user's commands cannot be flipped by `AWB_CONF` or any environment variable.

The scripts of the finders live in the session scratch folder (`rt/work/sec-*/`), not in the repository.
