---
name: drafting
description: Use for every text that leaves under his name, before he sees it. That is a reply to a customer or partner, a mail, an offer or tender text, a PoC or architecture document, a description. Writes in his voice, checks the facts first and the voice second and proves that the voice pass changed no fact.
---

# Drafting in his voice

A text that leaves under his name is his: the facts, the judgement and the last edit. This skill makes his last
edit short. It does not replace his review and it cannot promise that no reader or detector ever suspects a text.

## Before you write

1. Read the incoming message or the request in full. Write its questions down, one line each. The answer covers
   those and nothing else.
2. Set the length budget now, from the request. A two-line question gets a few lines. The budget does not grow
   later.
3. Note the nouns and verbs of the incoming message and reuse them. When they write "instance", do not write
   "server".
4. Collect the facts first. `awb kb find` for checked facts, `awb price` for every price (never from memory), a
   live check or a dated source for availability and for every negative. Keep the source of each fact.
   Public documentation of an allowed host (`awb doc hosts`) comes from `awb doc fetch URL`, never from memory:
   quote it with the source and the date of its first line.
   Every number you compute (a total, a saving, a product) comes from `awb calc`, never from your head.
5. A how-to for the TCP console (menu path, screen, field, button, tab) starts from `awb howto draft "TOPIC"
   [--service SVC]`, never from memory. It writes the draft into `deliverables/` with the steps in order and the exact
   labels, text only: no image, no screen name, no mention of screenshots, images, a mirror or a walk. The screens it
   rests on and the date of the walk go into the internal note `reviews/<name>/sources.md` next to the draft. Edit the
   text, keep every label as the screen spells it, never invent one. A step without a source stays "not confirmed
   yet: check on screen" until it is seen on screen. `awb mirror console find|show` looks up a single screen. An
   image version for yourself is `--internal`: it is never sent and never published. What a service is made of still
   comes from the docs, the API, the knowledge base and experience.

## How he writes

- Answer in the first line. The result or the ask comes first, the reasons after it.
- First person singular: I checked, I see, I need. A mail or a document never speaks as `we`, `our`, `us`
  or `wir`.
- Facts and numbers instead of reassurance: region, flavor, error code, price with its date and source.
- No commitment in his name. Say what is true now and what is still open, not what he will do.
- No invented names for options. Call an option by what it is, for example "CCE with two node pools".
- No pointer to an earlier mail. Say the point again.
- A courteous plain register for German readers: short sentences (his median is 11 words), plain verbs such as
  use, check, send, fix and build, no idioms and no slang.
- His connectors are and, so, then, but, also and because. `awb write check` names the others.
- T Cloud Public (TCP) at the first mention and TCP after it. Identifiers and host names stay as they are.
- No em-dash, no comma before "and" or "or", no list where every line starts with bold, no heading that states a
  verdict. Headings name topics.
- Where he already gave the words, keep his words.
- No section the request did not ask for, no admission of weakness, no sales tone, no history of the
  draft and no invented jargon. These are what he struck out most often.

## The order of the checks: facts first, voice second

1. Facts. For a deliverable in a project run `awb review init`, `awb review claims` and give every claim its
   evidence and verdict. From tier 2 the lenses run too. For a short reply outside a project, check every number,
   price, service name and negative against its source.
2. Keep the checked version before the voice pass. For a deliverable save it as
   `reviews/<name>/before-voice.md` in the project. For a reply save it in your scratch folder.
3. The voice pass. Rewrite with the rules above. Add no fact and drop no fact.
4. `awb write keep BEFORE AFTER`. Every number, identifier, negation and code block must still be there. A
   difference blocks: put the fact back.
5. `awb write check FILE --mode mail` (or `doc` or `chat`). Save the request as a file and add
   `--request REQUEST`: a section it did not ask for is named. Fix every blocking tell. Then read the reported
   numbers: sentence length, fragments, comma splices, verdict headings, connectors and the I rate. Fix what reads
   unlike him.
6. Cut to the budget as a separate last step. Then run steps 4 and 5 again.
7. Show him the draft with one line on which checks ran and what is still unverified. The review record stays out
   of the text. A mail draft goes into a file first. The stop hook holds a reply that shows a draft before
   `awb write check` of that file ran clean in the same turn.

## German and Russian

He accepts the English text first. Two chains decide what happens next.

Chain 1, the customer writes German (his material or request is German): you talk to him in Russian and the deliverable
is German. The German version and his Russian reading copy are made from the accepted English text in one step, never
by rewriting: `awb write translate FILE --to de --to ru`. Show him the Russian copy. Chain 2, he asks in Russian or
English: you talk to him in English and the deliverable is English only, no German or Russian version unless he asks.
He may ask you to talk Russian ("по-русски"): then you talk Russian until he switches back, and the deliverable stays English. Never comment on his
English, his dictation slips or his punctuation.

The translate command writes
`FILE.de.md` and `FILE.ru.md` next to the file, proves with `awb write keep` that every number, identifier and code
block is still there and runs `awb write check` on the German text. When no model is reachable it writes the prompt
file and stops: translate along that prompt into the named file, then run the same command with `--verify`.
The checker is built for English. In a German text the blocking rules on `wir`, commitments, earlier mails,
weakness, sales tone and the history of the draft still apply, the sentence numbers do not.

## After he sends it

When he gives you the version he actually sent, store the pair with `awb voice learn DRAFT SENT`. The report lists
what he removed and added. He changes the rules himself.
