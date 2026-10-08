"""The code view of the structured patterns (T12): dotted references in code files are not web addresses. Every
example is an invented fixture value."""
from __future__ import annotations

import pytest

from awb import patterns
from tests import fixtures


def kinds(text: str) -> list[str]:
    return [s.cls for s in patterns.find_structured(text)]


# --------------------------------------------------------------------------- the code view (T12)

_TF_REFS = [
    "opentelekomcloud_vpc_v1.vpc.id", "opentelekomcloud_vpc_v1.vpc.cidr", "module.loadbalancer.listener_port",
    "data.opentelekomcloud_compute_availability_zone_v2.az.name", "data.opentelekomcloud_images_image_v2.os.id",
    "opentelekomcloud_networking_secgroup_v2.web.id", "aws_instance.web.private_ip", "module.net.subnet.id",
    "local.cfg.app.name", "var.settings.de", "self.network.io", "module.dns.zone.com",
    "opentelekomcloud_lb_loadbalancer_v2.lb.vip_address", "opentelekomcloud_compute_instance_v2.vm.access_ip_v4",
    "opentelekomcloud_rds_instance_v3.db.private_ips", "module.cce.cluster.id", "local.tags.team",
    "var.bucket.name", "opentelekomcloud_obs_bucket.store.bucket_domain_name", "module.vpn.gateway.eu",
]


def _tf(extra: str = "") -> str:
    lines = ['resource "opentelekomcloud_ecs_instance_v1" "vm" {']
    lines += ["  ref_%02d = %s" % (i, r) for i, r in enumerate(_TF_REFS)]
    lines += ["  list = [%s]" % _TF_REFS[0], "  call = element(%s, 0)" % _TF_REFS[1], extra, "}"]
    return "\n".join(lines) + "\n"


def test_code_view_takes_no_dotted_reference_of_terraform_for_an_address():
    text = _tf()
    assert len(_TF_REFS) == 20
    assert "url" in kinds(text)                      # the plain view reads several of them as addresses
    assert [s for s in patterns.find_structured(text, code=True) if s.cls == "url"] == []


def test_code_view_keeps_mail_scheme_url_and_quoted_host():
    text = _tf('  owner = "ops@zyxwo-logistik.de"\n  url = "https://portal.zyxwo-logistik.de/x"\n'
               '  host = "api.zyxwo-logistik.de"')
    found = [(s.cls, text[s.start:s.end]) for s in patterns.find_structured(text, code=True)]
    assert ("mail", "ops@zyxwo-logistik.de") in found
    assert ("url", "https://portal.zyxwo-logistik.de/x") in found
    assert ("url", "api.zyxwo-logistik.de") in found


@pytest.mark.parametrize("name,code", [("main.tf", True), ("vars.tfvars", True), ("a.py", True), ("x.yml", True),
                                       ("n.md", False), ("n.txt", False), ("Main.TF", True)])
def test_code_files_are_known_by_their_suffix(name, code):
    assert patterns.is_code_file(name) is code


def test_a_markdown_file_with_the_same_references_keeps_its_url_hits(register_path, tmp_path):
    from awb import check

    md = tmp_path / "notes.md"
    md.write_text(_tf(), encoding="utf-8")
    tf = tmp_path / "main.tf"
    tf.write_text(_tf(), encoding="utf-8")
    assert any(h["cls"] == "url" for h in check.check_file(md, register_path, code=True))
    assert check.check_file(tf, register_path, code=True) == []
    assert any(h["cls"] == "url" for h in check.check_file(tf, register_path))     # only on request


def test_code_view_still_finds_a_mail_and_a_registered_name_in_a_tf_file(register_path, tmp_path):
    from awb import check

    tf = tmp_path / "main.tf"
    tf.write_text(_tf('  owner = "tobias.beispielmann@%s"\n  # for %s'
                      % (fixtures.CUSTOMER_DOMAIN, fixtures.CUSTOMER_FORMS[0])), encoding="utf-8")
    classes = [h["cls"] for h in check.check_file(tf, register_path, code=True)]
    assert "mail" in classes and "name" in classes and "url" not in classes


def test_awb_check_reads_a_tf_file_in_the_code_view(register_path, tmp_path, capsys):
    from awb import check

    tf = tmp_path / "main.tf"
    tf.write_text(_tf(), encoding="utf-8")
    assert check.main(["--register", str(register_path), str(tf)]) == 0
    md = tmp_path / "main.md"
    md.write_text(_tf(), encoding="utf-8")
    assert check.main(["--register", str(register_path), str(md)]) == 1
