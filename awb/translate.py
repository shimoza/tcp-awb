"""`awb write translate FILE --to de|en|ru [--to ...] [--mode mail|doc] [--scope tcp|hcs] [--verify]`

The German version and his Russian reading copy of a draft are made from the accepted text in one step, never by
rewriting (the work profile of 2026-10, proposal 4: 95 requests about translation, German texts polished in 38 % of
the cases).

For each language it writes FILE.<lang>.md next to FILE (the suffix of FILE dropped: offer.md gives offer.de.md):

1. The source must be accepted: `writing.check_text` finds no blocking tell in it (a Russian source is exempt).
2. The model gets the source with fixed rules (keep every number as digits, every identifier and code block as it
   is, add nothing, drop nothing). The model is reached through the Ask path of the Workbench (awb/tcp/ask.py: its
   key, its daily budget, its model), and only when the key is readable by the user who runs the command. The
   work user cannot read it (/etc/awb/anthropic.key belongs to awb-ask), so in a working session there is no model
   path: the command writes the prompt file and stops with one line. The session, itself a model, translates along
   the prompt into FILE.<lang>.md and runs the same command with `--verify`. No new credential exists for this.
3. `writing.keep(source, result, across=True)` proves that every number, identifier and code block is still there.
   A difference refuses (exit 1): the model's text goes to the prompt folder as <name>.<lang>.refused.md and
   nothing is written next to FILE; with `--verify` the session's file stays where it is and must be fixed.
4. `writing.check_text` on the result in the mode of the source. A blocking tell refuses (exit 1). The Russian
   reading copy is for him alone and is exempt, as it is in the stop hook.

The prompt file lies in <project>/reviews/.translate/ when FILE is in a project (so it never counts as a
deliverable), else next to FILE. Before a text leaves for the model the name check runs over it; a registered name
or a check that cannot run stops the call. Messages carry kinds, counts, line numbers and the language, never a
value and never the file name.

Exit codes: 0 every language written and clean, 1 refused (source not accepted, keep or check failed), 2 a usage,
read or name-check problem, 3 no model path (the prompt file is written).
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from awb import config, writing

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2
EXIT_PROMPT = 3

NAMES = {"en": "English", "de": "German", "ru": "Russian"}
EXEMPT = ("ru",)
"""Languages whose result is not run through the writing check: his Russian reading copy never leaves."""
MAX_TOKENS = 16000
MAX_SOURCE_CHARS = 60_000
PROMPT_DIR = ("reviews", ".translate")

SYSTEM = """You translate a draft of a cloud architect from {source} into {target}. The draft is accepted: translate,
never rewrite, improve or shorten it.

Rules:
- Keep the Markdown structure: the same headings, lists, tables and paragraphs in the same order.
- Keep every number as digits exactly as in the source, also where the source spells a number as a word. A decimal
  comma or point may follow the target language.
- Keep every identifier as it is: product and service names, region names such as eu-de, flavors, host names,
  paths, codes such as CUST-Q7M4 or tcp-q7m4, words in capitals, URLs and mail addresses.
- Keep every fenced code block and every inline code span byte for byte.
- Add nothing and drop nothing: no greeting, no note, no explanation of the translation.
- Write as the author, in the first person singular: I, never we.
- German: the polite form Sie, plain short sentences, no dash between clauses.
- Russian: a plain reading copy for the author himself.
Answer with the translated text alone."""


class TranslateError(Exception):
    """A usage, read or check problem. The message carries no value. Exit 2."""


@dataclass
class Result:
    lang: str
    state: str                     # written, refused, prompt
    out: Path | None = None
    prompt: Path | None = None
    lines: list[str] = field(default_factory=list)


def target_path(path: Path, lang: str) -> Path:
    """FILE.<lang>.md next to FILE: offer.md gives offer.de.md, notes gives notes.de.md."""
    path = Path(path)
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    return path.with_name("%s.%s.md" % (stem, lang))


def _project_root(path: Path) -> Path | None:
    return config.scope_root(path.parent)


def prompt_folder(path: Path) -> Path:
    root = _project_root(Path(path).resolve())
    return root.joinpath(*PROMPT_DIR) if root is not None else Path(path).resolve().parent


def build_prompt(text: str, source: str, lang: str) -> tuple[str, str]:
    """(system, user) of the model call."""
    return SYSTEM.format(source=NAMES[source], target=NAMES[lang]), text


def prompt_text(system: str, user: str, out: Path, verify: str) -> str:
    return ("# Translation prompt\n\nWrite the translation into `%s`, then run `%s`.\n\n## Instructions\n\n%s\n\n"
            "## Source\n\n~~~~~~~~~~ text\n%s\n~~~~~~~~~~\n" % (out, verify, system, user.rstrip("\n")))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- the model


def _ask_module():
    """awb.tcp.ask, the Ask path of the platform, or None. Imported by name: the core runs without the platform
    package (tests/test_price.py), and without it there is no model path."""
    try:
        return importlib.import_module("awb.tcp.ask")
    except Exception:
        return None


def default_sender():
    """(sender, key) of the Ask path when its key is readable here, else None. Tests replace it."""
    ask = _ask_module()
    if ask is None:
        return None
    try:
        key = ask.read_key()
    except Exception:
        return None
    return ask.post, key


def _screen(text: str) -> None:
    """A registered name never leaves for the model; a check that cannot run stops the call."""
    from awb import check, config

    try:
        hits = check.check_text(text, config.paths().register)
    except Exception:
        raise TranslateError("the name check did not run, so nothing was sent to the model") from None
    names = sum(1 for h in hits if isinstance(h, dict) and h.get("cls") == "name")
    if names:
        raise TranslateError("the source carries %d hits of the name check: replace them with codes" % names)


def ask_model(system: str, user: str, sender, key) -> str:
    """The text of the model's answer, counted against the daily budget of the Ask path."""
    ask = _ask_module()
    if ask is None:
        raise TranslateError("the Ask path is not installed, so no model can be asked")
    left_q, left_t = ask.budget_left()
    if left_q <= 0 or left_t <= 0:
        raise TranslateError("the daily budget of the Ask path is used up; it resets at midnight")
    tokens = 0
    try:
        resp = sender({"model": ask.MODEL, "max_tokens": MAX_TOKENS, "output_config": {"effort": ask._effort()},
                       "system": system,
                       "messages": [{"role": "user", "content": user}]}, key)
        usage = resp.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    except ask.AskError as err:
        raise TranslateError(str(err)) from None
    finally:
        try:
            ask.spend(tokens, questions=1)
        except OSError:
            pass
    if resp.get("stop_reason") in ("refusal", "max_tokens"):
        raise TranslateError("the model gave no full translation (%s)" % resp.get("stop_reason"))
    text = "\n\n".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text").strip()
    if not text:
        raise TranslateError("the model answered with no text")
    return text + "\n"


# --------------------------------------------------------------------------- the proof


def _keep_lines(source: str, result: str) -> tuple[bool, list[str]]:
    kept = writing.keep(source, result, across=True)
    lines = [line.replace("BEFORE", "the source").replace("AFTER", "the translation")
             for line in writing.keep_lines(kept)]
    return all(k.same for k in kept), lines


def _check_lines(result: str, lang: str, mode: str, scope: str) -> tuple[bool, list[str]]:
    if lang in EXEMPT:
        return True, ["check  exempt: the %s reading copy is for him alone" % NAMES[lang]]
    tells, _ = writing.check_text(result, mode, scope)
    block = [t for t in tells if t.blocking]
    lines = ["%s  %d  %s" % (t.cls, t.line, t.hint) for t in block]
    lines.append("check  %d blocking" % len(block))
    return not block, lines


def translate_one(path: Path, source: str, src_lang: str, lang: str, mode: str, scope: str, verify: bool,
                  model) -> Result:
    out = target_path(path, lang)
    if verify:
        try:
            result = writing.read_text(out)
        except (OSError, writing.WritingError):
            return Result(lang, "refused", lines=["the %s version is not there or not text: write it first"
                                                  % NAMES[lang]])
    else:
        system, user = build_prompt(source, src_lang, lang)
        folder = prompt_folder(path)
        if model is None:
            prompt = folder / ("%s.%s.prompt.md" % (out.name[: -len(".%s.md" % lang)], lang))
            verify_cmd = "awb write translate %s --to %s --mode %s --scope %s --verify" % (
                path, lang, mode, scope)
            _write(prompt, prompt_text(system, user, out, verify_cmd))
            return Result(lang, "prompt", out=out, prompt=prompt)
        result = ask_model(system, user, *model)
    kept, lines = _keep_lines(source, result)
    if not kept:
        if not verify:
            refused = prompt_folder(path) / ("%s.%s.refused.md" % (out.name[: -len(".%s.md" % lang)], lang))
            _write(refused, result)
            return Result(lang, "refused", prompt=refused, lines=lines)
        return Result(lang, "refused", out=out, lines=lines)
    clean, more = _check_lines(result, lang, mode, scope)
    if not verify:
        _write(out, result)
    return Result(lang, "written" if clean else "refused", out=out, lines=lines + more)


def translate(path: Path, langs: list[str], mode: str = "doc", scope: str = "tcp", verify: bool = False,
              model=None) -> tuple[list[Result], list[str]]:
    """(results, notes). `model` is (sender, key); None asks `default_sender` unless `verify` is set. Raises
    TranslateError for a usage, read or check problem and writing.Refused when the source is not accepted."""
    path = Path(path)
    langs = list(dict.fromkeys(langs))
    for lang in langs:
        if lang not in writing.LANGUAGES:
            raise TranslateError("the language must be one of %s" % ", ".join(writing.LANGUAGES))
    try:
        source = writing.read_text(path)
    except OSError as err:
        raise TranslateError("the file cannot be read (%s)" % type(err).__name__) from None
    if len(source) > MAX_SOURCE_CHARS:
        raise TranslateError("the file is longer than %d characters: translate it in parts" % MAX_SOURCE_CHARS)
    src_lang = writing.language_of(source)
    if src_lang in langs:
        raise TranslateError("the source is already in %s" % NAMES[src_lang])
    notes = ["source  %s" % NAMES[src_lang]]
    if src_lang not in EXEMPT:
        tells, _ = writing.check_text(source, mode, scope)
        block = sum(1 for t in tells if t.blocking)
        if block:
            raise writing.Refused("the source is not accepted: %d blocking tells, run awb write check on it and fix "
                                  "them first; nothing was translated" % block)
    if not verify:
        model = model if model is not None else default_sender()
        if model is not None:
            _screen(source)
    return [translate_one(path, source, src_lang, lang, mode, scope, verify, model) for lang in langs], notes


def main(argv: list[str]) -> int:
    from awb.cli import SafeParser

    prog = "awb write translate"
    ap = SafeParser(prog=prog, description="Make the German, English or Russian version of an accepted draft in "
                                           "one step and prove that every fact is kept.")
    ap.add_argument("file", type=Path)
    ap.add_argument("--to", action="append", choices=writing.LANGUAGES, required=True)
    ap.add_argument("--mode", choices=("mail", "doc"), default="doc")
    ap.add_argument("--scope", choices=writing.SCOPES, default="tcp")
    ap.add_argument("--verify", action="store_true",
                    help="no model: prove the translation the session wrote next to the file")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    try:
        results, notes = translate(args.file, args.to, args.mode, args.scope, args.verify)
    except writing.Refused as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return EXIT_REFUSED
    except (TranslateError, writing.WritingError) as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
    prompts = [r for r in results if r.state == "prompt"]
    if prompts:
        # no model path: one line, nothing else (the rest of the languages wait for --verify the same way)
        where = ("%s/" % "/".join(PROMPT_DIR) if prompts[0].prompt is not None
                 and prompts[0].prompt.parent.name == PROMPT_DIR[-1] else "the folder of the file")
        print("no model path for this user: the prompt%s %s in %s as %s; translate along each, then run this "
              "command again with --verify" % ("s" if len(prompts) > 1 else "", "are" if len(prompts) > 1 else "is",
                                              where, ", ".join("<file>.%s.prompt.md" % r.lang for r in prompts)))
        return EXIT_PROMPT
    for line in notes:
        print(line)
    refused = False
    for r in results:
        name = NAMES[r.lang]
        for line in r.lines:
            print("%s  %s" % (name, line))
        if r.state == "written":
            print("%s  %s next to the source as <file>.%s.md, every number, identifier and code block kept"
                  % (name, "proved" if args.verify else "written", r.lang))
        else:
            print("%s  refused%s" % (name, ", the model's text is in the prompt folder" if r.prompt else ""))
            refused = True
    return EXIT_REFUSED if refused else EXIT_OK
