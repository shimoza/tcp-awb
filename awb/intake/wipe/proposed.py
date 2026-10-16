"""The rules of proposed mode (DESIGN.md section 2 of presentations/names): labels, table columns, mail headers,
salutations, sign-offs, speakers, addresses, titles, initials, by-lines, identifiers, logins ... and the rule sets
that order them (PROPOSED_RULES). One family of the wipe rules (F1 step 8); the package text is
awb/intake/wipe/__init__.py.
"""
from __future__ import annotations

import re
from collections import Counter

from awb import codes
from awb.intake.wipe.basic import (_ABBREVIATIONS, _COMPANY_RE, _FORM_ALT, _FUNCTION_WORDS, _MAIL_RE, _PERSON_LOCAL_RE,
                                   _SPACED_RE, _STOP_SINGLES, _TOKEN_RE, _WORD_RE, _company_candidates,
                                   _initial_candidates, _inverted_candidates, _label_candidates, _particle_candidates,
                                   _run_candidates, _spaced_candidates, _title_candidates)
from awb.intake.wipe.lexicon import (_FIRST_NAMES, _GENERIC_ADDRESSEES, _GENERIC_COMPANY_WORDS, _GERMAN_FUNCTION,
                                     _LEGAL_WORDS, _MONTHS_DAYS, _NEUTRAL_LABELS, _PARTICLE_WORDS, _ROLE_WORDS,
                                     _SECTION_WORDS, _SOURCE_PLATFORM_WORDS, _STANDARD_WORDS, _TITLE_WORDS, _UNIT_WORDS,
                                     _compound_known, _generic_word, _known_word, _lettered_list, _letters, _org_word,
                                     _person_known, _role_shape, _speech_shaped)
from awb.matcher import GLUE_AFFIXES, Span


# --------------------------------------------------------------------------- the rules of proposed mode

_TOKEN_RE = _TOKEN_RE
_WORD_RE = _WORD_RE
_CODE_RES = (
    codes.PLACEHOLDER_RE, codes.PROJECT_RE,
    re.compile(r"(?<![\w-])F-[A-Z2-7]{4}(?:\.\d+)*(?![\w-])"),
    re.compile(r"\[(?:person|company|place|name)(?: \d+)?\]"),
)


def _token_spans(text: str) -> list[Span]:
    return [Span(m.start(), m.end(), "token") for rx in _CODE_RES for m in rx.finditer(text)]


def _value_words(text: str, a: int, b: int) -> list[tuple[int, int, str]]:
    return [(a + m.start(), a + m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text[a:b])]


def _trim_generic(words: list[tuple[int, int, str]], extra=frozenset(), months: bool = True) -> list[tuple[int, int, str]]:
    """Drop titles, generic addressees, role words, function words and `extra` at both ends."""
    def drop(w: str) -> bool:
        low = w.casefold().strip(".,;:!?()[]\"'")
        if low in _FIRST_NAMES:
            return False
        return (low in _TITLE_WORDS or low in _GENERIC_ADDRESSEES or low in _FUNCTION_WORDS
                or low in _ROLE_WORDS or _role_shape(low) or low in extra or (months and low in _MONTHS_DAYS))
    while words and drop(words[0][2]):
        words = words[1:]
    while words and drop(words[-1][2]):
        words = words[:-1]
    return words


def _person_value(text: str, a: int, b: int, blocked, *, allow_lower: bool = False,
                  max_words: int = 4, vocabulary: bool = False, months: bool = True) -> tuple[int, int] | None:
    """The span of a person name inside text[a:b]: the words after titles, roles and generic addressees, at most
    `max_words`, blocked words (a registered form) and a parenthesised part cut off at the edges, none of the rest a
    generic word (or, with `vocabulary`, a known word that is no first name), a lower-case word only with
    allow_lower."""
    cut = re.search(r"[(\[<|]", text[a:b])
    if cut:
        b = a + cut.start()
    words = _trim_generic(_value_words(text, a, b), months=months)
    if any(_CAMEL_RE.search(w[2]) for w in words):
        return None
    while words and blocked.hit(words[0][0], words[0][1]):
        words = words[1:]
    while words and blocked.hit(words[-1][0], words[-1][1]):
        words = words[:-1]
    if not allow_lower:
        while words and not words[0][2][:1].isupper():
            words = words[1:]
        while words and not words[-1][2][:1].isupper():
            words = words[:-1]
        if any(not w[2][:1].isupper() for w in words):
            return None
    words = words[:max_words]
    if not words:
        return None
    if any(blocked.hit(w[0], w[1]) for w in words):
        return None
    known = _person_known if vocabulary else (lambda w: _generic_word(w, months=months))
    if vocabulary:
        n0 = len(words)
        while words and known(words[0][2]):
            words = words[1:]
        while words and known(words[-1][2]):
            words = words[:-1]
        if not words:
            return None
        if len(words) < n0 and len(words) == 1 and not (words[0][2].casefold() in _FIRST_NAMES
                                                       or (_name_shaped(words[0][2]) and not _known_word(words[0][2]))):
            return None   # "Fachbereich Logistik" minus the known words leaves no name
    if all(known(w[2]) for w in words):
        return None
    if any(c.isdigit() for w in words for c in w[2]):
        return None
    if len(words) == 1 and (words[0][2].isupper() and len(words[0][2]) <= 5 or codes.is_code(words[0][2])):
        return None   # an acronym or a code is no one-word name
    return words[0][0], words[-1][1]


_GREETING = (r"(?:(?:sehr[ \t]+)?geehrte[rs]?|liebe[rs]?|hallo|hi|hey|hello|dear|moin|servus|grüß[ \t]+gott|"
             r"guten[ \t]+(?:morgen|tag|abend)|привет|здравствуйте|добрый[ \t]+(?:день|вечер)|доброе[ \t]+утро|"
             r"уважаем(?:ый|ая|ые)|доброго[ \t]+(?:времени[ \t]+суток|дня|утра|вечера)|приветствую|"
             r"bonjour|ciao|hola|good[ \t]+(?:morning|afternoon|evening))")
_SALUTATION_RE = re.compile(
    r"(?im)(?:^[ \t>*_-]*|,[ \t]*|\b(?:und|and)[ \t]+)" + _GREETING + r"[ \t,]+(?P<v>[^\n,!:;]{1,60}?)[ \t]*(?=[,!:;.]|[ \t]*$)")
_NAME_FIRST_GREETING_RE = re.compile(
    r"(?im)^[ \t>*_-]*(?P<v>[^\W\d_][\w'’-]*(?:[ \t]+[^\W\d_][\w'’-]*){0,2}),[ \t]*" + _GREETING + r"[ \t]*[,!.]*[ \t]*$")


def _salutation_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for rx in (_SALUTATION_RE, _NAME_FIRST_GREETING_RE):
        for m in rx.finditer(text):
            comma_form = rx is _SALUTATION_RE and text[m.start("v") - 2:m.start("v")].strip().endswith(",")
            value = text[m.start("v"):m.end("v")]
            if comma_form and not all(w[:1].isupper() for w in value.split()):
                continue   # "Привет, Veeam поставил": a sentence after the comma, not an addressee
            span = _person_value(text, m.start("v"), m.end("v"), blocked, allow_lower=not comma_form, max_words=3,
                                 months=False)
            if span:
                sw = text[span[0]:span[1]].split()
                if not any(w.casefold().strip(".,") in _TITLE_WORDS for w in value.split()) \
                        and all(_person_known(w) or _generic_word(w) or _org_word(w) for w in sw):
                    continue   # Hallo Netzwerkteam, Hallo Einkauf: a group, not a person
                out.append((span[0], span[1], "person"))
    return out


_CLOSING = (r"(?:mit[ \t]+(?:freundlichen|besten|herzlichen|lieben|sonnigen|freundlichem|bestem|herzlichem|liebem)[ \t]+gr(?:ü|ue|u)(?:ß|ss)(?:en|e)?|"
            r"(?:viele|beste|liebe|herzliche|freundliche|schöne|sonnige|sportliche|kollegiale)[ \t]+gr(?:ü|ue)(?:ß|ss)e|"
            r"(?:schönen|lieben|besten)[ \t]+gru(?:ß|ss)|gru(?:ß|ss)|gr(?:ü|ue)(?:ß|ss)e|lg|vg|mfg|hg|bg|vlg|glg|"
            r"best[ \t]+regards|kind[ \t]+regards|warm[ \t]+regards|warmest[ \t]+regards|regards|rgds|br|kr|best|"
            r"best[ \t]+wishes|all[ \t]+the[ \t]+best|take[ \t]+care|cheers|thanks|thx|thank[ \t]+you|many[ \t]+thanks|"
            r"thanks[ \t]+and[ \t]+regards|sincerely|yours[ \t]+(?:sincerely|faithfully|truly)|с[ \t]+уважением|"
            r"всего[ \t]+доброго|всего[ \t]+хорошего|всего[ \t]+наилучшего|с[ \t]+наилучшими[ \t]+пожеланиями|удачи|"
            r"хорошего[ \t]+(?:дня|вечера)|с[ \t]+благодарностью|искренне[ \t]+ваш(?:а)?|до[ \t]+связи|до[ \t]+встречи|"
            r"заранее[ \t]+спасибо|с[ \t]+уважением[ \t]+и[ \t]+наилучшими[ \t]+пожеланиями|cordialement|"
            r"bien[ \t]+à[ \t]+vous|saludos|un[ \t]+saludo|cordiali[ \t]+saluti)")
_CLOSING_RE = re.compile(
    r"(?im)^[ \t>*_-]*(?:(?:vielen[ \t]+dank|danke|thanks|thank[ \t]+you|спасибо)[ \t]*(?:und|and|,|и)?[ \t]*)?" + _CLOSING +
    r"(?![^\W\d_])(?:[ \t]*[/|,-][ \t]*" + _CLOSING + r"(?![^\W\d_]))?"
    r"(?:[ \t]+(?:aus|from|nach|из)[ \t]+(?P<place>[^\W\d_][\w'’-]*(?:[ \t]+[^\W\d_][\w'’-]*){0,2}))?"
    r"[ \t]*[,.!:]*[ \t]*(?P<same>[^\n]{0,80}?)[ \t]*$")
_SIGNER_PREFIX_RE = re.compile(r"(?i)^[ \t>*_-]*(?:i\.[ \t]?A\.|i\.[ \t]?V\.|ppa\.|gez\.|--|—)?[ \t]*")


def _signoff_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for m in _CLOSING_RE.finditer(text):
        if m.group("place"):
            a, b = m.start("place"), m.end("place")
            if not blocked.hit(a, b) and not all(_known_word(w) for w in text[a:b].split()):
                out.append((a, b, "place"))
        same = m.group("same")
        if same and same.strip():
            # the signer on the closing's line: capitalised words only (LG Tobias), never "und ein schönes Wochenende"
            if all(w[:1].isupper() or w.casefold() in _PARTICLE_WORDS for w in same.split()) and len(same.split()) <= 4:
                span = _person_value(text, m.start("same"), m.end("same"), blocked, allow_lower=False, max_words=4)
                if span and not any(c.isdigit() for c in text[span[0]:span[1]]):
                    out.append((span[0], span[1], "person"))
                continue
            if not re.search(r"[^\W\d_]", same.strip(" ,.!")):
                pass
            else:
                continue
        pos = m.end()
        seen = 0
        while pos < len(text) and seen < 4:
            nl = text.find("\n", pos)
            line_end = len(text) if nl < 0 else nl
            line = text[pos:line_end]
            nxt = line_end + 1
            if line.strip(" \t>*_-—") == "":
                pos = nxt
                seen += 1
                continue
            if re.fullmatch(r"[ \t>*_-]*\[(?:person|company|place|name)(?: \d+)?\][ \t,.]*", line):
                break   # the signer was wiped in an earlier pass; the role line below it stays
            seen += 1
            p = _SIGNER_PREFIX_RE.match(line)
            start = pos + (p.end() if p else 0)
            cut = len(line)
            for sep in ("|", ",", " / ", " - ", " – ", "(", "<", "@"):
                i = line.find(sep, p.end() if p else 0)
                if i >= 0:
                    cut = min(cut, i)
            # a company line (a legal form or a registered form) is not the signer: look one line further
            if _COMPANY_RE.search(line):
                pos = nxt
                continue
            line_words = text[start:pos + cut].split()
            lower_line = bool(line_words) and line_words[0][:1].islower()
            if lower_line and not (len(line_words) <= 2 and line_words[0].casefold().strip(".,") in _FIRST_NAMES):
                break   # "stage two of the plan": prose after a closing, no signer
            span = _person_value(text, start, pos + cut, blocked, allow_lower=lower_line, max_words=4)
            if span and not any(w.casefold().strip(".,") in _TITLE_WORDS for w in line_words) \
                    and all(_person_known(w) or _generic_word(w) or _org_word(w) for w in text[span[0]:span[1]].split()):
                span = None   # Das Projektteam, TCP Architekturteam: a team signs, not a person
            if span and not any(c.isdigit() for c in text[span[0]:span[1]]):
                out.append((span[0], span[1], "person"))
                break
            if blocked.hit(start, pos + cut):
                pos = nxt
                continue
            break
    return out


_HEADER_RE = re.compile(
    r"(?im)^[ \t>*_]*(?:from|von|to|an|cc|bcc|sender|absender|reply-to|antwort[ \t]+an|от[ \t]+кого|от|кому|копия|"
    r"скрытая[ \t]+копия|получатель|адресат|отправитель)"
    r"[ \t]*:[ \t]*[*_]*[ \t]*(?P<v>.+?)[ \t]*$(?P<cont>(?:\n[ \t]+[^\n]*)*"
    r"(?:\n(?![ \t>*_]*(?:from|von|to|an|cc|bcc|gesendet|sent|betreff|subject|date|datum|тема|дата)[ \t]*:)"
    r"[ \t]*[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,2}[ \t]*$)?)")
_ON_BEHALF_RE = re.compile(r"(?i)[ \t]+(?:im[ \t]+auftrag[ \t]+von|on[ \t]+behalf[ \t]+of|via)[ \t]+")
_ADDRESS_IN_HEADER_RE = re.compile(r"<[^<>\n]*>|(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INVERTED_ITEM_RE = re.compile(r"^\s*([^\W\d_][\w'’-]*)\s*,\s*([^\W\d_][\w'’-]*(?:\s+[^\W\d_]\.)?)\s*$")


def _header_items(value: str, offset: int) -> list[tuple[int, int]]:
    """(start, end) of every display name of a header value, positions in the text."""
    items: list[tuple[int, int]] = []
    pieces: list[tuple[int, int]] = []
    if _ADDRESS_IN_HEADER_RE.search(value):
        last = 0
        for m in _ADDRESS_IN_HEADER_RE.finditer(value):
            pieces.append((last, m.start()))
            last = m.end()
            # a separator after the address
            while last < len(value) and value[last] in ",; \t":
                last += 1
        if last < len(value):
            pieces.append((last, len(value)))
    else:
        parts = value.split(";") if ";" in value else [value]
        pos = 0
        for part in parts:
            a, b = pos, pos + len(part)
            pos = b + 1
            if _INVERTED_ITEM_RE.match(part):
                pieces.append((a, b))
            else:
                sub = 0
                for item in part.split(","):
                    pieces.append((a + sub, a + sub + len(item)))
                    sub += len(item) + 1
    for a, b in pieces:
        seg = value[a:b]
        lead = len(seg) - len(seg.lstrip(" \t\"'“”„‚‘’"))
        trail = len(seg) - len(seg.rstrip(" \t\"'“”„‚‘’,;"))
        if b - trail > a + lead:
            items.append((offset + a + lead, offset + b - trail))
    return items


def _header_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for m in _HEADER_RE.finditer(text):
        a0, b0 = m.start("v"), m.end("cont") if m.group("cont") else m.end("v")
        value = text[a0:b0]
        pieces = []
        last = 0
        for sep in _ON_BEHALF_RE.finditer(value):
            pieces.append((last, sep.start()))
            last = sep.end()
        pieces.append((last, len(value)))
        for pa, pb in pieces:
            for a, b in _header_items(value[pa:pb], a0 + pa):
                seg = text[a:b]
                cut = len(seg)
                for sep in ("(", "|", " via "):
                    i = seg.find(sep)
                    if i > 0:
                        cut = min(cut, i)
                seg_end = a + len(seg[:cut].rstrip())
                if seg_end <= a:
                    continue
                words = _value_words(text, a, seg_end)
                if _COMPANY_RE.search(seg[:cut]) or any(w.casefold() in _LEGAL_WORDS for w in seg[:cut].split()):
                    if words and not any(blocked.hit(w[0], w[1]) for w in words):
                        out.append((words[0][0], words[-1][1], "company"))
                    continue
                # a notification service as the sender (Elastic Cloud Server Alerts) is no person: every word known
                if words and all(_person_known(w[2]) for w in words):
                    continue
                span = _person_value(text, a, seg_end, blocked, allow_lower=True, max_words=4)
                if span:
                    out.append((span[0], span[1], "person"))
    return out


_NAME_RUN = r"(?:[^\W\d_][\w'’.-]*[ \t]+){0,3}[^\W\d_][\w'’.-]*"
_VERB = r"(?:schrieb|schreibt|wrote|writes|написал(?:а)?|пишет|писал(?:а)?|a[ \t]+écrit|escribió|ha[ \t]+scritto)"
_REPLY_DE_RE = re.compile(r"(?im)^[ \t>]*am[ \t]+.{4,60}?[ \t]+schrieb[ \t]+(?P<v>[^<:\n]{2,80}?)[ \t]*(?:<[^>\n]*>)?[ \t]*:?[ \t]*$")
_REPLY_EN_RE = re.compile(r"(?im)^[ \t>]*on[ \t]+.{4,80}?[ \t](?P<v>" + _NAME_RUN + r")[ \t]*(?:<[^>\n]*>)?[ \t\n>]+wrote:[ \t]*$")
_REPLY_RU_RE = re.compile(r"(?im)^[ \t>]*(?:.{4,80}?[ \t])?(?P<v>" + _NAME_RUN + r")[ \t]*(?:<[^>\n]*>)?[ \t]+(?:написал(?:а)?|пишет|писал(?:а)?)[ \t]*(?:\(а\))?:[ \t]*$")
_QUOTE_HEAD_RE = re.compile(r"(?im)^[ \t>]*(?P<v>" + _NAME_RUN + r")[ \t]*(?:<[^>\n]*>)?[ \t]+" + _VERB + r"(?:[ \t]+[^\n]{0,80})?:[ \t]*$")
_REPLY_NOVERB_RE = re.compile(r"(?im)^[ \t>]*(?:[^\n<]{4,80}?,[ \t]*)(?P<v>" + _NAME_RUN + r")[ \t]*<[^>\n]*>[ \t]*:[ \t]*$")


def _reply_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for rx in (_REPLY_DE_RE, _REPLY_EN_RE, _REPLY_RU_RE, _QUOTE_HEAD_RE, _REPLY_NOVERB_RE):
        for m in rx.finditer(text):
            a, b = m.start("v"), m.end("v")
            words = _value_words(text, a, b)
            # the date and time words in front of the name are known words or digits
            while words and (_known_word(words[0][2]) and words[0][2].casefold() not in _FIRST_NAMES
                             or words[0][2].casefold() in ("am", "pm", "uhr", "um", "at", "г")):
                words = words[1:]
            if not words:
                continue
            span = _person_value(text, words[0][0], b, blocked, allow_lower=True, max_words=4)
            if span:
                out.append((span[0], span[1], "person"))
    return out


_NAME_SHAPE_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?")
"""A word of a name: letters of any script, an inner hyphen or apostrophe, an initial's dot."""
_CAMEL_RE = re.compile(r"[a-zäöüß][A-ZÄÖÜ]")


def _name_shaped(w: str) -> bool:
    """Letters with an inner hyphen or apostrophe, no inner capital after a small letter (RegisterError, vSphere)."""
    return bool(_NAME_SHAPE_RE.fullmatch(w)) and not _CAMEL_RE.search(w)
_STAMP = r"(?:\[?\(?(?:[\d./-]{6,10}[,\s]+)?\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*(?:AM|PM|am|pm))?\)?\]?[ \t]*[-–]?[ \t]*)"
_SPEAKER_RE = re.compile(
    r"(?m)^[ \t>*_-]*" + _STAMP + r"?(?P<label>[^\s:\[\]|<>(){}\"'#*_-][^:\n\[\]|<>{}]{0,40}?)"
    r"(?:[ \t]*\((?P<paren>[^()\n]{1,30})\))?[*_]*[ \t]*(?:\(\d{1,2}:\d{2}(?::\d{2})?\))?[ \t]*:[*_]*(?:[ \t]+(?=\S)|[ \t]*$)")
_SPEAKER_PIPE_RE = re.compile(r"(?m)^[ \t>*_-]*" + _STAMP + r"?(?P<label>[^\s:\[\]|<>(){}\"'#*_-][^:\n\[\]|<>{}]{0,40}?)[ \t]*\|[ \t]*(?P<company>[^:\n|]{1,40}?)[ \t]*:[ \t]+(?=\S)")
_STAMP_ONLY_RE = re.compile(r"(?m)^[ \t>*_-]*" + _STAMP + r"(?P<label>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,3})[ \t]*(?:\([^()\n]{1,30}\))?[ \t]*$")
_TEAMS_RE = re.compile(r"(?m)^[ \t]*(?P<label>[^\n\t]{2,60}?)(?:[ \t]{2,}|\t+)\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*(?:AM|PM|am|pm))?[ \t]*$")
_ZOOM_RE = re.compile(r"(?m)^[ \t]*\d{1,2}:\d{2}(?::\d{2})?[ \t]+From[ \t]+(?P<label>[^\n:]{1,60}?)[ \t]+to[ \t]+[^\n:]{1,40}?[ \t]*:")
_QA_PAREN_RE = re.compile(r"(?m)^[ \t>*_-]*(?:[A-Za-z]{1,2}|Frage|Antwort|Question|Answer)[ \t]*\((?P<label>[^()\n]{2,40})\)[ \t]*:")
_LEGEND_RE = re.compile(r"(?im)^[ \t>*_-]*(?:speaker|sprecher|teilnehmer|participant)[ \t]*\d{1,2}[ \t]*[=:][ \t]*(?P<label>[^\n,;(]{2,60})")
_NOUN_VON_RE = re.compile(r"(?m)^[ \t>*_-]*(?:[A-ZÄÖÜ][\wäöüß-]+)[ \t]+(?:von|from|by|of)[ \t]+(?P<label>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,2})[ \t]*:")
_VTT_V_RE = re.compile(r"<v[ \t]+(?P<label>[^>\n]{1,40})>")
_LABEL_AFTER_VON_RE = re.compile(r"[ \t]+(?:von|van|de|vom|aus|from)[ \t]+(?P<company>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,2})[ \t]*$")


def _speaker_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    counts: Counter = Counter()
    speech: dict[str, bool] = {}
    fields: set[str] = set()
    found: list[tuple[int, int, str, str]] = []   # (start, end, key, company after von or "")
    for m in _SPEAKER_RE.finditer(text):
        label = m.group("label").strip()
        company = ""
        von = _LABEL_AFTER_VON_RE.search(label)
        if von:
            company = von.group("company")
            label = label[:von.start()].strip()
        key = " ".join(label.split()).casefold()
        if not label or key in _NEUTRAL_LABELS or any(c.isdigit() for c in label) or len(key) < 2:
            continue
        words = label.split()
        if not 1 <= len(words) <= 4 or len(label) > 40:
            continue
        if not all(_name_shaped(w) for w in words):
            continue   # "def check(text", "`check`", "Places (x)", "class VaultError" are code, never a speaker
        if len(words) >= 2 and not all(w[:1].isupper() or w.casefold() in _PARTICLE_WORDS for w in words):
            continue   # "check socket ops:", "file alone:" are fragments of prose, not a speaker
        if key.endswith(("s",)) and key[:-1] in _NEUTRAL_LABELS:
            continue
        rest = [w for w in words if w.casefold().strip(".") not in _TITLE_WORDS and not _role_word(w)]
        if not rest or len(rest) > 3:
            continue
        lead = len(rest) < len(words) and words[0] not in rest   # Architekt Zrbl: the role stands before the name
        rkey = " ".join(rest).casefold()
        if rkey in _NEUTRAL_LABELS or rkey in _COMPANY_LABELS:
            continue
        if any(w.casefold() in _PARTICLE_WORDS for w in rest) and not any(w.casefold() in _FIRST_NAMES for w in rest):
            counts[rkey] -= 1   # "Breite der Anbindung:" is a field; a name with a particle must repeat to count
        # a speaker is named by words nobody knows, or by a first name; "file id", "Note", "Status" are labels
        if len(rest) == 1 and _person_known(rest[0]) and not (rest[0].isupper() and len(rest[0]) <= 3):
            continue
        if len(rest) == 1 and rest[0].isupper() and len(rest[0]) <= 3:
            ikey = "initials:" + rest[0]
            counts[ikey] += 1
            line_end = text.find("\n", m.end())
            speech[ikey] = speech.get(ikey, False) or _speech_shaped(text[m.end():line_end if line_end >= 0 else len(text)])
            a = m.start("label") + m.group("label").find(rest[0])
            found.append((a, a + len(rest[0]), ikey, company))
            continue
        if len(rest) >= 2 and all(_person_known(w) for w in rest):
            continue
        unknown = [w for w in rest if not _person_known(w) and w.casefold() not in _FIRST_NAMES]
        if len(unknown) == 1 and not lead and not any(w.casefold() in _FIRST_NAMES for w in rest):
            # Zammad:, Bareos Director:, Bareos Storage Daemon: one unknown word, the rest product words or a
            # trailing role word: a speaker only when the label speaks somewhere, else a field of a list
            line_end = text.find("\n", m.end())
            value = text[m.end():line_end if line_end >= 0 else len(text)]
            speech[rkey] = speech.get(rkey, False) or _speech_shaped(value)
            fields.add(rkey)
        counts[rkey] += 1
        if lead:
            counts[rkey] += 1   # a title or a role before the name is a strong signal (Architekt Zrbl:)
        a = m.start("label") + m.group("label").find(rest[0])
        found.append((a, a + len(" ".join(rest)), rkey, company))
    out = []
    for a, b, key, company in found:
        words = key.split()
        if key.startswith("initials:"):
            if counts[key] >= 2 and not blocked.hit(a, b) and (not _known_word(key[9:]) or speech.get(key)):
                out.append((a, b, "person"))   # "TB:" that repeats and speaks is a person's initials, not a unit
            continue
        strong = len(words) >= 2 or counts[key] >= 2 or key in _FIRST_NAMES
        if key in fields and not speech.get(key):
            continue   # a product or a field name: its values are data, nobody speaks
        if strong and not blocked.hit(a, b):
            out.append((a, b, "person"))
            if company and not all(_known_word(w) for w in company.split()):
                i = text.find(company, b, b + 60)
                if i >= 0 and not blocked.hit(i, i + len(company)):
                    out.append((i, i + len(company), "company"))
    for m in _SPEAKER_PIPE_RE.finditer(text):
        span = _person_value(text, m.start("label"), m.end("label"), blocked, allow_lower=True, max_words=4)
        if not span:
            continue
        out.append((span[0], span[1], "person"))
        a, b = m.start("company"), m.end("company")
        if not blocked.hit(a, b) and not all(_known_word(w) for w in text[a:b].split()):
            out.append((a, b, "company"))
    for rx in (_STAMP_ONLY_RE, _TEAMS_RE, _ZOOM_RE, _QA_PAREN_RE, _LEGEND_RE, _NOUN_VON_RE, _VTT_V_RE):
        for m in rx.finditer(text):
            span = _person_value(text, m.start("label"), m.end("label"), blocked, allow_lower=True, max_words=4,
                                 vocabulary=(rx is _STAMP_ONLY_RE or rx is _NOUN_VON_RE))
            if span:
                out.append((span[0], span[1], "person"))
    return out


def _role_word(w: str) -> bool:
    low = w.casefold().strip(".,;:")
    return low in _ROLE_WORDS or _role_shape(low)


def _proposed_name_shape(word: str) -> bool:
    """A single capitalised word that is not known could be a first name (a speaker label)."""
    return word[:1].isupper() and not _known_word(word)


_PERSON_LABELS = (
    "name", "vorname", "nachname", "familienname", "ansprechpartner", "ansprechpartnerin", "kontakt", "contact",
    "contact person", "autor", "author", "authors", "verfasser", "ersteller", "erstellt von", "prepared by",
    "bearbeiter", "bearbeiterin", "sachbearbeiter", "sachbearbeiterin", "projektleiter", "projektleiterin",
    "project lead", "project manager", "owner", "besitzer", "verantwortlich", "verantwortlicher", "responsible",
    "teilnehmer", "teilnehmende", "participants", "attendees", "anwesend", "interviewer", "interviewee", "speaker",
    "sprecher", "presenter", "referent", "trainer", "dozent", "approved by", "freigegeben von", "geprüft von",
    "reviewed by", "signed", "unterschrift", "signature", "имя", "фамилия", "фио", "автор", "контакт",
    "ответственный", "участники", "докладчик", "manager", "lead", "reporter", "assignee", "requester", "requested by",
    "submitted by", "created by", "modified by", "last modified by", "geändert von", "zuletzt geändert von",
    "генеральный директор", "главный бухгалтер", "технический директор", "коммерческий директор", "финансовый директор",
    "исполнительный директор", "директор", "руководитель проекта", "руководитель", "начальник отдела", "начальник",
    "ответственный исполнитель", "контактное лицо", "подписант", "от исполнителя", "от заказчика", "представитель",
    "gesprächspartner", "ansprechperson", "ansprechpersonen", "kontaktperson", "key account", "account manager",
    "technischer ansprechpartner", "kaufmännischer ansprechpartner", "geschäftsführer", "geschäftsführerin",
    "geschäftsführung", "vorstand", "vorstandsvorsitzender", "vorstandsvorsitzende", "aufsichtsratsvorsitzender",
    "inhaber", "inhaberin", "managing director", "managing directors", "ceo", "cto", "cio", "cfo", "director",
    "directors", "real_name", "full_name", "display_name", "user_name", "username", "author_name", "owner_name",
    "contact_name", "fullname", "realname", "displayname", "sender_name", "from_name", "to_name", "cn",
    "unterzeichner", "unterzeichnet", "gez", "sachbearbeitung", "bearbeitung", "ihr ansprechpartner",
    "ihre ansprechpartnerin", "ihr gesprächspartner", "zuständig", "betreuer", "betreuerin", "kundenbetreuer",
    "kundenbetreuerin", "vertriebsbeauftragter", "vertriebsmitarbeiter", "berater", "beraterin",
    "first name", "last name", "surname", "given name", "family name", "middle name", "vor- und nachname",
    "vorname nachname", "vorname und nachname", "vor- und zuname", "zuname", "full name", "fullname", "person",
    "personen", "people", "mitarbeiter", "mitarbeiterin", "mitarbeitende", "kollege", "kollegin", "kollegen",
    "accountable", "consulted", "informed", "отчество", "patronymic", "сотрудник", "сотрудники", "участник",
    "ответственное лицо", "контактное лицо", "руководитель", "менеджер", "инженер", "architect", "engineer",
    "last_modified_by", "lastmodifiedby", "modified_by", "modifiedby", "created_by", "createdby", "maintainer",
    "maintainers", "maintained by", "initial-creator", "initial_creator", "creator", "comment author",
    "last modified by", "author_name", "ownername", "owner_name", "nick", "nickname", "handle", "login", "user",
    "ansible_user", "remote_user", "become_user", "ssh_user", "db_user", "git_user", "sender", "recipient",
    "empfänger", "absender", "vorgelegt von", "genehmigt von", "erstellt von", "verfasst von", "präsentiert von",
)
_LOGIN_LABELS = frozenset(("name", "login", "user", "username", "user_name", "nick", "nickname", "handle", "display_name",
                           "displayname", "real_name", "realname", "ansible_user", "remote_user", "become_user",
                           "ssh_user", "db_user", "git_user", "owner", "assignee", "reporter", "maintainer",
                           "created_by", "createdby", "modified_by", "modifiedby", "last_modified_by"))
"""Labels whose value may be a login in lower case (tpomblet, t.pomblet): one token of letters, dots and digits."""
_COMPANY_LABELS = (
    "firma", "company", "unternehmen", "organisation", "organization", "kunde", "kundin", "customer", "client",
    "auftraggeber", "auftraggeberin", "auftragnehmer", "mandant", "mandantin", "partner", "lieferant", "supplier",
    "vendor", "hersteller", "anbieter", "dienstleister", "reseller", "arbeitgeber", "employer", "компания",
    "заказчик", "клиент", "организация", "фирма", "партнёр", "партнер", "поставщик", "исполнитель", "подрядчик",
    "субподрядчик", "покупатель", "продавец", "арендатор", "арендодатель", "лицензиар", "лицензиат", "сторона",
    "стороны", "контрагент", "оператор", "провайдер", "интегратор", "вендор", "производитель",
)
_HOST_LABELS = ("hostname", "host", "hosts", "server", "servername", "fqdn", "node", "nodename", "vm", "instance",
                "rechner", "maschine", "hostnames", "servers", "nodes")
_TECH_HEADERS = frozenset(("ip", "typ", "type", "host", "hostname", "port", "os", "version", "status", "cpu", "ram",
                           "disk", "size", "größe", "flavor", "image", "region", "zone", "az", "vpc", "subnet", "mac",
                           "id", "uuid", "tag", "tags", "env", "environment", "umgebung", "cluster", "namespace", "pod",
                           "service", "dienst", "anwendung", "application", "app", "url", "endpoint", "path", "pfad",
                           "kategorie", "category", "preis", "price", "kosten", "cost", "menge", "quantity", "anzahl",
                           "modell", "model", "hersteller", "vendor", "lizenz", "license", "ablauf", "expiry"))
"""Headers that make a table technical: a Name column next to them holds resources, not people."""
_PLACE_LABELS = ("site", "sites", "datacenter", "data center", "rechenzentrum", "rz", "campus", "werk", "filiale",
                 "region", "ort", "standort", "city", "stadt", "location", "adresse", "address", "anschrift", "sitz",
                 "sitz der gesellschaft", "registergericht", "amtsgericht", "handelsregister", "firmensitz",
                 "hauptsitz", "niederlassung", "büro", "office", "lieferadresse", "rechnungsadresse", "postanschrift",
                 "город", "адрес", "место", "юридический адрес")
_LABEL_VALUE_RE = re.compile(
    r"(?im)(?<![A-Za-zÀ-ÿА-Яа-я-])_*(?P<label>%s)_*[\"']?[ \t]*[:=][ \t]*(?P<q>[\"']?)(?P<v>[^\n|\"]{1,160})" % "|".join(
        re.escape(l) for l in sorted(_PERSON_LABELS + _COMPANY_LABELS + _PLACE_LABELS, key=len, reverse=True)))
_ITEM_SPLIT_RE = re.compile(r"[ \t]*(?:;|,|/|&| und | and | и |\+)[ \t]*")


def _label_class(label: str) -> str:
    low = label.casefold()
    if low in _PERSON_LABELS:
        return "person"
    if low in _COMPANY_LABELS:
        return "company"
    return "place"


_WEAK_PERSON_LABELS = frozenset(("name", "owner", "besitzer", "manager", "lead", "contact", "kontakt", "reporter",
                                 "assignee", "requester", "responsible", "verantwortlich", "verantwortlicher",
                                 "key account", "account manager", "speaker", "sprecher", "presenter", "signed"))


_LOGIN_RE = re.compile(r"^[ \t]*(?P<v>[a-z][a-z0-9]{2,}(?:[._][a-z0-9]{2,}){0,2})[ \t]*$")
_SYSTEM_USERS = frozenset(("root", "admin", "administrator", "ubuntu", "ec2-user", "centos", "debian", "nginx", "www-data",
                           "apache", "postgres", "mysql", "redis", "docker", "jenkins", "gitlab", "git", "ansible",
                           "terraform", "vagrant", "nobody", "daemon", "sshd", "systemd", "nobody", "guest", "test",
                           "service", "svc", "app", "application", "backup", "monitoring", "deploy", "deployer",
                           "operator", "support", "unassigned", "none", "null", "true", "false", "default", "system",
                           "localhost", "server", "client", "user", "users", "bot", "cron", "mail", "postfix", "ftp",
                           "tomcat", "elastic", "kibana", "grafana", "prometheus", "node", "nodejs", "python", "java",
                           "netops", "devops", "secops", "dataops", "mlops", "finops", "gitops", "infra", "platform",
                           "network", "security", "dba", "dbas", "sre", "ops", "it", "itsm", "helpdesk", "servicedesk",
                           "cloudops", "team", "teams", "group", "groups", "squad", "tribe", "staff", "sales", "hr",
                           "finance", "legal", "marketing", "product", "engineering", "research", "qa", "test", "tester",
                           "automation", "pipeline", "ci", "cd", "build", "release", "stage", "staging", "prod", "dev",
                           "oncall", "pager", "alerts", "alert", "noreply", "info", "contact", "office", "noc", "soc"))


def _login_value(item: str) -> tuple[int, int] | None:
    """A lower-case login of letters, digits, dots and underscores (tpomblet, t.pomblet, pomblet_t) that is no
    system user and no known word: (start, end) inside `item`."""
    m = _LOGIN_RE.match(item)
    if not m:
        return None
    v = m.group("v")
    parts = [x for x in re.split(r"[._]", v) if x]
    if v in _SYSTEM_USERS or all(x in _SYSTEM_USERS or (x.isalpha() and _known_word(x)) or x.isdigit() for x in parts):
        return None
    if not any(sum(c.isalpha() for c in x) >= 4 for x in parts):
        return None
    return m.start("v"), m.end("v")


def _classed_items(text: str, a: int, b: int, cls: str, blocked, weak: bool = False,
                   quoted: bool = False, login_ok: bool = False) -> list[tuple[int, int, str]]:
    """Each item of a value (split at commas, semicolons, slashes, and, und) as a candidate of `cls`. A weak person
    label (Name:, Owner: ...) takes one word only when it is a first name or an unknown name-shaped word; a company
    value starts with a capital, has at most six words and no sentence in it."""
    out = []
    value = text[a:b]
    pos = 0
    for item in _ITEM_SPLIT_RE.split(value):
        start = value.find(item, pos)
        if start < 0:
            continue
        pos = start + len(item)
        if not item.strip():
            continue
        cut = re.search(r"[<(\[]", item)
        paren = None
        if cut:
            inner = re.match(r"\(([^()\n]{2,60})\)", item[cut.start():])
            if inner and cls == "person":
                paren = (a + start + cut.start() + 1, a + start + cut.start() + 1 + len(inner.group(1)))
            item = item[:cut.start()].rstrip()   # Author: First Last <mail>, Ansprechpartner: X (Company GmbH)
            if not item.strip():
                continue
        if re.search(r"[=>)\]{}`]|->", item):
            continue
        if cls == "person":
            login = _login_value(item) if (login_ok or quoted) else None
            if login is not None:
                span = (a + start + login[0], a + start + login[1])
            else:
                span = _person_value(text, a + start, a + start + len(item), blocked, allow_lower=False, max_words=4,
                                     vocabulary=True)
            if span and not login and re.search(r"[-_\d]", text[span[0]:span[1]]) \
                    and not all(w[:1].isupper() for w in re.split(r"[-\s]", text[span[0]:span[1]])):
                span = None   # svc-nrgtz-prod is an identifier; Müller-Lüdenscheidt a name
            if span and not login and weak and len(_value_words(text, span[0], span[1])) < 2:
                w = text[span[0]:span[1]]
                if not (w.casefold() in _FIRST_NAMES or (_name_shaped(w) and not _known_word(w))):
                    span = None
        else:
            words = _value_words(text, a + start, a + start + len(item))
            if not words or len(words) > 6 or any(blocked.hit(w[0], w[1]) for w in words):
                span = None
            elif not words[0][2][:1].isupper():
                span = None
            elif cls == "company" and len(words) == 1 and (words[0][2].casefold() in _GENERIC_COMPANY_WORDS
                                                           or words[0][2].casefold() in _ROLE_WORDS
                                                           or _known_word(words[0][2])):
                span = None   # "Partner", "intern", "Hersteller" under a company header are no company
            elif cls == "company" and (all(_known_word(w[2]) and w[2].casefold() not in _GENERIC_COMPANY_WORDS
                                           for w in words)
                                       or sum(1 for w in words if not w[2][:1].isupper()
                                              and w[2].casefold() not in ("und", "and", "&", "von", "de", "of", "for",
                                                                          "the", "la", "le", "del", "der", "des", "di",
                                                                          "en", "e", "y")) >= 1):
                span = None
            elif cls == "place" and all(_known_word(w[2]) for w in words):
                span = None
            else:
                if cls == "place":
                    while len(words) > 1 and _known_word(words[0][2]):
                        words = words[1:]   # Amtsgericht Musterhausen: the court word stays
                span = (words[0][0], words[-1][1])
        if span:
            out.append((span[0], span[1], cls))
            if paren and not blocked.hit(*paren):
                pw = text[paren[0]:paren[1]].split()
                if pw and not all(_known_word(w) for w in pw) and not any(codes.is_code(w) for w in pw):
                    out.append((paren[0], paren[1], "company"))   # Teilnehmer: Surname (Company)
    return out


def _dialogue_labels(text: str) -> set[str]:
    """Labels that open three lines or more are the roles of a dialogue (Kunde:, Berater:, Partner:), not fields."""
    counts: Counter = Counter()
    for m in re.finditer(r"(?m)^[ \t>*_-]*([^\W\d_][\w'’ -]{0,30}?)[ \t]*:[ \t]+\S", text):
        counts[m.group(1).strip().casefold()] += 1
    return {k for k, n in counts.items() if n >= 3}


def _label_value_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    dialogue = _dialogue_labels(text)
    for m in _LABEL_VALUE_RE.finditer(text):
        label = m.group("label").casefold()
        cls = _label_class(m.group("label"))
        if cls != "person" and label in dialogue:
            continue
        value = m.group("v")
        # the value ends at the next label on the same line ("Name: X Firma: Y")
        nxt = _LABEL_VALUE_RE.search(value, 1)
        b = m.start("v") + (nxt.start() if nxt else len(value))
        out.extend(_classed_items(text, m.start("v"), b, cls, blocked, weak=label in _WEAK_PERSON_LABELS,
                                  quoted=bool(m.group("q")) and label in _LOGIN_LABELS, login_ok=label in _LOGIN_LABELS))
    return out


_TABLE_ROW_RE = re.compile(r"(?m)^[ \t]*\|(.*)\|[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t:]*-{2,}[ \t:]*(?:\|[ \t:]*-{2,}[ \t:]*)*\|?[ \t]*$")


def _table_cells(line_start: int, body: str) -> list[tuple[int, int]]:
    """(start, end) in the text of every cell of one Markdown row body (between its outer pipes)."""
    cells = []
    pos = 0
    for part in body.split("|"):
        a = line_start + pos
        b = a + len(part)
        pos += len(part) + 1
        lead = len(part) - len(part.lstrip())
        trail = len(part) - len(part.rstrip())
        if b - trail > a + lead:
            cells.append((a + lead, b - trail))
    return cells


_PIPE_ROW_RE = re.compile(r"(?m)^[ \t]*\|?(?P<body>[^\n|]*(?:\|[^\n|]*)+?)\|?[ \t]*$")
_LEGAL_ONLY_RE = re.compile(r"^(?:%s)$" % _FORM_ALT)


def _row_cells(text: str, start: int, line: str) -> list[tuple[int, int]] | None:
    """The cells of a pipe row, with or without outer pipes; None for a line without a pipe."""
    if "|" not in line or _TABLE_SEP_RE.match(line):
        return None
    body = line
    lead = 0
    stripped = line.lstrip()
    if stripped.startswith("|"):
        lead = len(line) - len(stripped) + 1
        body = stripped[1:]
    if body.rstrip().endswith("|"):
        body = body.rstrip()[:-1]
    return _table_cells(start + lead, body)


def _header_class(label: str) -> str | None:
    label = " ".join(label.split()).casefold().strip("*_ :")
    if label in _PERSON_LABELS + _COMPANY_LABELS + _PLACE_LABELS:
        return _label_class(label)
    if label in _HOST_LABELS:
        return "host"
    first = label.split()[0] if label else ""
    if first in _PERSON_LABELS or first in _COMPANY_LABELS or first in _PLACE_LABELS:
        return _label_class(first)   # "Ansprechpartner Kunde", "Ansprechpartner technisch"
    return None


_DASH_LINE_RE = re.compile(r"(?m)^[ \t]*(?:-{3,}[ \t]+)+-{3,}[ \t]*$|^[ \t]*-{6,}[ \t]*$")


def _fixed_table_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A fixed-width text table (pandoc, a console listing): the line over a dashed line is the header, the words
    of a column with a person, company or place header are values; columns are separated by two spaces or more."""
    out = []
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    for i, line in enumerate(lines):
        if not _DASH_LINE_RE.match(line) or i == 0:
            continue
        header = lines[i - 1]
        cols = [(m.start(), m.end()) for m in re.finditer(r"\S+(?:[ \t]\S+)*", header)]
        if len(cols) < 2:
            continue
        classes = [_header_class(header[a:b]) for a, b in cols]
        if not any(classes):
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() and not _DASH_LINE_RE.match(lines[j]):
            row = lines[j]
            for (a, b), cls in zip(cols, classes):
                if not cls or cls == "host":
                    continue
                # the cell under the header column: from this column start to the next column start
                k = cols.index((a, b))
                cb = cols[k + 1][0] if k + 1 < len(cols) else len(row)
                cell = row[a:cb] if a < len(row) else ""
                if cell.strip():
                    x = starts[j] + a + (len(cell) - len(cell.lstrip()))
                    y = starts[j] + a + len(cell.rstrip())
                    out.extend(_classed_items(text, x, y, cls, blocked, weak=False))
            j += 1
    return out


def _table_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Cells of a table column whose header is a person, company, place or host label; a cell that holds only a
    legal form makes the cell before it a company; a two-column table of label cells classes its second column.
    Tables with or without outer pipes, with a separator line or two rows of the same width, a caption row above
    the header."""
    out = []
    lines = text.split("\n")
    starts = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    rows = [(_row_cells(text, starts[i], lines[i]), _TABLE_SEP_RE.match(lines[i]) is not None) for i in range(len(lines))]
    # the key-value pass: label cells in the first column class the second column (never the header row)
    all_labels = _PERSON_LABELS + _COMPANY_LABELS + _PLACE_LABELS
    for i, (cells, sep) in enumerate(rows):
        if not cells or len(cells) < 2 or sep:
            continue
        if i + 1 < len(rows) and rows[i + 1][1]:
            continue   # the header row of a table
        label = " ".join(text[cells[0][0]:cells[0][1]].split()).casefold().strip("*_ :")
        second = " ".join(text[cells[1][0]:cells[1][1]].split()).casefold().strip("*_ :")
        if label in all_labels and second not in all_labels:
            out.extend(_classed_items(text, cells[1][0], cells[1][1], _label_class(label), blocked,
                                      weak=label in _WEAK_PERSON_LABELS, login_ok=label in _LOGIN_LABELS))
    # the column pass
    i = 0
    while i < len(lines):
        cells, sep = rows[i]
        if not cells or sep:
            i += 1
            continue
        # a table starts here when a separator or a row of the same width follows
        nxt_sep = i + 1 < len(rows) and rows[i + 1][1]
        same_width = i + 1 < len(rows) and rows[i + 1][0] is not None and len(rows[i + 1][0]) == len(cells)
        if not (nxt_sep or same_width):
            i += 1
            continue
        if text[cells[0][0]:cells[0][1]].strip().endswith(":"):
            i += 1
            continue   # "Owner: | value": a key-value row, the key-value pass has it, never a header
        header_i = i
        header = cells
        j = i + 2 if nxt_sep else i + 1
        # a caption row (one non-empty cell) or a header without any label: try the next row as the header
        if (len(header) == 1 or not any(_header_class(text[a:b]) for a, b in header)) and j < len(rows) \
                and rows[j][0] and len(rows[j][0]) >= 2 and any(_header_class(text[a:b]) for a, b in rows[j][0]):
            header_i, header = j, rows[j][0]
            j = j + 2 if (j + 1 < len(rows) and rows[j + 1][1]) else j + 1
        if any(_LEGAL_ONLY_RE.match(text[a:b].strip()) for a, b in header):
            j = header_i   # "| Brand | GmbH |" as the first row: a table without a header, every row is data
            header = []
        labels = [" ".join(text[a:b].split()).casefold().strip("*_ :") for a, b in header]
        classes = [_header_class(text[a:b]) for a, b in header]
        technical = any(l in _TECH_HEADERS for l in labels)
        # a header cell of two or three unknown name-shaped words is a person (a RACI with names as columns)
        for (a, b), cls in zip(header, classes):
            words = text[a:b].split()
            if cls is None and 2 <= len(words) <= 3 and all(_name_shaped(w) and w[:1].isupper() for w in words) \
                    and not any(_person_known(w) for w in words) and not blocked.hit(a, b):
                out.append((a, b, "person"))
        while j < len(rows):
            rc, rsep = rows[j]
            if rc is None:
                break
            if rsep:
                j += 1
                continue
            for k, (a, b) in enumerate(rc):
                cell = text[a:b].strip()
                cls = classes[k] if k < len(classes) else None
                label = labels[k] if k < len(labels) else ""
                # a cell that is only a legal form: the cell before it is the company
                if _LEGAL_ONLY_RE.match(cell) and k >= 1:
                    pa, pb = rc[k - 1]
                    if not blocked.hit(pa, pb) and text[pa:pb].strip() and not _TABLE_SEP_RE.match(text[pa:pb]):
                        out.append((pa, pb, "company"))
                    continue
                if cls == "host":
                    out.extend(_host_piece_spans(text, a, b, blocked))
                elif cls == "person" and technical and label in _WEAK_PERSON_LABELS:
                    if label in _LOGIN_LABELS:
                        lv = _login_value(text[a:b])
                        if lv is not None and not blocked.hit(a + lv[0], a + lv[1]):
                            out.append((a + lv[0], a + lv[1], "person"))   # the owner login of a host inventory
                    continue   # a Name column of hosts or applications next to Typ, IP, Version
                elif cls == "person" and len(cell.split()) == 1 and (_person_known(cell) or _compound_known(cell.casefold())):
                    continue   # Zeiterfassung, Netzwerkbetrieb: a one-word application or department; never by a suffix alone
                elif cls:
                    out.extend(_classed_items(text, a, b, cls, blocked, weak=False, login_ok=label in _LOGIN_LABELS))
            j += 1
        i = j
    return out


_STREET_RE = re.compile(
    r"(?<![\w-])(?P<v>[A-ZÄÖÜ][\wäöüß.-]*(?:[ \t][A-ZÄÖÜ][\wäöüß.-]*){0,2}(?:[ \t-]?)(?:[Ss]traße|[Ss]trasse|[Ss]tr\.|"
    r"weg|[Ww]eg|allee|[Aa]llee|platz|[Pp]latz|gasse|[Gg]asse|ring|[Rr]ing|damm|[Dd]amm|ufer|[Uu]fer|chaussee|"
    r"[Cc]haussee|steig|markt|[Mm]arkt|promenade)[ \t]+\d{1,4}[ \t]?[a-z]?(?:[-/]\d{1,4})?)(?![\w-])")
_STREET2_RE = re.compile(r"(?<![\w-])(?P<v>(?:Am|An der|Auf dem|Im|In der|Zum|Zur)[ \t]+[A-ZÄÖÜ][\wäöüß-]+[ \t]+\d{1,4}[a-z]?)(?![\w-])")
_PLZ_CITY_RE = re.compile(r"(?<![\w.-])(?:D-\d{5}|(?:A-|CH-)\d{4}|\d{5})[ \t]+(?P<v>[^\W\d_a-zäöüß](?:[^\W\d_A-ZÄÖÜ]+|[^\W\d_a-zäöüß]{2,})(?:(?:[ \t](?:am|an|im|der|bei|ob)[ \t]|[ \t-])[^\W\d_a-zäöüß](?:[^\W\d_A-ZÄÖÜ]+|[^\W\d_a-zäöüß]{2,})){0,2})(?![\w-])")
_EN_STREET_RE = re.compile(r"(?<![\w-])(?P<v>\d{1,5}[ \t]+[A-Z][a-z]+(?:[ \t][A-Z][a-z]+){0,2}[ \t]+(?:Street|St\.|Road|Rd\.|Avenue|Ave\.|Lane|Drive|Dr\.|Boulevard|Blvd\.|Square|Way|Place))(?![\w-])")
_RU_STREET_RE = re.compile(r"(?<![\w-])(?<!и[ \t])(?<!т\.[ \t])(?<!т\.)(?P<v>(?:ул\.|улица|пр\.|проспект|пер\.|переулок|наб\.|набережная|пл\.|площадь|б-р|бульвар|ш\.|шоссе)[ \t]*[А-ЯЁ][а-яё-]+(?:[ \t][А-ЯЁ][а-яё-]+){0,2}(?:,?[ \t]*(?:д\.|дом)?[ \t]*\d{1,4}[а-я]?)?)(?![\w-])")
_RU_CITY_RE = re.compile(r"(?<![\w\d-])(?<!\d[ \t])(?<!\d)(?:г\.|город)[ \t]*(?P<v>[А-ЯЁ][а-яё-]+(?:[ \t][А-ЯЁ][а-яё-]+)?)(?![\w-])")
_PLACE_DATE_RE = re.compile(r"(?m)^[ \t]*(?P<v>[^\W\d_a-zäöüß][^\W\d_A-ZÄÖÜ-]+(?:[ \t][^\W\d_][^\W\d_A-ZÄÖÜ-]+){0,2}),[ \t]+(?:den|am|im|il|le|)[ \t]*\d{1,2}\.?[ \t]*(?:[^\W\d_a-zäöüß][^\W\d_A-ZÄÖÜ]+|[а-яё]{3,8}|\d{1,2}\.)[ \t]*\d{2,4}")
_POSTFACH_RE = re.compile(r"(?<![\w-])(?P<v>(?:Postfach|P\.?O\.? Box|PO Box)[ \t]+\d{1,8})(?![\w-])")


def _address_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for rx in (_STREET_RE, _STREET2_RE, _EN_STREET_RE, _RU_STREET_RE, _RU_CITY_RE, _POSTFACH_RE, _PLZ_CITY_RE, _PLACE_DATE_RE):
        for m in rx.finditer(text):
            a, b = m.start("v"), m.end("v")
            if blocked.hit(a, b):
                continue
            if rx is _PLACE_DATE_RE and all(_known_word(w) for w in m.group("v").split()):
                continue
            if rx is _RU_STREET_RE and not re.search(r"\d|,", m.group("v")) and not re.match(r"[ \t]*(?:,|д\.|дом|\d|$)", text[m.end():m.end() + 6]):
                continue   # "и пр. Миграция данных": the abbreviation of "и прочее", no street without a number
            if rx is _STREET2_RE:
                noun = m.group("v").split()[-2]
                if _known_word(noun) and not re.search(r"\b\d{5}\b", text[m.end():m.end() + 80]):
                    continue   # Am Standort 2 laufen 40 VMs: a known noun and no postcode nearby is no street
            if rx is _PLZ_CITY_RE:
                city = m.group("v")
                before = text[max(0, m.start() - 16):m.start()]
                if _known_word(city) or re.search(r"[\d.]$", before.rstrip()):
                    continue
                if re.search(r"(?i)(?:\b(?:rund|etwa|ca|circa|über|unter|knapp|als|zu|mindestens|maximal|insgesamt|weitere|"
                             r"nur|noch|dazu|gibt|sind|haben|hat|von|mit|auf|about|around|over|under|roughly|nearly|almost|"
                             r"some|than|to|approx)\.?)[ \t]*$", before):
                    continue   # rund 30000 Postfächer: a quantity, not a postcode
            out.append((a, b, "place"))
    return out


_RU_COMPANY_NOUN = (r"(?:компани(?:я|и|ей|ею|ю|ях|ям|ями)|фирм(?:а|ы|е|у|ой|ою|ах|ам|ами)|заказчик(?:а|у|ом|е|и|ов|ам)?|"
                    r"клиент(?:а|у|ом|е|ы|ов|ам)?|партн[её]р(?:а|у|ом|е|ы|ов|ам)?|"
                    r"банк(?:а|у|ом|е)?|холдинг(?:а|у|ом|е)?|фонд(?:а|у|ом|е)?|гк|группа[ \t]+компаний|группы[ \t]+компаний|"
                    r"группе[ \t]+компаний|группой[ \t]+компаний|корпораци[яиейю]|концерн(?:а|у|ом|е)?|поставщик(?:а|у|ом|е)?|"
                    r"подрядчик(?:а|у|ом|е)?|исполнител[ьяюем]|оператор(?:а|у|ом|е)?|провайдер(?:а|у|ом|е)?|интегратор(?:а|у|ом|е)?)")
_FIRMA_RE = re.compile(
    r"(?i)(?<![\w-])(?:Firma|Fa\.|company|%s)"
    r"[ \t]+(?P<v>(?-i:[A-ZÄÖÜА-ЯЁ])[\w'’-]*(?:[ \t]+(?-i:[A-ZÄÖÜА-ЯЁ])[\w'’-]*){0,3})(?![\w-])" % _RU_COMPANY_NOUN)
_RU_QUOTED_RE = re.compile(
    r"(?i)(?<![\w-])(?:%s|ООО|ОАО|ЗАО|ПАО|АО|НКО|ГУП|ФГУП|ТОО|ИП|АНО|МУП|ГБУ|ФГБУ|OOO|ZAO|PAO|OAO|TOO)"
    r"[ \t]*[«\"„“](?P<v>[^»\"“”\n]{2,60}?)[»\"“”]" % _RU_COMPANY_NOUN)
_RU_COMPANY_RE = re.compile(
    r"(?<![\w-])(?:ООО|ОАО|ЗАО|ПАО|АО|НКО|ГУП|ФГУП|ТОО|ИП|АНО|МУП|ГБУ|ФГБУ|OOO|ZAO|PAO|OAO|TOO)"
    r"[ \t]+(?P<v>[A-ZÄÖÜА-ЯЁ][\w'’-]*(?:[ \t]+[A-ZÄÖÜА-ЯЁ][\w'’-]*){0,3})(?![\w-])")
_CYR_COMPANY_RE = re.compile(
    r"(?<![\w&.-])(?P<v>[А-ЯЁ][\w'’-]*(?:[ \t]+[А-ЯЁ][\w'’-]*){0,4})[ \t]*,?[ \t]+(?:%s)(?![\w])" % _FORM_ALT)
_RU_IP_RE = re.compile(r"(?<![\w-])ИП[ \t]+(?P<v>[А-ЯЁ][а-яё]+(?:[ \t]+[А-ЯЁ]\.){0,2})(?![\w-])")
_NAME_INITIAL_RE = re.compile(r"(?<![\w.-])(?P<v>[A-ZÄÖÜА-ЯЁ][a-zäöüßа-яё]{2,}[ \t]+[A-ZÄÖÜА-ЯЁ]\.)(?![\w-])")


_LOWER_LEGAL_RE = re.compile(r"(?<![\w-])(?P<v>[a-zäöüß][\w'’-]{2,}(?:[ \t][a-zäöüß][\w'’-]{2,}){0,3})[ \t]+(?:gmbh|ag|kg|ug|ohg|gbr|ltd|inc|llc|mbh)(?![\w-])")


def _lower_company_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A company typed in lower case with its legal form (nrgtz beratung gmbh in a chat): the unknown words before
    the form, known words at the edges trimmed."""
    out = []
    for m in _LOWER_LEGAL_RE.finditer(text):
        words = _value_words(text, m.start("v"), m.end("v"))
        # the brand stands after the last function word: "reden mit nrgtz gmbh" is nrgtz
        cut = max((i for i, w in enumerate(words) if w[2].casefold() in _GERMAN_FUNCTION
                   or w[2].casefold() in _FUNCTION_WORDS), default=-1)
        words = words[cut + 1:]
        while words and (_known_word(words[0][2]) or _generic_word(words[0][2]) or words[0][2].casefold() in _PRONOUNS):
            words = words[1:]
        while words and (_known_word(words[-1][2]) and words[-1][2].casefold() not in _GENERIC_COMPANY_WORDS):
            words = words[:-1]
        if not words or any(blocked.hit(w[0], w[1]) for w in words):
            continue
        if all(_known_word(w[2]) for w in words):
            continue
        out.append((words[0][0], words[-1][1], "unknown"))
    return out


def _firma_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for m in _FIRMA_RE.finditer(text):
        a, b = m.start("v"), m.end("v")
        words = _value_words(text, a, b)
        if not words or blocked.hit(a, b) or all(_known_word(w[2]) for w in words):
            continue
        out.append((words[0][0], words[-1][1], "company"))
    for rx in (_RU_QUOTED_RE, _RU_COMPANY_RE, _RU_IP_RE, _CYR_COMPANY_RE):
        for m in rx.finditer(text):
            a, b = m.start("v"), m.end("v")
            if rx is _CYR_COMPANY_RE:
                words = _value_words(text, a, b)
                while words and (_known_word(words[0][2]) or _generic_word(words[0][2])):
                    words = words[1:]
                if not words:
                    continue
                a = words[0][0]
            if not blocked.hit(a, b) and text[a:b].strip() and not all(_known_word(w) for w in text[a:b].split()):
                out.append((a, b, "company"))
    for m in _NAME_INITIAL_RE.finditer(text):
        a, b = m.start("v"), m.end("v")
        first = text[a:b].split()[0]
        if blocked.hit(a, b) or _known_word(first) or first.casefold() in _SECTION_WORDS:
            continue   # Szenario B., Exhibit B.: a section letter, not an initial
        out.append((a, b, "person"))
    return out


_SENTENCE_END_IN_RE = re.compile(r"[^\W\d_]\.(?=\s)")


def _spaced_after_title_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A title (Herr, Frau, Dr ...) before a letter spaced stretch: the stretch is a person."""
    out = []
    for m in _SPACED_RE.finditer(text):
        if blocked.hit(m.start(), m.end()):
            continue
        before = text[max(0, m.start() - 12):m.start()].rstrip(" \t.")
        raw_title = re.split(r"[\s.-]+", before)[-1] if before else ""
        title = raw_title.casefold()
        if (title in _TITLE_WORDS or title in _TITLES_EXTRA) and not (len(raw_title) >= 2 and raw_title.isupper()):
            out.append((m.start(), m.end(), "person"))
    return out


def _title_candidates_cut(text: str, blocked) -> list[tuple[int, int, str]]:
    """The title rule of the intake, cut at a full stop that ends a sentence between two name words ("Herr X.
    Tobias Y" is two sentences, not one person); a dot right after a title word (Dr.) is not a sentence end."""
    out = []
    for a, b in _title_candidates(text, blocked, titles=_TITLES_WIPE):
        before = text[:a].rstrip(" \t.")
        title = (re.split(r"[\s.]+", before)[-1] if before else "").lstrip("-")
        if title and not title[:1].isupper() and title.casefold() not in _TITLES_EXTRA:
            continue   # "ms Submillis": a unit, not a title; господин and г-ну are titles
        if len(title) >= 2 and title.isupper() and title.casefold() not in _RU_TITLES:
            continue   # DR Konzept, MS Teams, HR Portal, NAT Regel: an all-caps abbreviation is no title
        parts = re.split(r"[\s.]+", before)
        prev = parts[-2].casefold().strip(",;:") if len(parts) >= 2 else ""
        prev2 = parts[-3].casefold().strip(",;:") if len(parts) >= 3 else ""
        weekdays = ("mo", "di", "mi", "do", "fr", "sa", "so")
        if title.casefold() in ("fr", "hr", "mo", "di", "mi", "do", "sa", "so") and (
                prev in weekdays or (prev in ("bis", "und", "and", "to", "von") and prev2 in weekdays)
                or re.search(r"(?i)\b(?:mo|di|mi|do|fr|sa|so)\.?[-–/][ \t]*%s$" % re.escape(title), text[:a].rstrip(" \t."))):
            continue   # Mo bis Fr Supportzeiten, Mo.-Fr. Wartungsfenster: a weekday, not Frau; "Hr. X und Fr. Y" stays
        m = _SENTENCE_END_IN_RE.search(text, a, b)
        while m and text[a:m.end()].rstrip(".").split()[-1].casefold().rstrip(".") in _TITLE_WORDS:
            m = _SENTENCE_END_IN_RE.search(text, m.end(), b)
        end = m.end() - 1 if m else b
        # a role word after the title stays: Frau Projektleiterin Pomblet
        words = _value_words(text, a, end)
        while words and _role_word(words[0][2]):
            words = words[1:]
        if not words:
            continue
        out.append((words[0][0], end, "person"))
    return out


# --------------------------------------------------------------------------- the rule sets

def _wrap(rule, cls: str):
    def run(text: str, blocked):
        return [(a, b, cls) for a, b in rule(text, blocked)]
    run.__name__ = rule.__name__
    return run


_CN_RE = re.compile(r"(?im)(?<![\w-])CN=(?P<v>[^:;\n\"]{2,80})")
_DC_RE = re.compile(r"(?i)(?:DC=([^,\n]+),?)+")
_BY_VERBS_DE = ("erstellt", "vorgelegt", "genehmigt", "geprüft", "freigegeben", "präsentiert", "verfasst", "bearbeitet",
                "erarbeitet", "zusammengestellt", "gezeichnet", "unterschrieben", "eingereicht", "geändert", "überprüft",
                "reviewt", "vorbereitet", "moderiert", "protokolliert", "aufgenommen", "notiert", "gehalten")
_BY_VERBS_EN = ("prepared", "presented", "reviewed", "approved", "created", "written", "submitted", "authored",
                "compiled", "drafted", "edited", "modified", "updated", "signed", "owned", "maintained", "comment",
                "revision", "revised", "checked", "verified", "delivered", "hosted", "moderated", "recorded")
_BYLINE_RE = re.compile(
    r"(?im)(?<![\w-])(?:(?:%s)[ \t]+von|(?:%s)[ \t]+by)[ \t]+(?P<v>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,3})"
    % ("|".join(_BY_VERBS_DE), "|".join(_BY_VERBS_EN)))


_INITIALS_RE = re.compile(r"(?<![\w.])(?P<v>(?:[A-ZÄÖÜА-ЯЁ]\.[ \t]?){1,3}[A-ZÄÖÜА-ЯЁ][^\W\d_]{2,})(?![^\W\d_])")


def _initial_candidates_known(text: str, blocked) -> list[tuple[int, int, str]]:
    """The initial rule of the intake (X. Surname), not for a list letter: at a line start before a known word, in
    a lettered list (A. B. C. on neighbouring lines) or after a section word (Teil V. Betrieb)."""
    out = []
    for a, b in _initial_candidates(text, blocked):
        words = text[a:b].split()
        last = words[-1] if words else ""
        line_start = text.rfind("\n", 0, a) + 1
        at_line_start = not text[line_start:a].strip(" \t>*_-#|")
        head = text[:a].rstrip(" \t")
        prev = re.split(r"[\s.]+", head)[-1].casefold().strip(",;:") if head.strip() else ""
        if prev in _SECTION_WORDS or (_known_word(last) and at_line_start):
            continue   # Szenario B. Lift and Shift, Teil V. Betrieb, A. Einleitung: a section letter
        if at_line_start and _lettered_list(text, line_start):
            continue
        out.append((a, b, "person"))
    return out


def _initials_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Initials with dots before a surname, Latin or Cyrillic, one to three of them (Т. Surname, Т.И. Surname)."""
    out = []
    for m in _INITIALS_RE.finditer(text):
        a, b = m.start("v"), m.end("v")
        last = re.split(r"[. \t]+", text[a:b])[-1]
        if blocked.hit(a, b) or _known_word(last) or _generic_word(last):
            continue
        before = text[max(0, a - 3):a]
        if re.search(r"[^\W\d_]\.\s?$", before):
            continue   # z. B., u. a.
        head = text[:a].rstrip(" \t")
        prev = re.split(r"[\s.]+", head)[-1].casefold().strip(",;:") if head.strip() else ""
        line_start = text.rfind("\n", 0, a) + 1
        if prev in _SECTION_WORDS or (not text[line_start:a].strip(" \t>*_-#|") and _lettered_list(text, line_start)):
            continue   # Szenario B. Lift and Shift, a lettered list: a section letter, not an initial
        out.append((a, b, "person"))
    return out


def _byline_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Erstellt von X, Vorgelegt von X, Prepared by X, comment by X: the words after the verb and the particle."""
    out = []
    for m in _BYLINE_RE.finditer(text):
        span = _person_value(text, m.start("v"), m.end("v"), blocked, allow_lower=False, max_words=4, vocabulary=True)
        if span and any(_org_word(w) for w in m.group("v").split()):
            continue   # Reviewed by Architecture Board, Erstellt von Abteilung Netze: a body, not a person
        if span:
            out.append((span[0], span[1], "person"))
    return out
_SYSLOG_RE = re.compile(r"(?m)^(?:[A-Z][a-z]{2}[ \t]+\d{1,2}[ \t]+\d\d:\d\d:\d\d|\d{4}-\d\d-\d\dT[\d:.+Z-]+)[ \t]+(?P<host>[a-z][a-z0-9.-]*)[ \t]+\S+(?:\[\d+\])?:[ \t](?P<rest>.*)$")
_SYSLOG_USER_RE = re.compile(r"(?:\bfor(?:[ \t]+invalid[ \t]+user)?|\buser|\bsudo:|USER=|\bby|\bsession opened for user)[ \t]+(?P<v>[a-z][a-z0-9._-]{2,})(?![\w.-])")
_SUBJECT_RE = re.compile(r"(?im)^[ \t>*_]*(?:subject|betreff|тема)[ \t]*:[ \t]*(?:(?:aw|re|fw|fwd|wg|sv|antw|ответ|antwort)[ \t]*:[ \t]*)*(?P<v>[^\n]{1,120})")
_LONE_WORD_RE = re.compile(r"(?<![^\W\d_])(?P<v>[A-ZÄÖÜА-ЯЁ][^\W\d_]{2,})(?![^\W\d_])")


_IDENT_RE = re.compile(r"(?<![\w-])[^\W_]+(?:[_-][^\W_]+)+(?![\w-])|(?<![\w-])[^\W\d_a-z][^\W\d_A-Z]+(?:[^\W\d_a-z][^\W\d_A-Z]*)+(?![\w-])")
_COMPANY_PART_WORDS = frozenset(("gmbh", "ag", "kg", "ug", "ohg", "gbr", "ltd", "inc", "llc", "corp", "ev",
                                 "beratung", "consulting", "systemhaus", "holding", "group", "gruppe", "partner",
                                 "partners", "industries", "logistik", "logistics", "handel", "verlag", "versicherung",
                                 "immobilien", "spedition", "technik", "elektrotechnik", "maschinenbau"))


def _identifier_run_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Capitalised parts of an identifier or file name joined by underscores or hyphens (Angebot_Nrgtz_Beratung,
    Nrgtz-Beratung-Angebot): two or more in a row with an unknown word among them are a name inside the identifier;
    the parts alone are replaced, the rest of the identifier stays."""
    out = []
    for m in _IDENT_RE.finditer(text):
        token = m.group(0)
        if blocked.hit(m.start(), m.end()) or not any(c.isalpha() for c in token):
            continue
        if "_" not in token and "-" in token and token.count("-") < 2:
            # Soll-Konzept, Go-Live, Ist-Situation: a hyphenated compound of two known parts is a word; with an
            # unknown name-shaped first part (Nrgtz-Lösung, Nrgtz-seitig, Pomblet-Konzept) the first part is a name
            first, second = token.split("-", 1)
            if (first[:1].isupper() and _letters(first) >= 4 and _name_shaped(first) and not _known_word(first)
                    and not _generic_word(first) and (_known_word(second) or _generic_word(second) or second.islower())
                    and not any(c.isdigit() for c in first)):
                if not blocked.hit(m.start(), m.start() + len(first)):
                    out.append((m.start(), m.start() + len(first), "unknown"))
            continue
        if "_" not in token and "-" not in token:
            core = token
            for form in ("GmbH", "GMBH", "AG", "KG", "UG", "SE", "Ltd", "Inc", "LLC", "GbR", "OHG"):
                if core.endswith(form) and len(core) > len(form) + 3:
                    core = core[:-len(form)]   # NrgtzBeratungGmbH: the legal form closes the name
                    break
            parts = [(m.start() + x.start(), m.start() + x.end(), x.group(0)) for x in re.finditer(r"[^\W\d_a-z][^\W\d_A-Z]*", core)]
            parts = [x for x in parts if _letters(x[2]) >= 2]
            if core != token and parts and not all(_known_word(x[2]) and x[2].casefold() not in _GENERIC_COMPANY_WORDS
                                                   for x in parts) and not blocked.hit(m.start(), m.end()):
                out.append((m.start(), m.end(), "company"))   # NrgtzBeratungGmbH
                continue
            if not (parts and parts[0][2].casefold() in _FIRST_NAMES) and any(_known_word(x[2]) for x in parts):
                continue   # IntakeResult, RevealError: a known part makes a code identifier
        else:
            parts = [(m.start() + x.start(), m.start() + x.end(), x.group(0)) for x in re.finditer(r"[^_-]+", token)]
            lows = [x[2].casefold() for x in parts]
            if any(l in _COMPANY_PART_WORDS for l in lows):
                # angebot_nrgtz_beratung_gmbh, nrgtz-beratung-angebot: an unknown lower-case part next to a company
                # word is the brand
                for x in parts:
                    low = x[2].casefold()
                    if (_letters(x[2]) >= 4 and x[2].isalpha() and low not in _COMPANY_PART_WORDS
                            and not _known_word(x[2]) and not _generic_word(x[2]) and not blocked.hit(x[0], x[1])):
                        out.append((x[0], x[1], "unknown"))
        run: list[tuple[int, int, str]] = []
        for part in parts + [(0, 0, "")]:
            w = part[2]
            if w and w[:1].isupper() and w[1:].islower() and _letters(w) >= 2 and not any(c.isdigit() for c in w):
                run.append(part)
                continue
            if (len(run) >= 2 and any(not _person_known(x[2]) and not _generic_word(x[2]) for x in run)
                    and not any(x[2].casefold() in _FUNCTION_WORDS for x in run)):
                if not blocked.hit(run[0][0], run[-1][1]):
                    out.append((run[0][0], run[-1][1], "unknown"))
            run = []
    return out


_BARE_USER_HOST_RE = re.compile(r"(?<![\w.@-])(?P<local>[a-z][a-z0-9._-]{2,})@(?P<host>[a-z][a-z0-9-]{1,}(?:\.[a-z0-9-]+)*)(?::(?P<owner>[a-z][\w-]{2,})/)?(?![\w.@-])")
_DECORATORS = frozenset(("dataclass", "property", "staticmethod", "classmethod", "abstractmethod", "cached_property",
                         "wraps", "lru_cache", "contextmanager", "override", "overload", "pytest", "mock", "patch",
                         "task", "dag", "route", "app", "router", "bp", "api", "login_required", "receiver", "signal",
                         "command", "group", "option", "argument", "click", "cli", "fixture", "parametrize", "mark",
                         "skip", "skipif", "xfail", "timeout", "retry", "singledispatch", "total_ordering", "unique",
                         "verify", "validator", "field_validator", "model_validator", "root_validator", "pydantic",
                         "cache", "memoize", "profile", "deprecated", "experimental", "beta", "internal", "export",
                         "component", "injectable", "input", "output", "override", "test", "before", "after", "setup",
                         "teardown", "given", "when", "then", "step", "scenario", "feature", "tag", "media", "import",
                         "extends", "implements", "interface", "param", "return", "returns", "throws", "see", "since",
                         "author", "version", "deprecated", "link", "code", "value", "example", "brief", "file"))
_HANDLE_RE = re.compile(r"(?<![\w.@/-])@(?P<v>[a-z][\w-]{2,})(?P<slash>/)?")


def _host_piece_spans(text: str, a: int, b: int, blocked) -> list[tuple[int, int, str]]:
    """The unknown pieces of a host or inventory name (srv-pomblet-02: pomblet), four letters or more, no affix."""
    from awb.matcher import GLUE_AFFIXES
    out = []
    first = text[a:b].split(".")[0]
    for m in re.finditer(r"[a-z]{4,}", first.casefold()):
        piece = m.group(0)
        if piece in GLUE_AFFIXES or piece in _SYSTEM_USERS or _known_word(piece) or piece in _PUBLIC_PROVIDERS:
            continue
        x, y = a + m.start(), a + m.end()
        if not blocked.hit(x, y):
            out.append((x, y, "unknown"))
    return out


_INVENTORY_HOST_RE = re.compile(r"(?m)^[ \t]*(?P<host>[a-z][a-z0-9.-]{3,})[ \t]+(?=ansible_|[a-z_]+=)")
_USERS_LIST_RE = re.compile(r"(?i)\"(?:users|members|admins|logins|owners|assignees|reviewers|approvers|maintainers|accounts)\"[ \t]*:[ \t]*\[(?P<v>[^\]]*)\]")


def _inventory_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Hosts of an Ansible inventory line (pomblet-web01 ansible_host=...) and the logins of a JSON users list."""
    out = []
    for m in _INVENTORY_HOST_RE.finditer(text):
        out.extend(_host_piece_spans(text, m.start("host"), m.end("host"), blocked))
    for m in _USERS_LIST_RE.finditer(text):
        for item in re.finditer(r"\"([^\"]{3,40})\"", m.group("v")):
            v = item.group(1)
            if _login_value(v.lower()) is not None:
                a = m.start("v") + item.start(1)
                if not blocked.hit(a, a + len(v)):
                    out.append((a, a + len(v), "person"))
    return out


def _login_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """ssh login@host without a domain, git@host:owner/repo, @handle of a code owners file: the login, the owner
    and the handle are wiped when they are no system user and no known word; the host labels are learned."""
    out = []
    for m in _BARE_USER_HOST_RE.finditer(text):
        if blocked.hit(m.start(), m.end()):
            continue
        local = m.group("local")
        if "." in m.group("host") and _MAIL_RE.match(text, m.start()):
            continue   # a real address, the MAIL token has it
        if _login_value(local) is not None or _PERSON_LOCAL_RE.fullmatch(local):
            out.append((m.start("local"), m.end("local"), "person"))
        if m.group("owner") and _login_value(m.group("owner")) is not None:
            out.append((m.start("owner"), m.end("owner"), "unknown"))
        out.extend(_host_piece_spans(text, m.start("host"), m.end("host"), blocked))
    for m in _HANDLE_RE.finditer(text):
        if blocked.hit(m.start("v"), m.end("v")) or _login_value(m.group("v")) is None:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        after = text[m.end():m.end() + 1]
        if text[line_start:m.start()].strip() == "" and (after in ("(", ".", "", "\n") or m.group("v") in _DECORATORS):
            continue   # @dataclass, @property, @pytest.mark: a decorator, not a mention
        out.append((m.start("v"), m.end("v"), "unknown" if m.group("slash") else "person"))
    # the owner segment of an scp-style git remote (git@host:owner/repo.git); the address part is a MAIL token
    for m in re.finditer(r"@[\w.-]+:(?P<owner>[a-z][\w-]{2,})/[\w.-]+", text):
        if not blocked.hit(m.start("owner"), m.end("owner")) and _login_value(m.group("owner")) is not None:
            out.append((m.start("owner"), m.end("owner"), "unknown"))
    return out


_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+(?P<v>[^\n]{2,60})[ \t]*$")


def _subject_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A subject line or a Markdown heading (a sheet name): a lone unknown name-shaped word in it (AW: Pomblet,
    ## Pomblet) is a name or a product, [name]."""
    out = []
    for m in list(_SUBJECT_RE.finditer(text)) + list(_HEADING_RE.finditer(text)):
        words = _value_words(text, m.start("v"), m.end("v"))
        if len(words) != 1:
            continue
        a, b, w = words[0]
        if blocked.hit(a, b) or not _NAME_SHAPE_RE.fullmatch(w) or not w[:1].isupper() or w.isupper() \
                or _known_word(w) or codes.is_code(w) or text[b:b + 1].isdigit() or re.match(r"[ \t]+\d", text[b:b + 4]):
            continue   # Лист1, Sheet2, Slide 1, Welle 2: a word with a number is a label, no name
        out.append((a, b, "person" if w.casefold() in _FIRST_NAMES else "unknown"))
    return out


_LONE_LINE_RE = re.compile(r"(?m)^[ \t]*(?P<v>[^\W\d_][\w'’-]{2,})[ \t]*\n[ \t]*(?P<next>[^\n]{3,80})$")


def _syslog_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """The host field and the user of a syslog line (sshd: Accepted password for tpomblet, sudo: tpomblet)."""
    out = []
    for m in _SYSLOG_RE.finditer(text):
        host = m.group("host")
        for part in re.finditer(r"[a-z]{4,}", host.split(".")[0]):
            w = part.group(0)
            if not _known_word(w) and w not in _SYSTEM_USERS:
                a = m.start("host") + part.start()
                if not blocked.hit(a, a + len(w)):
                    out.append((a, a + len(w), "unknown"))
        for u in _SYSLOG_USER_RE.finditer(m.group("rest")):
            v = u.group("v")
            if _login_value(v) is not None:
                a = m.start("rest") + u.start("v")
                if not blocked.hit(a, a + len(v)):
                    out.append((a, a + len(v), "person"))
    return out


_HYPHEN_BREAK_RE = re.compile(r"(?m)^[ \t]*(?P<v>[^\W\d_][\w'’]{2,})-[ \t]*\n[ \t]*(?P<next>[^\n]{3,80})$")


def _hyphen_break_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A word that ends in a hyphen at a line end before a company line: the brand of a hyphenated company broken
    at its hyphen (Nrgtz-\\nBeratung GmbH)."""
    out = []
    for m in _HYPHEN_BREAK_RE.finditer(text):
        w, nxt = m.group("v"), m.group("next")
        if not w[:1].isupper() or _known_word(w) or _generic_word(w) or blocked.hit(m.start("v"), m.end("v")):
            continue
        if _COMPANY_RE.search(nxt):
            out.append((m.start("v"), m.end("v"), "company"))
    return out


def _lone_line_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A lone unknown capitalised word on a line of its own, followed by a company line (a legal form) or a role
    line: the signer of a mail without a closing phrase."""
    out = []
    for m in _LONE_LINE_RE.finditer(text):
        w, nxt = m.group("v"), m.group("next")
        if not w[:1].isupper() or _person_known(w) or _generic_word(w) or blocked.hit(m.start("v"), m.end("v")):
            continue
        words = nxt.split()
        if _COMPANY_RE.search(nxt) or (words and all(_role_word(x) or _known_word(x) for x in words)
                                             and any(_role_word(x) for x in words)):
            out.append((m.start("v"), m.end("v"), "person"))
    return out


_TWO_LINE_NAME_RE = re.compile(r"(?m)^[ \t]*(?P<first>[^\W\d_][\w'’-]{1,})[ \t]*\n(?=[ \t]*(?P<last>[^\W\d_][\w'’-]{2,})[ \t]*$)")
"""The second line is a lookahead, so a heading over the first name does not swallow the pair below it."""


def _two_line_name_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A first name of the list on a line of its own, an unknown capitalised word on the next line: the boxes of
    an org chart, a contact slide, a signature laid out in two paragraphs."""
    out = []
    for m in _TWO_LINE_NAME_RE.finditer(text):
        first, last = m.group("first"), m.group("last")
        if not last[:1].isupper() or _person_known(last) or _generic_word(last) or not _name_shaped(last):
            continue
        if first.casefold() not in _FIRST_NAMES:
            # an unknown capitalised word over another unknown one, a role or company line below: a name box
            nxt = text[m.end():m.end() + 80].split("\n")[1] if text[m.end():m.end() + 80].count("\n") >= 1 else ""
            words = nxt.split()
            if not (first[:1].isupper() and _name_shaped(first) and not _person_known(first) and not _generic_word(first)
                    and words and (_COMPANY_RE.search(nxt) or (all(_role_word(x) or _known_word(x) for x in words)
                                                                      and any(_role_word(x) for x in words)))):
                continue
        if blocked.hit(m.start("first"), m.end("last")):
            continue
        out.append((m.start("first"), m.end("first"), "person"))
        out.append((m.start("last"), m.end("last"), "person"))
    return out


def _lone_first_name_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A first name of the list standing alone in prose (Markus hat die Firewall gebaut), capitalised, not a word
    of the public corpus and not written in lower case elsewhere in the text."""
    out = []
    lows = None
    for m in _LONE_WORD_RE.finditer(text):
        w = m.group("v")
        if not w[:1].isupper() or w.isupper() or w.casefold() not in _FIRST_NAMES or blocked.hit(m.start(), m.end()):
            continue
        if w.casefold() in _UNIT_WORDS or w.casefold() in _ABBREVIATIONS or text[m.end():m.end() + 1] == ".":
            continue   # Max. Bandbreite is an abbreviation
        prev = re.search(r"([^\W\d_][\w'’-]*)[ \t]+$", text[max(0, m.start() - 40):m.start()])
        nxt = re.match(r"[ \t]+([^\W\d_][\w'’-]*)", text[m.end():m.end() + 40])
        if (prev and prev.group(1)[:1].isupper()) or (nxt and nxt.group(1)[:1].isupper()):
            continue   # part of a run of capitalised words, the run rule's business
        if re.match(r"[ \t]+\d", text[m.end():m.end() + 4]):
            continue   # Julia 1.9 is a language, not a person
        if nxt and nxt.group(1).casefold() in _PARTICLE_WORDS | {"of", "del", "da"}:
            continue   # Bill of Materials, Thomas von X: the particle rule decides
        if lows is None:
            lows = set(x.casefold() for x in re.findall(r"(?<![\w.@/-])[a-zäöüß]{3,}(?![\w.@/-])", text))
        if w.casefold() in lows:
            continue
        out.append((m.start("v"), m.end("v"), "person"))
    return out


def _cn_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """The common-name parameter of a calendar attendee or organiser (ATTENDEE;CN=Name:mailto:...)."""
    out = []
    for m in _CN_RE.finditer(text):
        span = _person_value(text, m.start("v"), m.end("v"), blocked, allow_lower=True, max_words=4)
        if span:
            out.append((span[0], span[1], "person"))
    return out


_VON_UNKNOWN_RE = re.compile(r"(?<![\w-])(?P<first>[A-ZÄÖÜ][\wäöüß-]{3,})[ \t]+(?:von|vom)[ \t]+(?P<v>[A-ZÄÖÜ][\wäöüß'’-]{3,})(?![\w-])(?![ \t]+[A-ZÄÖÜ])")


def _von_unknown_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """"Firewall von Nrgtz", "Ablösung von Qwertz": a known noun, von, then exactly one unknown name-shaped word
    (no vendor, no corpus or list word, no first name, no capitalised word after it): the word is [name]."""
    out = []
    for m in _VON_UNKNOWN_RE.finditer(text):
        first, v = m.group("first"), m.group("v")
        a, b = m.start("v"), m.end("v")
        if not _known_word(first) or blocked.hit(a, b) or _known_word(v) or _generic_word(v) or codes.is_code(v):
            continue
        if v.casefold() in _FIRST_NAMES or not _name_shaped(v) or v.isupper():
            continue
        out.append((a, b, "unknown"))
    return out


_VOCATIVE_RE = re.compile(r"(?im)(?<![\w-])(?:danke|hallo|hi|hey|moin|servus|thanks|thx|merci|спасибо|привет)[ \t]+"
                          r"(?P<v>[A-ZÄÖÜА-ЯЁ][\wäöüßа-яё'’-]{2,})(?=[ \t]*(?:[,.!?;:]|$))")


def _vocative_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """"Danke Tobi, dann teste ich": a capitalised unknown word right after thanks or a greeting inside a line,
    before a comma, a full stop or the line end, is the person spoken to (a nickname the learned forms miss)."""
    out = []
    for m in _VOCATIVE_RE.finditer(text):
        v = m.group("v")
        a, b = m.start("v"), m.end("v")
        if blocked.hit(a, b) or _person_known(v) or _generic_word(v) or codes.is_code(v) or v.isupper():
            continue
        out.append((a, b, "person"))
    return out


def _particle_candidates_known(text: str, blocked) -> list[tuple[int, int, str]]:
    """First von Last of the intake, not when the first word is a known word that is no first name (Freigabe von X)
    or the last word is a known word (Peter von Siemens stays a limit)."""
    out = []
    for a, b in _particle_candidates(text, blocked):
        words = [w for _, _, w in _value_words(text, a, b)]
        if words and (_person_known(words[0]) or _generic_word(words[0]) or _known_word(words[-1])
                      or words[0].casefold() in _BY_VERBS_DE or words[0].casefold() in _BY_VERBS_EN):
            continue   # Erstellt von Tobias: the by-line rule takes the person after the particle
        out.append((a, b, "person"))
    return out


PROPOSED_RULES = (
    _firma_candidates,
    _wrap(_company_candidates, "company"),
    _lower_company_candidates,
    _wrap(_label_candidates, "company"),
    _label_value_candidates,
    _table_candidates,
    _fixed_table_candidates,
    _header_candidates,
    _reply_candidates,
    _salutation_candidates,
    _signoff_candidates,
    _speaker_candidates,
    _address_candidates,
    _spaced_after_title_candidates,
    _title_candidates_cut,
    _particle_candidates_known,
    _von_unknown_candidates,
    _vocative_candidates,
    _initial_candidates_known,
    _initials_candidates,
    _cn_candidates,
    _byline_candidates,
    _subject_candidates,
    _wrap(_run_candidates, "unknown"),
    _wrap(_inverted_candidates, "person"),
    _wrap(_spaced_candidates, "unknown"),
    _identifier_run_candidates,
    _two_line_name_candidates,
    _syslog_candidates,
    _inventory_candidates,
    _hyphen_break_candidates,
    _lone_line_candidates,
    _lone_first_name_candidates,
    _login_candidates,
)




def _plausible(text: str, a: int, b: int, cls: str) -> bool:
    """Proposed: a candidate carries a letter; a company label value carries a capitalised word; a "Last, First"
    pair is no person when both halves are known words or one is an acronym."""
    value = text[a:b]
    if not any(c.isalpha() for c in value):
        return False
    words = [w for _, _, w in _value_words(text, a, b)]
    if cls == "company" and not any(w[:1].isupper() for w in words):
        return False
    if cls == "person" and "," in value and len(words) == 2:
        if _person_known(words[0]) or _generic_word(words[0]) or words[0].casefold() in _GREETING_WORDS \
                or all(w.isupper() and len(w) <= 5 for w in words):
            return False
        if any(w.isupper() and len(w) <= 5 for w in words) and not any(w.casefold() in _FIRST_NAMES for w in words):
            return False
    if cls == "company" and len(words) >= 2 and sum(
            1 for w in words if not w[:1].isupper() and w.casefold() not in _COMPANY_JOINERS) >= 1:
        return False   # "Wir wollen eine Landing Zone bis": a sentence after a label is no company
    if cls == "company" and words and words[-1].casefold().strip(".") in _AMBIGUOUS_LEGAL \
            and all(_known_word(w) for w in words[:-1]):
        return False   # "TiB SAS", "Standard SE": a unit or a known word before a form that is also a word
    before = text[:a].rstrip()
    prev = re.split(r"[\s|]+", before)[-1].strip(".,") if before and "\n" not in text[len(before):a] else ""
    # only a capitalised word in front names the entity (T-Systems International GmbH); "from" and "into" are SQL
    # words of the stop list, and "from Nrgtz Beratung GmbH" is a company like any other
    prev = prev.casefold() if prev[:1].isupper() else ""
    if cls == "company" and words and (words[0].casefold().strip(".,") in _SOURCE_PLATFORM_WORDS
                                       or words[0].casefold().strip(".,") in _STOP_SINGLES
                                       or prev in _SOURCE_PLATFORM_WORDS or prev in _STOP_SINGLES):
        return False   # T-Systems International GmbH, Microsoft Deutschland GmbH: the vendor names the entity
    return True


_COMPANY_JOINERS = frozenset(("und", "and", "&", "von", "de", "of", "for", "the", "la", "le", "del", "der", "des",
                              "di", "en", "e", "y", "für", "im", "am", "zu", "zur", "zum", "und", "co", "mbh", "gmbh"))
_GREETING_WORDS = frozenset(("hallo", "hi", "hey", "hello", "dear", "moin", "servus", "привет", "здравствуйте", "danke",
                             "thanks", "thx", "спасибо", "grüße", "gruß", "regards", "cheers", "ciao", "hola", "bonjour",
                             "guten", "good", "liebe", "lieber", "sehr", "geehrte", "geehrter", "уважаемый", "уважаемая"))
_AMBIGUOUS_LEGAL = frozenset(("sas", "se", "ag", "kg", "sa", "oy", "ug", "co", "inc", "plc", "ltd", "mbh"))


_ROLE_GENERIC = frozenset(("data", "cloud", "security", "product", "produkt", "service", "services", "customer", "kunde",
                           "kunden", "technical", "technischer", "technische", "solutions", "solution", "business",
                           "development", "enterprise", "global", "regional", "digital", "program", "programme", "project",
                           "projekt", "portfolio", "strategy", "strategie", "transformation", "migration", "governance",
                           "compliance", "risk", "quality", "qualität", "testing", "test", "support", "key", "account",
                           "sales", "delivery", "operations", "infrastructure", "platform", "plattform", "intern",
                           "internal", "external", "extern", "guest", "gast", "desk", "hotline", "helpdesk", "servicedesk",
                           "center", "centre", "zentrale", "leitstelle", "betrieb", "team", "office", "backoffice",
                           "frontoffice", "member", "mitglied", "staff", "vice", "executive", "senior", "junior",
                           "principal", "associate", "partner", "abteilung", "bereich"))
"""Role words that are also ordinary words of a product or a department: they trim a run but do not make a person."""
_PRONOUNS = frozenset(("my", "mine", "me", "we", "you", "us", "our", "your", "their", "its", "his", "her", "them",
                       "mein", "meine", "dein", "deine", "unser", "unsere", "euer", "eure", "ihr", "ihre", "sein", "seine"))


def _trim_run(text: str, a: int, b: int) -> tuple:
    """A run of capitalised words minus the known words at both ends (a first name of the list stays): "Protokoll
    Abstimmung Nrgtz" leaves Nrgtz, "Projektleiter Pomblet" leaves Pomblet as a person. (start, end, class) or
    (None, None, None) when nothing is left."""
    words = _value_words(text, a, b)
    role = company = False
    left = right = 0
    # a first name of the list in front makes the rest a person's words: the German noun suffix says nothing of a
    # surname (Max_Qwertzung_export, tf-ident-known-first-suffix-surname)
    person = bool(words) and words[0][2].casefold().strip(".,;:") in _FIRST_NAMES and not words[0][2].isupper()

    def known(w: str) -> bool:
        low = w.casefold().strip(".,;:")
        if low in _FIRST_NAMES and not w.isupper():
            return False
        if person:
            return _person_known(w) or _generic_word(w) or low in _PRONOUNS
        return _known_word(w) or _generic_word(w) or low in _PRONOUNS

    while words and known(words[0][2]):
        low = words[0][2].casefold().strip(".,;:")
        role = role or _role_shape(low) or (low in _ROLE_WORDS and low not in _ROLE_GENERIC)
        company = company or low in _GENERIC_COMPANY_WORDS or low in _COMPANY_PART_WORDS
        words = words[1:]
        left += 1
    while words and known(words[-1][2]):
        low = words[-1][2].casefold().strip(".,;:")
        company = company or low in _GENERIC_COMPANY_WORDS or low in _COMPANY_PART_WORDS
        words = words[:-1]
        right += 1
    if not words:
        return None, None, None
    if len(words) == 1:
        w = words[0][2]
        if w.isupper() and len(w) <= 5 or codes.is_code(w):
            return None, None, None   # "CUST FILEID" minus the known word leaves an acronym, not a name
        low = w.casefold().strip(".,;:")
        if left == 0 and right >= 1 and low not in _FIRST_NAMES and not role and not company:
            head = text[:words[0][0]].rstrip(" \t")
            if not head or head.endswith(("\n", ".", "!", "?", ":", ";", "|", ">", "-", "*")):
                return None, None, None   # "Je Mandant", "Statt Backup": a sentence opener nobody listed, not a name
    cls = "person" if role else "unknown"
    return words[0][0], words[-1][1], cls


def _run_rule_exempt(text: str, a: int, b: int) -> bool:
    """Proposed: a run of capitalised words is not a name when every word is known (a first name of the list in
    front is never known), when the run is a known phrase of the corpus, a neutral label or a stop phrase, or when
    a vendor or platform word opens it (past a short acronym): Juniper Mist, HPE Nimble Storage."""
    words = [w for _, _, w in _value_words(text, a, b)]
    if not words:
        return True
    phrase = " ".join(words).casefold()
    if phrase in _PHRASES or phrase in _NEUTRAL_LABELS:
        return True
    if words[0].casefold().strip(".,;:") in _FIRST_NAMES and not words[0].isupper():
        rest = [w.casefold().strip(".,;:") for w in words[1:]]
        if rest and all(w in _CORPUS or w in _SOURCE_PLATFORM_WORDS or w in _UNIT_WORDS or w in _STANDARD_WORDS
                        for w in rest) and not any(w in _FIRST_NAMES for w in rest):
            return True   # Grace Period, Max Pods: a first name that opens a phrase of the corpus
        return False
    if all(_known_word(w) or w.casefold() in _PRONOUNS for w in words):
        return True
    rest = words
    while rest and rest[0].isupper() and len(rest[0]) <= 3:
        rest = rest[1:]
    first = rest[0].casefold().strip(".,;:") if rest else ""
    if first in _SOURCE_PLATFORM_WORDS or first in _STANDARD_WORDS:
        return True   # the vendor names the product: Juniper Mist, Veeam ONE, Sophos Firewall
    return False


_MARKS_RE = re.compile(r"\*{1,3}|</?(?:b|i|u|em|strong|span|font|a|mark|small|sup|sub)\b[^<>\n]{0,80}>")


def _blank_marks(text: str) -> str:
    """The text with Markdown emphasis marks and inline html tags turned into spaces of the same length, so that
    **First** **Last** and <b>First</b> Last read as a run (positions unchanged)."""
    if "*" not in text and "<" not in text:
        return text
    return _MARKS_RE.sub(lambda m: " " * len(m.group(0)), text)


# the names of the later families this one calls; they import this module, so these come after its own
from awb.intake.wipe.learned import _PUBLIC_PROVIDERS, _RU_TITLES, _TITLES_EXTRA  # noqa: E402
from awb.intake.wipe.runstate import _CORPUS, _PHRASES, _TITLES_WIPE  # noqa: E402
