"""Ask 2026-11-25-tcp-s2z5-3: the result of a side session is a background agent's result: a host name or an
address of its work passes awb side take. DMZ 4 (B-088): a registered name or a mail passes it too, the result is
taken as written."""
import pytest

from awb import side
from tests import fixtures
from tests.test_side import _wait, proj  # noqa: F401 - the fixture


@pytest.fixture(autouse=True)
def side_register(home):
    """The output checks use the temporary fixture register, never a host service."""
    return home.register


def test_a_vendor_host_name_in_the_result_passes_take(proj):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    (proj / ".side" / "audit" / side.RESULT).write_text("Hosts that could not be fetched:\n- www.v2v-converter-tools.com\n"
                                       "- https://docs.v2v-converter-tools.com/guide.html\n- 10.0.0.12\n")
    side.take("audit", cwd=proj)
    assert "www.v2v-converter-tools.com" in (proj / "notes" / "side-audit.md").read_text()


@pytest.mark.parametrize("bad", [fixtures.CUSTOMER_FORMS[0], "contact: someone@example.com"])
def test_a_name_or_a_mail_still_withholds_the_result(proj, bad):
    side.start("audit", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "audit")
    (proj / ".side" / "audit" / side.RESULT).write_text("result line\n%s\n" % bad)
    side.take("audit", cwd=proj)
    assert bad in (proj / "notes" / "side-audit.md").read_text()
