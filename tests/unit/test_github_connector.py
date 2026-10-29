import pytest

from rag_authz.connectors.github import ConnectorError, GitHubConnector
from rag_authz.identity import MappingResolver
from rag_authz.model import Tuple, UpsertGroup, UpsertNode, doc
from rag_authz.oracle import Oracle
from tests.fakes import IDENTITIES, FakeGitHub

EXPECTED = {
    "olga@acme.test": {"api", "handbook", "website", "payroll"},
    "alice@acme.test": {"handbook", "website", "payroll"},
    "bob@acme.test": {"handbook", "website"},
    "carol@acme.test": {"api", "handbook", "website"},
    "eve@elsewhere.test": {"website"},
}


def snapshot_tuples(gh: FakeGitHub, **kw: object) -> tuple[frozenset[Tuple], GitHubConnector]:
    conn = GitHubConnector("acme", MappingResolver(IDENTITIES), client=gh.client(), **kw)  # type: ignore[arg-type]
    tuples: set[Tuple] = set()
    for ev in conn.snapshot():
        match ev:
            case UpsertNode(acl=acl):
                tuples |= acl.tuples()
            case UpsertGroup(group=g):
                tuples |= g.tuples()
    return frozenset(tuples), conn


def visible_repos(o: Oracle, user: str) -> set[str]:
    return {d.split("/")[2] for d in o.visible(user)}


def test_snapshot_matches_github_semantics() -> None:
    tuples, conn = snapshot_tuples(FakeGitHub())
    o = Oracle(tuples)
    for u, repos in EXPECTED.items():
        assert visible_repos(o, u) == repos, u
    assert conn.stats.repos == 4
    assert conn.stats.files == 5
    assert conn.stats.unmapped_principals == 1  # dave


def test_access_does_not_flow_up_from_child_team() -> None:
    tuples, _ = snapshot_tuples(FakeGitHub())
    o = Oracle(tuples)
    assert o.can_view("carol@acme.test", doc("github/acme/api/README.md"))
    assert not o.can_view("bob@acme.test", doc("github/acme/api/README.md"))
    # Parent-team access does flow down: give engineering the repo and carol keeps it.
    gh = FakeGitHub()
    gh.repos["api"].teams = ["engineering"]
    o2 = Oracle(snapshot_tuples(gh)[0])
    assert o2.can_view("carol@acme.test", doc("github/acme/api/README.md"))
    assert o2.can_view("bob@acme.test", doc("github/acme/api/README.md"))


def test_unmapped_outside_collaborator_cannot_be_claimed() -> None:
    tuples, _ = snapshot_tuples(FakeGitHub())
    assert any(t.subject is not None and t.subject.id == "~github/dave" for t in tuples)
    with pytest.raises(ValueError, match="not a valid query subject"):
        Oracle(tuples).can_view("~github/dave", doc("github/acme/api/README.md"))


def test_base_permission_read_opens_private_repos_to_members_only() -> None:
    gh = FakeGitHub(base_permission="read")
    o = Oracle(snapshot_tuples(gh)[0])
    assert visible_repos(o, "bob@acme.test") == {"api", "handbook", "website", "payroll"}
    assert visible_repos(o, "eve@elsewhere.test") == {"website"}


def test_invisible_base_permission_fails_closed() -> None:
    gh = FakeGitHub(base_permission=None)
    tuples, conn = snapshot_tuples(gh)
    assert conn.stats.base_permission is None
    assert visible_repos(Oracle(tuples), "bob@acme.test") == {"handbook", "website"}


def test_pagination_is_followed() -> None:
    gh = FakeGitHub(page_size=1)
    tuples, conn = snapshot_tuples(gh)
    assert conn.stats.repos == 4
    assert visible_repos(Oracle(tuples), "olga@acme.test") == EXPECTED["olga@acme.test"]


def test_empty_repo_and_content_toggle() -> None:
    gh = FakeGitHub()
    gh.repos["payroll"].files = {}
    _, conn = snapshot_tuples(gh)
    assert conn.stats.files == 4
    _, conn2 = snapshot_tuples(FakeGitHub(), include_content=False)
    assert conn2.stats.files == 0


def test_source_errors_raise() -> None:
    gh = FakeGitHub(org="other")
    with pytest.raises(ConnectorError):
        snapshot_tuples(gh)
