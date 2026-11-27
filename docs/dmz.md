# Customer data: the entry check, inside and the exit check

What the harness does with customer data at the entry, inside a project and at the exit. Back to the [README](../README.md).

## Customer data

The Workbench runs on whatever model the architect's assistant uses. That model may be public. With a key to a
private model, customer data may go in and nothing needs to be hidden. On a public model the harness keeps live
customer data out of the model as far as it can:

- `awb import CODE` turns documents into working copies with a code (`CUST-XXXX`) in place of every registered
  name and a token (`[person 1]`, `[company 1]`, `[place 1]`, `[name 1]`) in place of every other name the rules
  recognise, without a stop and without a review. While it runs it prints `checking 13 files` and then one line per
  file (`file 3 of 13: F-XXXX, 0.8 s`) on standard error, with the file id and never a name. It also takes out bank data, tax ids, phone numbers, mail addresses,
  secrets and hidden text. The entry keeps registered names and real secrets only (DMZ 1, the owner's word of
  2026-11-25): IP addresses, host names, web addresses (the platform's endpoints included), hashes, UUIDs and tenant,
  domain or project numbers arrive as written, never a finding and never a token, so that a technical handover (Terraform,
  a Postman collection) stays usable. A technical word is never a name: `VMs`, `IPs`, `ECSs`, `VPCs`, `SGs`, an acronym
  or its plural, a hyphen compound in lower case such as `pre-shared-key` and the words of `rules/allowed-terms.txt`.
  A word of the German or English lists (`rules/known-words-de.txt`, `rules/known-words.txt`, `rules/common-words.txt`)
  and a product or vendor name (`rules/vendor-names.txt`, `rules/source-platforms.txt`, `rules/tech-terms.txt`) is
  never a learned form and a guessed name matches whole words only: a letter of any script, a digit or a hyphen next to
  it makes it part of a word, so `Grafikkarte` stays whole (B-090, ask 2026-11-26-tcp-6edq-2). In a text whose language
  is German a run of capitalised words needs a first name of `rules/first-names.txt` in it, every German noun being
  capitalised (a run that opens with an organisation word such as `Stadt` or `Landkreis` is still a name). A BIC needs a
  country code of ISO 3166 at its positions 5 and 6 and is no dictionary word, so a chapter title in capitals is none.
  A paragraph or a list item of a docx is a heading only by a heading style, a picture leaves no empty `#` line and a
  docx with text carries no "no text layer" note. The line `wiped:` of the import and the table of `intake-report.md`
  use the same class words (person, company, place, name; the report says registered for a registered form).
  `awb images release CUST-XXXX FILEID N... [--project CODE]` puts a released picture into `input/FILEID-img/` of the
  project and the take note says so; the commit gate of a project checks no content (DMZ 3), so it can be committed. A
  script line that only reads or sets a variable (`pm.environment.set("token", pm.response.json().access_token)`,
  `{{token}}`, `$env:TOKEN`) is no secret; a literal password or key still is `[secret]`. The same entry rules hold for
  `awb import --paste`, `awb paste`, `awb material add`, `awb start --from`, `awb inbox take`, the bucket folder `in/`
  and the console. The laptop clean (`awb clean`) is another zone and keeps its tokens. What the rules do not recognise passes (a first name alone in prose, a name in a picture),
  and the limits are written down. It is tested with 1,188 invented cases of structured data and written forms and
  405 cases of unregistered names in mails, transcripts, decks, code, tables, German and Russian text
  (`calibration/redteam/`).
- Inside your own assistant session (desk or development, UX10) two forms run: `awb import CODE --trusted DIR|FILE...`
  (a folder gives every regular file of it and its subfolders) and `awb import CODE` of the bucket folder `in/`, never
  the vault inbox. `--customer`, `--review`, `--redo`, `--paste`, a FILE without `--trusted` and a customer whose
  forms are still to be typed stay in your own terminal; the work user is refused every import.
- `awb import CODE --paste [--kind mail|transcript|table]` is the same door for text you copy from a mail client, a meeting tool
  or a spreadsheet (work profile: a pasted mail with a signature, a pasted transcript or an Excel extract was the usual
  slip). The text comes on standard input (`pbpaste | awb import tcp-xxxx --paste`) and is never printed. A secret refuses the
  whole paste with its class and line. A mail loses its signature and its quoted chain headers as a whole, a transcript keeps
  its turns as `[person 1]`, `[person 2]` and its timestamps, a table keeps its columns and loses the columns of people and
  mail addresses. The copy lands in the outbox as `mail-<date>-<n>.md`, `transcript-<date>-<n>.md` or `table-<date>-<n>.csv`. A
  text you wrote yourself is still your brief and goes into the chat.
- `awb material add [--kind mail|transcript|table|note] [--title WORDS]` keeps text that holds no name at all, without leaving
  the project session. Write `material:` and the text in the chat; the session pipes what follows that word, unchanged, to
  the command inside the project folder and reports the one line it prints (the file name). The text passes the secret scan, the
  home-path rule, your blocklist and the name check through the check socket of the work user. A hit of any of them (a
  secret, a name, a mail address, bank data or a phone number; an IP address, a host name or a web address is no hit) refuses the whole text with its class and line, never the value, and nothing is
  written. On a pass the text lands in `input/` of the project as `note-<date>-<n>.md` (`mail-`, `transcript-` with `.md`,
  `table-` with `.csv` by kind) and one line goes to the ledger. Nothing is wiped or rewritten, so a text that may hold
  names still goes in through `awb import CODE --paste` in your own terminal, which stays as it is.
- `awb clean FILE --project tcp-xxxx --kind docs|logs` does the same anonymization on the owner's laptop before
  anything reaches the host, offline, and `--put` drops the clean copies into the project's `in/`, where the timer
  of `awb import --auto` takes them with `--trusted` (see "Clean material on your laptop first").
- The register of names lives in a vault on the owner's side. The sealed setup runs every session as a separate
  system user that cannot read it.
- Seven hooks watch every prompt, every written file, every file or shell call and the start and end of every
  session. They read no content inside a project (DMZ 2, the owner's word of 2026-11-25: the checks stay at the entry
  of customer material and at the exit of the public repositories). A prompt, a paste, the result of a background
  agent or a CI event passes with names, addresses, mail addresses and passwords; the prompt hook refuses one thing, a
  private key block (`BEGIN ... PRIVATE KEY`) or an access key pair (a key id next to its secret), because it would
  leave the host for the model, and a check that cannot run never refuses. A file written inside a project is not
  checked for names or secrets and not masked; outside a project folder the write hook keeps its name and secret
  checks. The start and the compaction show SCOPE.md, STATE.md, OPEN.md and the pinned block as written. The writing
  check of a deliverable (banned words, vendor names) and the bidi override stay.
- The commit gate runs no content check inside (DMZ 3, the same word of 2026-11-25). A commit in a project folder (an
  ask project too), in a build clone and in the working copy of the public repository `tcp-awb` passes with names, IP
  addresses, UUIDs, hashes, tenant numbers, home paths and keys; its hooks run `awb gate --staged --inside` and
  `--message --inside`. Three things stay there because they are no content: a co-author line that names an assistant
  (the commit-msg hook of a project takes it out), an item of a project's `OPEN.md` without a date and a text file over
  8 MiB. A commit of the public repository is checked at its push: `pre-push`, the push of `main` by the runner and
  the merge message of `awb pr take` keep the registered names, secrets, private keys, tokens, home paths and your
  blocklist, and never report an id (32 hex, a UUID, a tenant, domain or project number), an address or a host name.
  The runner's prompt, its result lines (subjects, test ids, the end of the suite log) and the deploy check no content.
- Hook notes go into one footer line (Q-0005): the start of a project session tells it to put every note of the
  hooks (a notice, a release line, a receipt, a request, the reason a reply was held) into one line under its
  answer, `Notes: ` and the notes joined with `; `, never a paragraph and never above the answer. A prompt that is
  an interim agent or background task notification gets no note, and neither does the stop after it: the notes
  wait for the next prompt or stop. The checks and blocks run on a notification as on any prompt.
- The prompt, stop and pre-compact hooks count frictions (Q-0024): one line per event in `<shared>/ux/events.tsv`
  with the UTC time, the user kind (owner or work), the session kind (project, development or desk), the project
  code or `-` and a class. A prompt that passed its checks gets the class of the first phrase of
  `rules/ux-phrases.tsv` it carries (status question, nudge, repeated rule, bare yes, pasted block), matched in
  memory only; one that matches nothing writes no line. The stop hook writes `held-draft` or `held-note` when it
  holds a reply, the pre-compact hook `compaction`. A counted line never carries a word of the prompt and calls no
  vault. See "Counted frictions" in [owner-guide.md](owner-guide.md#counted-frictions).
- A development session (a checkout of this repository, no project folder) does not end a turn on its own next step:
  the stop hook reads the reply and holds it once, with one line on stderr, when its last paragraph carries a phrase
  of `hooks.NEXT_STEP_PHRASES` ("следующее по плану", "теперь чиню", "начинаю с", "жду проверку", "next:", "i will
  now" and the like, whole words, case ignored) and the reply names no step for him (no line "Твои шаги:" or "Your
  steps:" with text other than "нет" or "none"). The line says to do the step now, to wait on a build task or check
  with `awb build wait ID` in the background so the app wakes the session and to end the turn only on a step that is
  his. The hold is a `held-note` event; `stop_hook_active` lets the next stop through. Project and desk sessions are
  not read.
- A session on the sealed host starts only when every guard answers: the hooks, the commit gate of the project, the
  vault daemon, the key service, the installed rules and the host mode. Its context opens with "guards: 6 of 6
  active"; a missing guard refuses the start and every prompt with one line that names it. `awb board show` counts
  the sessions refused today.
- A tool guard keeps a session inside its project: a file or shell call that reaches another project, the client's
  folder, a key folder or the session records is refused before it runs and logged. The shell part reads the command
  line as the shell splits it and refuses a path it cannot read (a variable, a quote or an escape inside it, a cd it
  cannot follow), so a path a program builds at run time still gets past it; the guard keeps an honest session in its
  lane and is no fence against a hostile one. Text is no path: a / of a sed or awk program, of the pattern of a grep,
  egrep, fgrep or rg (a URL or a regular expression with slashes, the value of `-e` and of `--glob`), of a quoted
  sentence or of a here-document body passes, and text is refused only when it names a place inside a refused one;
  the files a search names stay paths. A loop
  `for f in ch1/en.md ch2/de.md; do sed -n 1,5p "$f"; done` passes when its body passes for one path; a list item
  that is a variable, a substitution, an absolute or home path or has a `..` is refused. The Workflow tool
  runs only the Workbench's own `review.js`, `request-check.js` and `final-check.js` of the installed code, by
  their scriptPath.
- Knowledge base first (task KF): when a prompt starts a new task (the first prompt of a session, a prompt after
  more than two hours of silence or one that names a run, a customer question or a deliverable, such as "прогони",
  "run" or "проверь"), the prompt hook adds "Known from the knowledge base (N facts on this topic ...)" with the
  closest facts of `awb kb find` to the prompt and to SCOPE.md, at most ten and about 1500 characters, once per task.
  After the facts, under "Learned in other projects (leads, not facts)", it adds at most three lesson cards of
  `awb kb pattern` whose words match the prompt, never a card of the project of the session itself or one past its
  best-before. When the base cannot be read it adds nothing. `awb review claims` marks each claim in kb.tsv as known before the
  work or not; the final check carries the same yes/no column, so you see what was known and what is new.
- A gate checks every commit for names, secrets, home paths and the owner's blocklist. A text file over 8 MiB is
  refused at once as `too-large`, unscanned; a large lab log keeps its full text out of git (see lab runs). The lab
  log writers write the home folder part of a path as `~` (`/home/<user>/x` as `~/x`, `C:\Users\<user>\x` as `~\x`),
  so a lab log and its summary pass on the first try. A script under `evidence/` of a project may name the home of
  the work user (`/home/<work user>/tcp-xxxx/...`), where its lab runs read and write; any other home and every
  other file keep the rule. A workbook (`.xlsx`) is read and checked like a text: its cells, formulas, sheet names,
  comments, defined names and document properties; it stays `opaque` when it cannot be read safely (a zip bomb,
  too many parts, bytes outside its parts, a macro, an embedded object or picture, an external link or data
  connection), and a `.xlsm` stays opaque. A fact id (`KB-Q7M4`) or a file id (`F-Q7M4.1`) of the
  Workbench is never read as a name, also when its four letters spell a registered acronym. The commit-msg hook of
  a project takes a `Co-Authored-By:` line naming Claude and a "Generated with Claude Code" line out of the message
  and says so in one line, instead of refusing the commit; every other finding of the message still refuses it. The
  Workbench's own `hooks/commit-msg` and the build hooks still refuse such a line. Customer material is
  checked once, at the entry; a put after it never refuses a file and replaces only its secrets with [secret]
  (task GT). `awb close` still writes the fingerprint of a closed project (sha256 and salted word hashes, no
  text) and pins its hash in the vault.
- The name check learns from its false positives without your marking anything. Every block of
  the gate, `awb check` and `awb import` is recorded inside the vault (`<vault>/log/fp/`, yours alone); the work side
  keeps the count and the class only. Each night `awb check learn` (the timer awb-check-learn.timer) takes a blocked
  host name, id, error code, status line, path, timestamp or service name for harmless when it stands in the public
  corpus (the mirrors, the kb, rules/tech-terms.txt, rules/common-words.txt) or fits a log rule, and keeps it
  blocked when it carries a first name, a title, a legal form, a registered form or a person's name. A learned entry
  relaxes one exact string of one class (web address, IP, MAC, phone), never a name, a mail or a secret. Before the
  list changes, the planted names and the red team wipe pack run against it; one planted name that would pass drops
  the change. Once a week one line of counts reaches you by mail. `awb check allow WORD` releases a word by hand and
  `awb check allow --list` shows the list, in your own terminal only.

These checks are a safeguard, not a guarantee. The architect stays responsible for what goes into a session. Give
the model only technical tasks and keep them simple: a detailed description of a customer's project carries metadata
(region, sector, size, dates, applications) that can identify the customer even without a name and that can break
data protection law.
