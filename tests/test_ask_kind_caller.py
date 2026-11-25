"""Ask 2026-11-25-tcp-s2z5-4: the key token of an ask project's folder names its project as the caller, so its lab
run start and its calls to a test tenant are bound to it; a query project's token still names none."""
from awb import projects
from tests.test_ask_kind import granted, service  # noqa: F401 - the fixtures


def test_the_folder_token_of_an_ask_project_names_its_project(home, service):
    folder = granted(home)
    req = {"op": "call", "token": projects.read_key_token(folder)}
    assert service._caller_project(req, 4242) == folder.name


def test_the_folder_token_of_a_query_project_names_none(home, service):
    query = granted(home, "query")
    assert service._caller_project({"op": "call", "token": projects.read_key_token(query)}, 4242) is None
