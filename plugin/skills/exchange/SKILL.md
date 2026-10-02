---
name: exchange
description: Take a file the owner dropped into an inbox into the project, and put a result of the project back for him to fetch in the OBS console
---

# Files in and out

The owner drops files into an inbox in the OBS console and names one in his prompt ("read FILE from the inbox").

1. Take only the file he names: `awb inbox take FILE`. From the lab inbox you get the file after the name check;
   from the owner inbox you get only the sanitised copies of his intake, never the original. A held file stays
   where it is and the owner gets a mail: tell him in one line and go on.
2. A project without a customer takes from the owner inbox only with the code he gives: `--customer CUST-XXXX`.
3. A file from or about a customer that reached you through the lab inbox: stop, tell him, keep it out of git
   and ask him to drop it into the owner inbox.
4. Results go out with `awb xchg put FILE` into `<project>/from-session/<date>/`; he gets a mail. Codes only, no ids
   in logs. A screenshot only with `--image --reason "what it shows, checked for names and ids"`. A deliverable of
   a customer project leaves only with a valid review.
5. `awb xchg list` shows what you put; `awb inbox list` shows the lab inbox.
