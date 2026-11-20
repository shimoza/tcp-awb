"""tcp-ui5o: test-10503 is a lab tenant like test-10491 (the owner's decision of 2026-11-20); the list stays closed.

The logins here are invented and built from pieces at run time.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from awb import labclient, projects, rulesync
from awb.tcp import keys, labadmin, xchg

LAB = "test-10491"
LAB2 = "test-10497"
LAB3 = "test-10503"
NOT_LAB = "test-10498"
UNKNOWN = "test-10599"
LOGIN = {"domain": "lab-account-" + "fixture", "user": "lab-admin-" + "fixture",
         "password": "-".join(("Invented", "Admin", "4711"))}


def _svc(line: str, readonly: tuple = ()):
    tenants = {a: {"admin": dict(LOGIN)} for a in (LAB, LAB2, LAB3)}
    tenants[NOT_LAB] = {}
    return SimpleNamespace(lock=threading.Lock(), tenants=tenants, settings={"lab_admin": line},
                           _readonly=lambda alias: {} if alias in readonly else None)


def test_10503_is_in_the_closed_list_of_lab_tenants():
    assert labadmin.ALLOWED == (LAB, LAB2, LAB3)
    assert labadmin.parse(LAB3) == [LAB3]
    assert labadmin.parse("%s %s %s" % (LAB, LAB2, LAB3)) == [LAB, LAB2, LAB3]
    assert labadmin.parse("%s, %s" % (LAB3, LAB)) == [LAB3, LAB]
    assert labadmin.of_settings({"lab_admin": "%s %s" % (LAB, LAB3)}) == [LAB, LAB3]
    for bad in (UNKNOWN, NOT_LAB, "%s %s" % (LAB3, UNKNOWN), "%s %s" % (LAB3, LAB3), "test-*", "*"):
        assert labadmin.parse(bad) is None, bad


def test_10503_passes_keys_conf_and_an_unknown_alias_is_refused(tmp_path):
    conf = tmp_path / "keys.conf"
    conf.write_text("lab_admin = %s %s %s\n" % (LAB, LAB2, LAB3), encoding="utf-8")
    assert xchg.read_settings(conf)["lab_admin"] == "%s %s %s" % (LAB, LAB2, LAB3)
    for bad in (UNKNOWN, "%s %s" % (LAB3, UNKNOWN), "%s %s" % (LAB3, LAB3)):
        conf.write_text("bucket_tenant = test-1\nlab_admin = %s\n" % bad, encoding="utf-8")
        with pytest.raises(xchg.XchgError) as err:
            xchg.read_settings(conf)
        assert str(err.value) == ("line 2 of keys.conf: lab_admin names the lab tenants %s and %s and %s only, "
                                  "each once" % (LAB, LAB2, LAB3))


def test_10503_takes_a_lab_run_and_an_unknown_alias_does_not():
    tenant, login = labadmin._tenant(_svc("%s %s" % (LAB, LAB3)), LAB3)
    assert login == LOGIN and tenant["admin"] == LOGIN
    with pytest.raises(keys.Refused) as err:
        labadmin._tenant(_svc("%s %s" % (LAB, LAB3)), UNKNOWN)
    assert str(err.value) == labadmin.TENANT_LINE % (UNKNOWN, " and ".join(labadmin.ALLOWED))
    assert LAB3 in str(err.value)
    with pytest.raises(keys.Refused) as err:
        labadmin._tenant(_svc(LAB), LAB3)
    assert str(err.value) == labadmin.SETTING_LINE % (LAB3, " ".join(labadmin.ALLOWED))
    with pytest.raises(keys.Refused):
        labadmin._tenant(_svc("%s %s" % (LAB, LAB3), readonly=(LAB3,)), LAB3)


def test_the_texts_name_every_lab_tenant():
    helps = [" ".join(p.format_help().split()) for p in (labclient._parser("start"), labclient._parser("end"),
                                                         labclient._obs_parser("put"), labclient._obs_parser("get"))]
    for alias in labadmin.ALLOWED:
        assert alias in projects.LAB_RUN_RULE
        for text in helps:
            assert alias in text
    assert projects.LAB_RUN_RULE.startswith(projects.LAB_RUN_RULE_HEAD)


def test_sync_gives_an_older_lab_run_rule_test_10503(tmp_path):
    text = projects._claude("tcp-ab2c", "project", projects.NO_CUSTOMER)
    old = text.replace(projects.LAB_RUN_RULE_HEAD, projects.LAB_RUN_RULE_OLD_HEAD)
    assert old != text
    (tmp_path / "CLAUDE.md").write_text(old, encoding="utf-8")
    assert rulesync.sync_project(tmp_path) == ["CLAUDE.md"]
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == text
    assert rulesync.sync_project(tmp_path) == []
