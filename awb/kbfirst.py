"""Knowledge base first (task KF, his decision of 2026-10-28).

A session in a project spent a 35-minute live run on an OBS 403 with federated temporary AK/SK; only at the end,
while it harvested its facts, it found facts of other projects that IAM-granted OBS rights took 20 to 30 minutes
to work while a bucket policy worked in seconds. Read first, the bucket policy would have been tested at once.

So the prompt hook adds, once per task, a context block with the facts of the knowledge base closest to the prompt
and to the project's SCOPE.md:

    Known from the knowledge base (N facts on this topic; open one with `awb kb show ID`, search more with
    `awb kb find WORDS`):
    KB-XXXX live 2026-09-02: <one line>
    ...

A task is new on the first prompt of a session, on a prompt after more than SILENCE seconds without one and on a
prompt that names a customer question, a run or a deliverable (TRIGGER_RE) whose closest facts are not all told in
this session yet. The facts are ranked by `kb.find`, the ranking of `awb kb find`; a fact counts only when it
shares at least MIN_UNITS content words with the query, so a short "ok, go on" finds nothing. The facts pass
`kb.screen`, which checks no content (B-089). When the knowledge base cannot run, the
block is left out and the prompt goes on: this is a lead, never a gate.

After the facts the block adds, under the line LESSONS_HEAD, at most MAX_LESSONS lesson cards (awb/kbpattern.py,
DESIGN 8) whose words match the prompt, as leads and never as facts: the cards of the project of the session itself
are left out, as are those past their best-before, retired or screened out. A project that cannot be told by its
folder name gets no lessons.

What a session was told lives in the session mark of awb/rulesync.py under the key "kb": the time of the last
prompt and the ids told.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from awb import codes, config

SILENCE = 2 * 3600
"""Seconds without a prompt after which the next prompt starts a new task."""
MAX_FACTS = 10
MAX_CHARS = 1500
"""The block never grows past this many characters: the closest facts first, the rest stay behind `awb kb find`."""
MAX_LESSONS = 3
LESSONS_HEAD = "Learned in other projects (leads, not facts)"
MIN_UNITS = 2
"""Content words of the query a fact must share to count."""
LINE_WIDTH = 120
HEAD = ("Known from the knowledge base (%d facts on this topic; `awb kb show ID` opens one, `awb kb find WORDS` "
        "finds more; say in one line what is known; before a live run look here for a known result or trap):")
TRIGGER_RE = re.compile(
    r"(?i)\b(?:"
    # a run
    r"run|rerun|re-run|test|try|прогон\w*|прогна\w*|запуст\w*|провер\w*|протестир\w*|тест\w*|попробу\w*"
    # a customer question
    r"|customer|client|question|asks?|клиент\w*|заказчик\w*|вопрос\w*|спрашива\w*"
    # a deliverable
    r"|deliverable|draft|answer|reply|offer|concept|mail|e-?mail|document|письм\w*|ответ\w*|черновик\w*"
    r"|документ\w*|концепц\w*|оффер\w*|предложени\w*|напиши|подготовь"
    r")\b")
"""Words that start a new task: a run ("прогони", "run", "проверь"), a customer question or a deliverable."""
_SCOPE_KEYS = ("- goal:", "- tags:")
_MAX_QUERY = 4000


def is_trigger(text: str) -> bool:
    return bool(TRIGGER_RE.search(text or ""))


def _scope_query(root: Path) -> str:
    """The goal and the tags of the project's SCOPE.md, empty when it cannot be read."""
    try:
        text = (Path(root) / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    out = []
    for line in text.splitlines():
        for key in _SCOPE_KEYS:
            if line.startswith(key):
                value = line[len(key):].strip()
                if value and value != "none":
                    out.append(value)
    return " ".join(out)


def _ranked(query: str, scope: str, where) -> list:
    """Entries of `kb.find` for `query` that share at least MIN_UNITS content words with it, in its order."""
    from awb import kb

    if not query.strip():
        return []
    units = [u for u in kb.concepts(query) if not u <= kb._QUERY_STOP]
    if len(units) < MIN_UNITS:
        return []
    out = []
    for _, e in kb.find(query, where=where, scope=scope):
        ew = kb.words(e.statement) | kb.words(" ".join(e.tags))
        if sum(1 for u in units if u & ew) >= MIN_UNITS:
            out.append(e)
    return out


def closest(prompt: str, root: Path | None, scope: str = "tcp", where=None,
            register_path: Path | None = None) -> tuple[int, list]:
    """(how many facts count, the closest MAX_FACTS of them): those closest to the prompt first, then those closest
    to SCOPE.md, each once; the shown ones screened like `awb kb find`. Raises what the base or the check raises."""
    from awb import kb

    seen: dict[str, object] = {}
    for query in (prompt[:_MAX_QUERY], _scope_query(root) if root is not None else ""):
        for e in _ranked(query, scope, where):
            seen.setdefault(e.id, e)
    found = list(seen.values())
    shown, withheld = kb.screen(found[:MAX_FACTS], register_path)
    return len(found) - withheld, shown


def lessons(prompt: str, root: Path | None, where=None, p: config.Paths | None = None) -> list:
    """The closest MAX_LESSONS lesson cards that share at least MIN_UNITS content words with the prompt and that
    the project of `root` did not write. Raises what the cards or the check raise."""
    from awb import kb, kbpattern

    if root is None or not codes.PROJECT_RE.fullmatch(Path(root).name):
        return []
    units = [u for u in kb.concepts(prompt[:_MAX_QUERY]) if not u <= kb._QUERY_STOP]
    if len(units) < MIN_UNITS:
        return []
    own = kbpattern.supported_by(Path(root).name, p)
    out = []
    for _, c in kbpattern.find(prompt[:_MAX_QUERY], where, kind="lesson"):
        cw = kb.words(c.statement) | kb.words(" ".join(c.tags))
        if c.id not in own and sum(1 for u in units if u & cw) >= MIN_UNITS:
            out.append(c)
    shown, _ = kbpattern.screen(out[:MAX_LESSONS], p.register if p else None)
    return shown


def _lesson_line(c) -> str:
    from awb import kb

    text = c.statement + (" Why: %s" % c.reason if c.reason else "")
    return "%s %s %s: %s" % (c.id, c.sub, c.checked, kb._one_line(text, LINE_WIDTH))


def render(facts: list, count: int | None = None, leads: list = ()) -> str:
    """The block: HEAD with the count, then one line per fact while MAX_FACTS and MAX_CHARS allow, then the lesson
    cards under LESSONS_HEAD; their room is kept free first, so ten facts never push them out."""
    from awb import kb

    tail = [_lesson_line(c) for c in leads[:MAX_LESSONS]]
    tail = [LESSONS_HEAD] + tail if tail else []
    room = MAX_CHARS - sum(1 + len(l) for l in tail)
    lines = [HEAD % (len(facts) if count is None else count)] if facts else []
    size = sum(1 + len(l) for l in lines) - 1
    for e in facts[:MAX_FACTS]:
        line = "%s %s %s: %s" % (e.id, e.grade, e.checked, kb._one_line(e.statement, LINE_WIDTH))
        if size + 1 + len(line) > room:
            break
        lines.append(line)
        size += 1 + len(line)
    return "\n".join(lines + tail)


def block(p: config.Paths, session_id: str | None, root: Path | None, prompt: str, scope: str = "tcp",
          now: float | None = None, where=None) -> str | None:
    """The prompt hook: the block when this prompt starts a new task and the base knows something, else None.
    Records the time of the prompt and the ids told in the session mark. Never raises."""
    try:
        return _block(p, session_id, root, prompt, scope, time.time() if now is None else now, where)
    except Exception:
        return None


def _block(p, session_id, root, prompt, scope, now, where) -> str | None:
    from awb import rulesync

    if not session_id or root is None or not isinstance(prompt, str):
        return None
    path = rulesync._mark(p, session_id)
    mark = rulesync._read_mark(path)
    state = mark.get("kb") if isinstance(mark.get("kb"), dict) else None
    last = state.get("last") if state else None
    told = [x for x in (state.get("told") or []) if isinstance(x, str)] if state else []
    first = state is None or not isinstance(last, (int, float)) or now - last > SILENCE
    trigger = is_trigger(prompt)
    new_state = {"last": now, "told": told[-200:]}
    said = None
    if first or trigger:
        try:
            count, facts = closest(prompt, root, scope, where if where is not None else p, p.register)
        except Exception:
            count, facts = 0, []
        try:
            leads = lessons(prompt, root, where if where is not None else p, p)
        except Exception:
            leads = []
        ids = [e.id for e in facts[:MAX_FACTS]] + [c.id for c in leads]
        if (facts or leads) and (first or not set(ids) <= set(told)):
            said = render(facts, count, leads)
            new_state["told"] = (told + [i for i in ids if i not in told])[-200:]
    mark["kb"] = new_state     # no digest here: a mark without one still gets the rules notice (rulesync)
    rulesync._write_mark(path, mark)
    return said
