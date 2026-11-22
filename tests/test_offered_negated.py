"""A review may say a service is gone: a sentence that names it as no longer offered is not a finding (tcp-ui5o)."""
from __future__ import annotations

from awb import offered


def _gone():
    catalog = offered.load()
    names = offered.mentions("We use Bare Metal Server for the database.", catalog)
    assert names, "the not-offered list must know Bare Metal Server for this test"
    return catalog, names[0]


def test_a_sentence_that_says_the_service_is_no_longer_offered_passes():
    catalog, name = _gone()
    text = "The 2018 guide named Bare Metal Server. Bare Metal Server is no longer offered on TCP."
    assert name in offered.mentions(text, catalog, skip_negated=True)   # the first sentence names it without the negation
    assert offered.mentions("Bare Metal Server is no longer offered on TCP.", catalog, skip_negated=True) == []


def test_a_proposal_of_the_service_is_still_found():
    catalog, name = _gone()
    text = "Put the database on Bare Metal Server. Bare Metal Server is no longer offered on TCP."
    assert name in offered.mentions(text, catalog, skip_negated=True)
    assert name in offered.mentions("Put the database on Bare Metal Server.", catalog, skip_negated=True)


def test_without_the_flag_every_mention_counts_as_before():
    catalog, name = _gone()
    assert name in offered.mentions("Bare Metal Server is no longer offered on TCP.", catalog)
