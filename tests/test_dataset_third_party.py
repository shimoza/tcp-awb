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


def test_windows_out_of_support_and_licence_terms_stay_out_and_current_windows_stays():
    assert dataset.third_party({"statement": "A Windows Server 2008 R2 guest needs VirtIO drivers injected.", "tags": []})
    assert dataset.third_party({"statement": "The 2012 R2 image boots after the driver change.", "tags": []})
    assert dataset.third_party({"statement": "SPLA covers the licence of a hosted Windows guest.", "tags": []})
    assert not dataset.third_party({"statement": "TCP offers a Windows Server 2022 image with the licence included.",
                                    "tags": ["ims"]})
