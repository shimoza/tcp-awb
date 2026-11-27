# Files in and out

How material reaches a project and how results leave it. Back to the [README](../README.md).

## The exchange: files in and out

Files come in through two inboxes in object storage (OBS). You drop a file there and tell the session in your own
words which one you mean ("the pdf", "the newest", a part of its name). The session takes it with `awb inbox take`.

| inbox | for | what the session gets |
|---|---|---|
| the lab inbox | material without customer content: vendor images, test data, public documents | the file itself, after the name check |
| the owner inbox | anything that may hold customer material | only the sanitised copies of the owner's intake, never the original |

Every project also has its own folder `<YYYY-MM>/<code>/in/` in the owner bucket. `awb spawn` makes it through
the key service. The console puts files there, each of at most 25 MiB and of a kind the console reads; the key
service moves at most `max_mb` of keys.conf (default 100) in one object. `awb/bucket_layout.py` sets the folders, the
kinds and both limits for every side. Each project folder holds a small file `.keep` in `in/` and `out/` (one line of text) so
that S3 clients which cannot open an empty folder open it; `awb bucket folder` and `awb bucket sync` write it when
missing, and every reader of `in/` and `out/` (pull, fetch, import, the inbox and the console listing) ignores it. The owner's own commands (`awb bucket`, `awb import`, `awb dataset put`) reach
the owner bucket through the admin socket of the key service (list, get and put inside these folders, the same limit
and the owner's user id only, and one move: a file of `out/` into `_archive/<stamp>/` of its folder when `awb bucket
put` puts a newer version); there is no bucket key in the environment, and a locked or stopped service stops
them with one line that names `awb keys unlock`. `awb bucket move` needs a copy and a delete that the service does
not offer, so it stops with a line saying so. `awb bucket pull` and `awb bucket move` never run inside an assistant
session, and the client gives no file of `in/` to one, whatever the uid: an original reaches the owner's own
terminal only, as with `awb import` and `awb fetch`. The project code is the one identifier: a customer code exists
only when a customer is registered. A project without one is normal. A file of the owner inbox goes to the
project's customer. The key service checks every file of the lab inbox before the session gets it.

Results go back with `awb xchg put` into the project's own folder. A put never refuses a file for its review, a
name or structured data: a secret is replaced with [secret] in the copy, by the session and again by the key
service and the put says `review: none` or `review: passed DATE`. A Markdown file arrives as md and as HTML to paste into a mail; the output ends with the line saying where both are (Total Commander, the lab bucket and the folder). A mail tells you about each file. `awb
fetch tcp-xxxx` in your own terminal downloads what is new into `~/awb-fetch/tcp-xxxx/` with the names back. Then
it archives every put you fetched 30 days or more ago (see "The archive without a plan" in [install.md](install.md#the-archive-without-a-plan)); `--no-archive` skips
that.

The key of a put is readable and the key service builds it:
`<code>/from-session/<YYYY-MM-DD>/<kind>/<code>-<kind>-<lang>-<HHMM>.<ext>`, for example
`tcp-q7m4/from-session/2026-11-06/offer/tcp-q7m4-offer-de-1432.md`. The kind is the word of `--kind` (`doc` without
it), the language is `--lang` or read from the text, the time is the UTC minute of the put. A second put of the same
key in the same minute gets `-2`, `-3` and never overwrites. `--as NAME` puts the stem of NAME in place of language
and time (`tcp-q7m4-offer-sizing-notes.md`, at most 40 characters of a-z, 0-9 and -) when the name check of the key
service passes it; otherwise the key stays neutral and the session gets one line that the name was not used. The
service runs the name check on the whole key and falls back to `<code>-doc-<lang>-<HHMM>.<ext>` on a hit, so no
name reaches a key, the console, Total Commander or your mail. Keys of the old form `<date>/put-<hex>` stay valid for
`awb xchg list`, `awb fetch` and `awb xchg clean`. `<code>/from-session/INDEX.md` lists every put, newest first:
date, time, key, kind, language, size, review (`none` or `passed DATE`) and fetched (`no` or the date `awb fetch`
downloaded it). The key service rewrites it on every put, on every delete of `awb xchg clean`, when `awb fetch`
marks a key and when it archives one (the row of an archived put goes); it holds checked key parts only, never a title. `awb fetch` writes each file under the name of its key.

## Files in and out

You drop a file into `inbox/` of the lab bucket or of the owner bucket in the OBS console, with no project code
and no command. Then you describe it to the session in your own words. `awb inbox take` matches your words inside
the key service: a kind of file ("the pdf", "the spreadsheet") and "the newest" for every file, the same letters or
a part of the name only for a file of the lab inbox whose name the session may see. A file of the owner inbox or of
`in/` is never found by a word of its name, so no answer tells whether a hidden name holds a word. One match is
taken. None or several list both inboxes, without any name of the owner
inbox. The session takes the one you mean with `--id`. `--all` takes every file of both inboxes. A file of the
owner inbox goes through your intake in wipe mode and the session gets only the sanitised copies; the original goes
to the vault. A code file (`.ps1 .psm1 .psd1 .sh .py .bat .cmd .tf .yaml .yml .json .sql`) arrives as it is: names, secrets and addresses are looked for in its string literals and comments only, and an identifier, a property name or a variable is never taken for a URL or a bank code; a hex or base64 run in it is still decoded and checked. A Word file with tracked changes gets a list `### Tracked changes` after its text: each insertion and deletion with an author number (`author-1`), its text and the 40 characters before it. The intake asks you only about what can be a name: a capitalised word that is a plain English word (`rules/common-words.txt`) or a vendor or technical term (`rules/tech-terms.txt`: KVM, Sapphire Rapids, Credential Guard) is no candidate on its own, nor is anything inside code (a backtick span, a fenced block or a shell command line) or a section letter such as `D. Built`. Two capitalised words in a row stay a candidate unless the phrase is known, and a first name of `rules/first-names.txt` always does. The new files of the project's own folder `in/` are listed beside the inboxes for every project.
"take the files from in" takes all of them through the same intake: for the project's customer if it has one, else
with no customer code and every name a token. A project without a customer takes nothing from the owner inbox. A
text you wrote yourself and paste into the chat is your brief, not customer material: the session works with it.

A file of the lab inbox with a registered name is held, and so is a file of the owner inbox that cannot be read as
text. A file of the owner inbox above `max_mb` of keys.conf, or in a folder that is not exactly one customer code,
stays there and its take is refused. A pdf, a sheet or an archive from the lab inbox lands in `input/opaque/`, which `.gitignore` keeps out of git. You get a mail with the one command to run (`awb import tcp-xxxx`). Results come back with
`awb xchg put` into `<project>/from-session/<date>/<kind>/<readable key>` of the lab bucket, never refused for its review, a name or
structured data: a secret is replaced with [secret] in the copy. A project names its deliverables
`<project code>-<kind word>-<language>.<ext>` (`tcp-q7m4-mapping-de.md`) from the start and the review commands propose one. No object key, log line or mail carries a file name the check did not pass; a mail names a put only by its checked key. You fetch them with `awb fetch tcp-xxxx` in your own terminal (what is new since your last fetch, through the key service, into `~/awb-fetch/tcp-xxxx/` of mode 700 with the names put back as `awb reveal` does) or in the OBS console. A mail tells you about each one by its readable key, which the key service built and checked. The session deletes what it put with
`awb xchg clean CODE --dry-run|--go` (`--older-than DATE` for older date folders), each deletion a row in RESOURCES.md. The key service signs every object call
and refuses everything outside the inbox and the project's own folders, and every put outside from-session/.

With `extract_user` in `/etc/awb/paths.conf` (`seal/setup.sh` writes `awb-extract`, a user with no home and no login) every poppler call of the pdf reader runs in a `sudo -n systemd-run` unit of that user with no network, a read-only system, no home, 512M and 120 seconds, its own IPC and empty /dev/shm, no other process visible, no socket but a local one and no devices, the file on standard input and the text from standard output (at most 64 MB of output, more fails that file). The import report and the intake counts say `extractor: unit` or `extractor: direct`, a unit that fails fails that file and is never run direct, a host file that is there and cannot be read fails the file too (direct runs only when there is no host file or it names no `extract_user`), and embedded files of a pdf are not read in a unit.
