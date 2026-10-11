"""`awb write translate`: the German version and the Russian reading copy of an accepted draft in one step, with a
fake model. The model is reached through the Ask path only; without a readable key the command writes the prompt
file and stops with one line. Every fact is kept (`writing.keep` across languages) and the result passes
`awb write check` (the Russian copy is exempt).

Every planted text is invented.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import cli, translate, writing
from awb.tcp import ask
from tests import fixtures

SOURCE = """# Backup offer

I checked the quota in eu-de. Two ECS nodes cost 2.5 EUR per hour, 1,000 GB of OBS cost 20 EUR.

```
openstack server list --name tcp-q7m4
```
"""
GERMAN = """# Angebot für das Backup

Ich habe die Quota in eu-de geprüft. 2 ECS-Knoten kosten 2,5 EUR pro Stunde, 1.000 GB OBS kosten 20 EUR.

```
openstack server list --name tcp-q7m4
```
"""
RUSSIAN = """# Предложение по бэкапу

Я проверил квоту в eu-de. 2 узла ECS стоят 2,5 EUR в час — 1.000 GB OBS стоят 20 EUR. Мы готовы.

```
openstack server list --name tcp-q7m4
```
"""


class FakeModel:
    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.bodies: list[dict] = []

    def __call__(self, body: dict, key):
        self.bodies.append(body)
        lang = next(k for k, name in translate.NAMES.items() if "into %s" % name in body["system"])
        return {"content": [{"type": "text", "text": self.answers[lang]}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 200}}


@pytest.fixture
def draft(home, tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(ask, "USAGE_FILE", tmp_path / "usage.json")
    root = tmp_path / "tcp-q7m4"
    (root / "deliverables").mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope\n", encoding="utf-8")
    f = root / "deliverables" / "offer.md"
    f.write_text(SOURCE, encoding="utf-8")
    return f


def use(monkeypatch, model) -> None:
    monkeypatch.setattr(translate, "default_sender", lambda: (model, "test-key") if model is not None else None)


def test_german_and_russian_in_one_step_with_every_fact_kept(draft, monkeypatch, capsys):
    model = FakeModel({"de": GERMAN, "ru": RUSSIAN})
    use(monkeypatch, model)
    assert cli.main(["write", "translate", str(draft), "--to", "de", "--to", "ru"]) == 0
    out = capsys.readouterr().out
    assert (draft.parent / "offer.de.md").read_text(encoding="utf-8") == GERMAN
    assert (draft.parent / "offer.ru.md").read_text(encoding="utf-8") == RUSSIAN
    assert "German  numbers  same, 4 in both" in out and "German  code blocks  same, 1 in both" in out
    assert "German  check  0 blocking" in out
    assert "Russian  check  exempt: the Russian reading copy is for him alone" in out
    assert "offer" not in out                                          # never the file name
    # the model got the accepted text and the rules, through the Ask path's model and budget
    assert [b["messages"][0]["content"] for b in model.bodies] == [SOURCE, SOURCE]
    assert all(b["model"] == ask.MODEL for b in model.bodies)
    assert "Keep every number as digits" in model.bodies[0]["system"] and "never we" in model.bodies[0]["system"]
    assert ask.budget_left()[1] == ask.DAILY_TOKENS - 600


def test_a_lost_number_refuses_and_nothing_lands_next_to_the_source(draft, monkeypatch, capsys):
    use(monkeypatch, FakeModel({"de": GERMAN.replace("2,5 EUR", "3 EUR")}))
    assert cli.main(["write", "translate", str(draft), "--to", "de"]) == 1
    out = capsys.readouterr().out
    assert "German  numbers  differ" in out and "German  refused, the model's text is in the prompt folder" in out
    assert not (draft.parent / "offer.de.md").exists()
    assert (draft.parent.parent / "reviews" / ".translate" / "offer.de.refused.md").is_file()
    assert "2.5" not in out and "3 EUR" not in out


@pytest.mark.parametrize("change", [
    ("eu-de", "eu-nl"),                                       # an identifier
    ("--name tcp-q7m4", "--name tcp-q7m5"),                   # a code block
    ("ECS-Knoten", "Server"),                                 # an identifier inside a German compound
])
def test_a_changed_identifier_or_code_block_refuses(draft, monkeypatch, capsys, change):
    use(monkeypatch, FakeModel({"de": GERMAN.replace(*change)}))
    assert cli.main(["write", "translate", str(draft), "--to", "de"]) == 1
    assert not (draft.parent / "offer.de.md").exists()


def test_the_german_result_must_pass_the_writing_check(draft, monkeypatch, capsys):
    use(monkeypatch, FakeModel({"de": GERMAN.replace("Ich habe", "Wir haben")}))
    assert cli.main(["write", "translate", str(draft), "--to", "de"]) == 1
    out = capsys.readouterr().out
    assert 'German  we-in-doc  3  a text in his name speaks as I: no "wir"' in out and "German  refused" in out


def test_a_source_that_is_not_accepted_is_never_sent(draft, monkeypatch, capsys):
    model = FakeModel({"de": GERMAN})
    use(monkeypatch, model)
    draft.write_text(SOURCE.replace("I checked", "We checked"), encoding="utf-8")
    assert cli.main(["write", "translate", str(draft), "--to", "de"]) == 1
    assert "the source is not accepted: 1 blocking tells" in capsys.readouterr().err
    assert model.bodies == [] and not (draft.parent / "offer.de.md").exists()


def test_a_registered_name_is_never_sent(draft, monkeypatch, capsys):
    model = FakeModel({"de": GERMAN})
    use(monkeypatch, model)
    draft.write_text(SOURCE.replace("I checked the quota", "I checked the quota of %s" % fixtures.ORG_FORMS[0]),
                     encoding="utf-8")
    assert cli.main(["write", "translate", str(draft), "--to", "de"]) == 2
    err = capsys.readouterr().err
    assert "hits of the name check" in err and model.bodies == []
    fixtures.assert_no_fixture_name(err, "the translate message")


def test_without_a_model_path_the_prompt_file_is_written_and_one_line_said(draft, monkeypatch, capsys):
    use(monkeypatch, None)
    assert cli.main(["write", "translate", str(draft), "--to", "de", "--to", "ru"]) == 3
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("no model path for this user: the prompts are in "
                                                   "reviews/.translate/ as <file>.de.prompt.md, <file>.ru.prompt.md")
    prompt = (draft.parent.parent / "reviews" / ".translate" / "offer.de.prompt.md").read_text(encoding="utf-8")
    assert "into German" in prompt and SOURCE.strip() in prompt and "--verify" in prompt
    assert not (draft.parent / "offer.de.md").exists()


def test_verify_proves_the_translation_the_session_wrote(draft, monkeypatch, capsys):
    use(monkeypatch, None)
    assert cli.main(["write", "translate", str(draft), "--to", "de", "--verify"]) == 1
    assert "the German version is not there" in capsys.readouterr().out
    (draft.parent / "offer.de.md").write_text(GERMAN, encoding="utf-8")
    assert cli.main(["write", "translate", str(draft), "--to", "de", "--verify"]) == 0
    assert "German  proved next to the source" in capsys.readouterr().out
    (draft.parent / "offer.de.md").write_text(GERMAN.replace("20 EUR", "zwanzig EUR"), encoding="utf-8")
    assert cli.main(["write", "translate", str(draft), "--to", "de", "--verify"]) == 1
    assert (draft.parent / "offer.de.md").is_file()                    # the session's file stays to be fixed


def test_the_ask_path_is_the_only_model_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, "KEY_FILE", tmp_path / "missing.key")
    assert translate.default_sender() is None
    key = tmp_path / "anthropic.key"
    key.write_text("planted-key\n", encoding="utf-8")
    monkeypatch.setattr(ask, "KEY_FILE", key)
    assert translate.default_sender() == (ask.post, "planted-key")


def test_usage_errors(draft, monkeypatch, capsys):
    use(monkeypatch, FakeModel({"en": SOURCE}))
    assert cli.main(["write", "translate", str(draft), "--to", "en"]) == 2
    assert "the source is already in English" in capsys.readouterr().err
    assert cli.main(["write", "translate", str(draft), "--to", "fr"]) == 2
    assert cli.main(["write", "translate", str(draft.parent / "missing.md"), "--to", "de"]) == 2
    assert cli.main(["write", "translate", str(draft)]) == 2


def test_the_target_name_and_the_language_guess():
    assert translate.target_path(Path("a/offer.md"), "de") == Path("a/offer.de.md")
    assert translate.target_path(Path("a/notes"), "ru") == Path("a/notes.ru.md")
    assert writing.language_of(SOURCE) == "en"
    assert writing.language_of(GERMAN) == "de"
    assert writing.language_of(RUSSIAN) == "ru"


def test_keep_across_languages_reads_a_decimal_comma_and_a_compound():
    same = writing.keep("It costs 2.5 EUR and 1,000 GB on TCP.", "Es kostet 2,5 EUR und 1.000 GB auf dem TCP-Konto.",
                        across=True)
    assert all(k.same for k in same) and [k.kind for k in same] == list(writing.KEEP_ACROSS)
    differ = writing.keep("It costs 2.5 EUR.", "Es kostet 2,6 EUR.", across=True)
    assert not differ[0].same
    # within one language the comma stays a separator and negations are compared
    assert [k.kind for k in writing.keep("a", "b")] == list(writing.KEEP_KINDS)
