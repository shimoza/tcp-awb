# Calibration run of the review workflows, 2026-09-25

The first live run of `workflows/review.js` and `workflows/request-check.js`, on the 13 cases of
`workflow-cases.json`, in a lab project of invented texts, at tier 3, with the TCP lens and the partner lens. Score
with `awb review calibrate --lenses PROJECT`. This file is the baseline the next run is compared with.

## Result

| case | defect | caught by (an open blocking finding or, for extra content, an EXTRA finding) |
|---|---|---|
| m1 | a false "does not exist" | blind verification (WRONG), partner, request check |
| m2 | an invented flavor | blind verification (WRONG), TCP lens, partner (P3), request check |
| m3 | an invented patch schedule | blind verification (WRONG), TCP lens (WRONG), partner |
| m4 | content nobody asked for | request check (EXTRA), partner (EXTRA), TCP lens |
| m5 | a misread request | TCP lens, partner (P1) and request check, all MISREAD |
| m6 | recovery steps in inverted order | TCP lens (WRONG), partner, request check |
| m7 | a review record inside the text | TCP lens, partner and request check, all EXTRA |
| m8 | a claim graded said stated as fact | request check (MISLEADING), blind verification (WRONG), partner |

8 of 8 defects caught, each by a finding that names it (read by hand, not just counted). On the 5 clean texts
there were 2 false alarms, the limit of the design: the partner question P9 (a negative needs what was tried)
fired on c1 ("masks down to /29") and c5 ("only in eu-de"). Both texts carry an implicit negative without naming
the check, so the finding is debatable rather than wrong.

## What the run changed

- The request check first raised blocking findings on every clean text. Two causes: the claim lists of the clean
  cases were empty (in a real review l0 fills them before the last check) and a factual sentence without a row
  was judged an overclaim. The request check now judges overclaims only on rows of the claim list and reports a
  sentence without a row as a major UNSOURCED finding that asks for a row. After the fix: no blocking finding on
  any clean text.
- The claim list missed an invented flavor (s9.huge.8), a physical flavor name (c7t.28xlarge.4.physical), a mask
  (/29) and the negative "only in eu-nl". All four are claims now.

## Cost

185 agent runs: 13 reviews of 6 to 13 agents each (123 runs) and 31 request checks of 2 agents each (three rounds
while the request check was fixed). Subagent tokens: about 10.7 million for the reviews and 3.4 million for the
request checks, so a tier 3 review of a short text costs 0.45 to 1.35 million tokens on this model. The design
note on cost ("measure on a small slice first") now has its first numbers.
