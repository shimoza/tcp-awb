"""The two nightly owner jobs stay within their limits: the owner mail fits the notify cap of the key service and the
learning run of the name check keeps below the rate of the vault daemon (both failed every night until 2026-11-08)."""

from awb import fplearn, ownerwait
from awb.tcp import keys


def test_the_mail_cap_is_the_notify_cap_of_the_key_service():
    assert ownerwait.MAIL_MAX == keys.NOTIFY_MAX


def test_a_short_list_goes_whole():
    lines = ["2 items wait on you", "a  first  [here]", "    awb one", "b  second  [here]"]
    assert ownerwait.fit(lines, 200) == "\n".join(lines) + "\n"


def test_a_long_list_is_cut_at_whole_items_and_counts_the_rest():
    lines = ["40 items wait on you"]
    for n in range(40):
        lines += ["item %02d  a step to take  [his terminal]" % n, "    awb command %02d with its arguments" % n]
    body = ownerwait.fit(lines, 1000)
    assert len(body) <= 1000
    kept = [ln for ln in body.splitlines() if ln.startswith("item ")]
    assert kept and kept[0] == lines[1]
    last = body.splitlines()[-2]
    assert last.startswith("    awb command"), "an item is never cut from its block"
    assert body.splitlines()[-1] == "... %d more lines left out, the console lists them all" % (40 - len(kept))


class Fake:
    def __init__(self):
        self.calls = 0

    def name_spans(self, text):
        self.calls += 1
        return []


def test_the_learning_run_waits_for_the_next_minute_after_its_quota():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    inner = Fake()
    paced = fplearn.Paced(inner, per_minute=3, clock=lambda: now[0], sleep=sleep)
    for _ in range(7):
        paced.name_spans("x")
        now[0] += 1.0
    assert inner.calls == 7
    assert len(slept) == 2 and all(0 < s <= 60 for s in slept)


def test_the_learning_run_paces_its_name_check():
    assert fplearn.CHECKS_A_MINUTE < 600
