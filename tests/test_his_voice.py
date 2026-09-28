"""Package 6, writing in his voice: the reported counts of T-52 and T-53 (fragments, comma splices, headings
written as verdicts, connectors outside his set, the I rate of a mail), the fact keeper of the voice pass (V-15,
`awb write keep`) and the drafting skill of T-51 with its place in the seal.

Every planted text is invented. Each count is shown to fire on a planted case and to stay quiet on the near miss,
so that a count which finds nothing cannot pass.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from awb import cli, gate, writing
from tests import fixtures
from tests.test_seal import SETUP, VERIFY, commands, fakebin, run_script  # noqa: F401  (fakebin is a fixture)

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "seal" / "work-claude" / "skills" / "drafting" / "SKILL.md"
WORK_CLAUDE = REPO / "seal" / "work-claude" / "CLAUDE.md"


def reported(text: str, cls: str, mode: str = "doc") -> list[int]:
    tells, _ = writing.check_text(text, mode=mode)
    assert not [t for t in tells if t.cls == cls and t.blocking], "%s must never block" % cls
    return [t.line for t in tells if t.cls == cls]


# --------------------------------------------------------------------------- the reported counts


def test_fragments_are_counted_in_running_prose_only():
    text = ("The quota is fine.\n\nNo downtime. No surprises.\n\nThe catch?\n\n- No downtime.\n- A second item.\n\n"
            "Check the quota. Yes. Both options work. Please send the numbers.\n")
    assert reported(text, "fragment") == [3, 3, 5]
    _, m = writing.check_text(text)
    assert m["fragments"] == 3 and m["fragment_share"] == round(100.0 * 3 / m["sentences"], 1)
    for clean in ("Costs stay low.", "I agree.", "The flavor is sold out.", "They sell it in two regions.",
                  "Thanks.\n\nBest regards", "The job failed twice."):
        assert reported("%s\n" % clean, "fragment") == [], clean


def test_comma_splices_and_what_is_not_one():
    for splice in ("The quota is fine, it is the flavor that fails.", "I checked the quota, it is fine today.",
                   "We tested it, we removed it after the test.", "The node is up, you can log in now."):
        assert reported(splice + "\n", "comma-splice") == [1], splice
    for fine in ("If the quota is fine, it is the flavor that fails.", "Yes, it works in both regions.",
                 "We create the VPC, then we attach the subnet.", "The flavor, I think, is sold out.",
                 "It works in eu-de, I think.", "In short, it is fine for the test.",
                 "The cluster runs in eu-de, which is the main region.", "The nodes, the disks and the EIP stay."):
        assert reported(fine + "\n", "comma-splice") == [], fine
    _, m = writing.check_text("The quota is fine, it is the flavor that fails.\n")
    assert m["comma_splices"] == 1


def test_headings_written_as_verdicts():
    verdicts = ["CCE is the right choice", "Why CCE wins", "Recommendation: CCE with two node pools",
                "Costs stay low.", "The bottom line", "This will not scale"]
    topics = ["Network", "Next steps", "Migration plan", "What is in scope", "How the backup works",
              "2. Costs per month", "Open questions"]
    text = "\n\n".join("## %s" % h for h in verdicts + topics) + "\n"
    lines = reported(text, "verdict-heading")
    assert lines == [1 + 2 * i for i in range(len(verdicts))]
    _, m = writing.check_text(text)
    assert (m["verdict_headings"], m["headings"]) == (len(verdicts), len(verdicts) + len(topics))
    setext = "CCE is the right choice\n=======================\n\nThe text.\n"
    assert reported(setext, "verdict-heading") == [1]


def test_front_matter_is_not_a_heading():
    text = "---\nname: probe\ndescription: The quota is fine for the test.\n---\n\n# Topic\n\nThe text is here.\n"
    _, m = writing.check_text(text)
    assert m["headings"] == 1 and reported(text, "verdict-heading") == []


def test_connectors_outside_his_set_are_reported_with_the_rule_word():
    rules = writing.load_rules()
    assert rules.connectors == ("and", "so", "then", "but", "also", "because")
    assert "therefore" in rules.other_connectors
    assert not set(rules.other_connectors) & set(rules.banned_words)
    tells, m = writing.check_text("The node stops. Therefore the job fails.\n\nAs a result the test waits.\n")
    found = [t for t in tells if t.cls == "connector"]
    assert [t.line for t in found] == [1, 3] and not any(t.blocking for t in found)
    assert '"therefore"' in found[0].hint and "use and, so, then, but, also or because" in found[0].hint
    assert m["other_connectors_per_1000"] > 0
    tells, m = writing.check_text("The node stops, so the job fails. Then I restart it because the test waits.\n")
    assert [t for t in tells if t.cls == "connector"] == []
    assert m["other_connectors_per_1000"] == 0 and m["his_connectors_per_1000"] > 0
    # a banned connector blocks as a banned word and is not reported twice
    tells, _ = writing.check_text("However the job fails.\n")
    assert [t.cls for t in tells] == ["banned-word"]


def test_the_i_rate_of_a_mail():
    impersonal = " ".join(["The cluster runs in two zones and the quota is fine for the test."] * 5)
    assert reported(impersonal + "\n", "i-rate", mode="mail") == [1]
    assert reported(impersonal + "\n", "i-rate", mode="doc") == []
    personal = " ".join(["I checked the cluster and my numbers show the quota is fine."] * 5)
    assert reported(personal + "\n", "i-rate", mode="mail") == []
    assert reported("The quota is fine.\n", "i-rate", mode="mail") == []           # too short to judge


def test_the_new_counts_are_reported_never_blocking_and_quote_at_most_one_word():
    new = ("fragment", "comma-splice", "verdict-heading", "connector", "i-rate")
    assert set(new) <= set(writing.REPORTED) and not set(new) & set(writing.BLOCKING)
    text = ("# CCE is the right choice\n\nNo downtime. The quota is fine, it is the flavor that fails. "
            "Therefore the node stops. " + " ".join(["The cluster runs in two zones for the test."] * 5) + "\n")
    tells, m = writing.check_text(text, mode="mail")
    assert {t.cls for t in tells} >= set(new)
    assert m["blocking"] == 0
    for t in tells:
        assert len(re.findall(r'"([^"]*)"', t.hint)) <= 1
    lines = writing.metric_lines(m)
    for must in ("fragments  1", "comma splices  1", "headings written as verdicts  1 of 1",
                 "his connectors per 1000 words", "other connectors per 1000 words"):
        assert any(line.startswith(must) for line in lines), must


# --------------------------------------------------------------------------- the fact keeper of the voice pass

BEFORE = """# Sizing

The cluster runs 2 nodes in eu-de on `s3.large.2`. The flavor is not supported in eu-nl.
One EIP is enough. The price is 0.085 EUR per hour, see https://example.invalid/prices for the source.

```
kubectl get nodes
```

1. Create the VPC.
2. Attach the EIP.
"""

VOICED = """# Sizing

I run the cluster on two nodes in eu-de with `s3.large.2`. The flavor is unsupported in eu-nl.
1 EIP is enough and the price is 0.085 EUR per hour (source: https://example.invalid/prices).

```
kubectl get nodes
```

- Create the VPC.
- Attach the EIP.
"""


def kinds_that_differ(before: str, after: str) -> list[str]:
    return [k.kind for k in writing.keep(before, after) if not k.same]


def test_a_voice_pass_that_keeps_the_facts_passes():
    assert kinds_that_differ(BEFORE, BEFORE) == []
    assert kinds_that_differ(BEFORE, VOICED) == []
    assert kinds_that_differ("The flavor is not available.\n", "The flavor isn't available.\n") == []
    facts = writing.facts_of(BEFORE)
    assert {v for v, _ in facts["identifier"]} >= {"eu-de", "eu-nl", "s3.large.2", "EIP", "EUR", "VPC",
                                                   "https://example.invalid/prices"}
    assert {v for v, _ in facts["number"]} == {"2", "0.085"}          # list numbers are structure


@pytest.mark.parametrize("change,kind", [
    (("0.085", "0.095"), "number"),
    (("two nodes", "three nodes"), "number"),
    (("eu-nl", "eu-ch2"), "identifier"),
    (("`s3.large.2`", "`s3.large.4`"), "identifier"),
    (("EIP is", "ELB is"), "identifier"),
    (("https://example.invalid/prices", "https://example.invalid/other"), "identifier"),
    (("unsupported", "supported"), "negation"),
    (("kubectl get nodes", "kubectl get pods"), "code block"),
])
def test_each_changed_fact_blocks(change, kind):
    after = VOICED.replace(*change)
    assert after != VOICED
    assert kinds_that_differ(BEFORE, after) == [kind]


def test_one_pairs_with_1_only_when_the_other_text_has_it():
    assert kinds_that_differ("One node is enough.\n", "1 node is enough.\n") == []
    assert kinds_that_differ("A node is enough.\n", "1 node is enough.\n") == ["number"]


def test_the_report_names_kinds_counts_and_lines_never_a_value(tmp_path, capsys):
    before, after = tmp_path / "before.md", tmp_path / "after.md"
    before.write_text(BEFORE, encoding="utf-8")
    after.write_text(VOICED.replace("eu-nl", "eu-ch2").replace("0.085", "0.095"), encoding="utf-8")
    assert cli.main(["write", "keep", str(before), str(after)]) == 1
    out = capsys.readouterr().out
    assert "numbers  differ: 2 before, 2 after; see BEFORE line 4; see AFTER line 4" in out
    assert "identifiers  differ" in out and "negations  same" in out and "code blocks  same, 1 in both" in out
    assert "2 kinds changed by the voice pass" in out
    for value in ("eu-nl", "eu-ch2", "0.085", "0.095"):
        assert value not in out
    after.write_text(VOICED.replace("unsupported", "supported"), encoding="utf-8")
    assert cli.main(["write", "keep", str(before), str(after)]) == 1
    assert "negations  differ: 1 before, 0 after; see BEFORE line 3\n" in capsys.readouterr().out
    after.write_text(VOICED, encoding="utf-8")
    assert cli.main(["write", "keep", str(before), str(after)]) == 0
    assert "every number, identifier, negation and code block is kept" in capsys.readouterr().out
    assert cli.main(["write", "keep", str(before), str(tmp_path / "missing.md")]) == 2


# --------------------------------------------------------------------------- the drafting skill


def test_the_drafting_skill_is_written_the_way_it_asks(tmp_path):
    text = SKILL.read_text(encoding="utf-8")
    head = re.match(r"---\nname: drafting\ndescription: (.+)\n---\n", text)
    assert head and len(head.group(1)) < 1024
    tells, _ = writing.check_text(text, mode="doc")
    assert [t for t in tells if t.blocking] == []
    assert [t for t in tells if t.cls in ("verdict-heading", "fragment", "comma-splice", "connector")] == []
    reg = tmp_path / "register.tsv"
    reg.write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    assert gate.scan_files([SKILL], reg) == []
    fixtures.assert_no_fixture_name(text, "the drafting skill")
    # the steps of T-51 and the order of T-56 and V-15
    for must in ("first line", "First person singular", "No commitment", "No invented names", "earlier mail",
                 "German readers", "length budget", "nouns and verbs", "facts first, voice second",
                 "awb review claims", "awb write keep BEFORE AFTER", "awb write check", "awb voice learn",
                 "awb price", "before-voice.md", "Cut to the budget as a separate last step"):
        assert must in text, must
    assert text.index("awb write keep") < text.index("awb write check FILE") < text.index("Cut to the budget")
    assert "drafting skill" in WORK_CLAUDE.read_text(encoding="utf-8")


def test_the_seal_installs_the_skill_owned_by_root_and_immutable(fakebin):
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    cmds = commands(res.stdout)
    skill = [c for c in cmds if "skills/drafting/SKILL.md" in c]
    assert any(re.match(r"^\+ install -o root -g awb -m 644 \S+/work-claude/skills/drafting/SKILL\.md "
                        r"\S+/\.claude/skills/drafting/SKILL\.md$", c) for c in skill), skill
    assert any(c.startswith("+ chattr +i ") and c.endswith("/.claude/skills/drafting/SKILL.md") for c in skill)
    folders = [i for i, c in enumerate(cmds) if c.startswith("+ install -d -o root -g awb -m 755 ")]
    assert len(folders) == 1
    assert re.match(r"^\+ install -d -o root -g awb -m 755 (\S+)/\.claude/skills \1/\.claude/skills/drafting$",
                    cmds[folders[0]])
    locks = [c for c in cmds if c.startswith("+ chattr +i ") and c.rstrip().endswith(("/skills", "/skills/drafting"))]
    assert len(locks) == 2
    # the folders are unlocked before anything is written into them and locked after the file
    unlock = max(i for i, c in enumerate(cmds) if c.startswith("+ chattr -i ")
                 and c.endswith(("/.claude/skills  # if present", "/.claude/skills/drafting  # if present")))
    write = next(i for i, c in enumerate(cmds) if "install -o root -g awb -m 644" in c and "SKILL.md" in c)
    lock = min(i for i, c in enumerate(cmds) if c in locks)
    assert unlock < folders[0] < write < lock
    checks = run_script(VERIFY, ["--dry-run"], fakebin).stdout
    assert "the client file skills/drafting/SKILL.md of the work user is the one of the repository" in checks
    assert "the skill folder skills/drafting of the work user is immutable" in checks
