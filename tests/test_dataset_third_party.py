"""The owner's word of 2026-11-25: the public facts dataset carries TCP only, never the work with a vendor such as
Veeam. A fact whose statement or tag names a product of rules/dataset-third-party.txt stays out."""
from awb.tcp import dataset


def test_a_veeam_fact_is_third_party_and_a_tcp_fact_is_not():
    assert dataset.third_party({"statement": "Veeam Backup & Replication 13 consumes licences only to back up data.",
                                "tags": ["backup"]})
    assert dataset.third_party({"statement": "An OBS bucket keeps versions.", "tags": ["veeam"]})
    assert not dataset.third_party({"statement": "An OBS bucket keeps versions after a delete.", "tags": ["obs"]})
    assert not dataset.third_party({"statement": "The veeamless setup uses CBR.", "tags": []})


def test_the_list_holds_veeam_and_no_comment_line():
    names = dataset.third_party_names()
    assert "Veeam" in names and all(not n.startswith("#") for n in names)
