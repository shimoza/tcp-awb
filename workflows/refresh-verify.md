# Second check of proposed corrections (T Cloud Public, TCP)

The prompt of every second-check agent of `awb refresh`. It reads refresh-brief.md first.

A first checker re-checked expired facts of a knowledge base about T Cloud Public (TCP) and proposed to correct
(changed) or withdraw (refuted) some of them. You are the second check. Nothing is applied unless you agree. Read
refresh-brief.md first: its sources, its rules and its text rules apply to you unchanged.

## Your input and output

- Input: your file, one JSON object per line: the old fact (old_statement, old_grade, old_source, old_tried, tags)
  and the proposal (proposed_verdict, proposed_statement, proposed_grade, proposed_source, proposed_tried,
  proposed_why).
- Output: your result file, one JSON object per line, one line for EVERY id:

  {"id": "KB-XXXX", "decision": "agree|adjust|disagree", "statement": "...", "grade": "...", "source": "...",
   "tried": ["...", "..."], "why": "one line"}

  - agree: the proposal is right and its text is exact. Copy nothing else; why says in one line what you checked.
  - adjust: the proposal is right in substance but its statement, grade, source or tried texts need a fix. Give the
    fixed fields in full (statement, grade, source, tried); leave out what needs no fix.
  - disagree: the old fact stands or the proposal is wrong in a way you cannot fix. why says what you found.
- Write the file with a small python3 script (json.dumps per line). Answer at the end with counts per decision
  and the ids you disagreed with, one line each.

## How to check

- Check the claim yourself against the sources, do not take the first checker's source on trust. Open the file or
  make the GET call it names. Look at least at one other place.
- A refutation withdraws a fact for good: be strict. "Not mentioned in the docs" does not refute a live
  observation. A live GET that shows the opposite does.
- A corrected statement has to be true as written, carry no more than the evidence shows and pass the text rules
  of BRIEF.md (English, no price, no home path, no names or ids, a negative with two tried texts of today).
- Some old facts describe the behaviour of a write call. A docs page that describes the call differently is a
  reason to adjust the wording to "the documentation says", not to claim the live behaviour changed.

## Budget

About three tool calls per entry. When you cannot settle an entry within about six calls, decide disagree with why
"could not settle": the old fact then stays as it is, which is the safe side.
