"""K1: a session reads the lab bucket under the prefixes of its own project only (the layout of awb/bucket_layout.py)."""
from awb.tcp import keys
from tests.test_keys_review import buckets, lab_project, quiet, svc  # noqa: F401


def ask(svc, project, **req):
    return keys.request(svc.call_path, dict({"op": "obs", "project": project.code}, **req))


def test_a_read_under_the_own_prefix_is_signed_and_one_under_another_prefix_is_refused(svc, buckets, lab_project):
    lab, _ = buckets
    own = "%s/in/a.md" % lab_project.code
    lab.objects[own] = b"The plan.\n"
    lab.objects["tcp-zz99/in/a.md"] = b"Another plan.\n"
    assert ask(svc, lab_project, method="HEAD", key=own)["exists"]
    assert ask(svc, lab_project, method="GET", key=own)["ok"]
    for key in ("tcp-zz99/in/a.md", "tcp-zz99/from-session/a.md", "a.md"):
        for method in ("GET", "HEAD"):
            a = ask(svc, lab_project, method=method, key=key)
            assert not a["ok"] and "outside inbox/" in a["error"] and key not in a["error"]


def test_a_listing_without_a_prefix_is_refused_and_one_with_the_own_prefix_is_signed(svc, buckets, lab_project):
    lab, _ = buckets
    lab.objects["%s/in/a.md" % lab_project.code] = b"The plan.\n"
    for prefix in ("", "tcp-zz99/in/", "%s/" % lab_project.code, "tcp-"):
        a = ask(svc, lab_project, method="LIST", prefix=prefix)
        assert not a["ok"] and "own folders only" in a["error"]
    a = ask(svc, lab_project, method="LIST", prefix="%s/in/" % lab_project.code)
    assert a["ok"] and [o["name"] for o in a["objects"]] == ["a.md"]
