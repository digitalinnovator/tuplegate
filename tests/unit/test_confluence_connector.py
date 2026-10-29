from __future__ import annotations

import httpx
import pytest

from rag_authz import ids
from rag_authz.authz.base import Consistency
from rag_authz.authz.memory import MemoryBackend
from rag_authz.connectors.confluence import ConfluenceConnector, storage_to_text
from rag_authz.index.memory import MemoryIndex
from rag_authz.model import Tuple, UpsertGroup, UpsertNode
from rag_authz.oracle import Oracle
from rag_authz.sync import SyncEngine
from rag_authz.testing import HashingEmbedder
from tests.fakes_confluence import FakeConfluence

SITE = "https://acme.atlassian.net"
STAND_IN = {"all-licensed-users": "g-users"}
EXPECTED = {
    "olga@acme.test": {"P1", "P5", "P6"},
    # in g-legal (P2's restriction) but without space access: restrictions never grant
    "alice@acme.test": {"P5", "P6"},
    # blocked by P2's restriction, so also from P3 below it; passes F1's restriction
    "bob@acme.test": {"P1", "P4", "P5", "P6"},
    # passes P2 (user) and P3 (g-eng): intersective
    "carol@acme.test": {"P1", "P2", "P3", "P5", "P6"},
    "eve@elsewhere.test": {"P5"},  # anonymous access
}


def _connector(fc: FakeConfluence, **kw: object) -> ConfluenceConnector:
    return ConfluenceConnector(
        SITE,
        email="bot@acme.test",
        api_token="t",
        client=fc.client(),
        sleep=lambda s: None,
        **kw,  # type: ignore[arg-type]
    )


def _tuples(conn: ConfluenceConnector) -> frozenset[Tuple]:
    out: set[Tuple] = set()
    for ev in conn.snapshot():
        match ev:
            case UpsertNode(acl=acl):
                out |= acl.tuples()
            case UpsertGroup(group=g):
                out |= g.tuples()
    return frozenset(out)


def _visible(o: Oracle, u: str) -> set[str]:
    return {d.rsplit("/", 1)[-1] for d in o.visible(u)}


@pytest.mark.parametrize("mode", ["ROLES", "ROLES_TRANSITION"])
def test_snapshot_matches_confluence_semantics(mode: str) -> None:
    conn = _connector(FakeConfluence(mode=mode), access_class_groups=STAND_IN)
    o = Oracle(_tuples(conn))
    for u, pages in EXPECTED.items():
        assert _visible(o, u) == pages, u
    s = conn.stats
    assert (s.spaces, s.pages, s.other_nodes, s.groups) == (3, 6, 1, 3)
    assert s.restricted_nodes == 3
    assert s.unmapped_principals == 1  # a-dave hides his e-mail


def test_unmapped_access_classes_fail_closed() -> None:
    conn = _connector(FakeConfluence())
    o = Oracle(_tuples(conn))
    # Without a stand-in group nobody is "all licensed users": HR is invisible, PUB is public.
    assert _visible(o, "alice@acme.test") == {"P5"}
    assert conn.stats.unmapped_access_classes == 2


def test_pre_roles_mode_reads_space_permissions() -> None:
    fc = FakeConfluence(mode="PRE_ROLES")
    conn = _connector(fc, access_class_groups=STAND_IN)
    o = Oracle(_tuples(conn))
    assert _visible(o, "bob@acme.test") == {"P1", "P4"}
    assert _visible(o, "olga@acme.test") == {"P1"}
    assert _visible(o, "alice@acme.test") == set()  # "create page" is not a view grant
    assert conn.stats.role_principals_skipped == 1


def test_without_email_scope_falls_back_to_identity_map() -> None:
    from rag_authz.identity import MappingResolver

    fc = FakeConfluence(email_lookup_allowed=False)
    conn = _connector(
        fc,
        identity=MappingResolver({"confluence": {"a-bob": "bob@corp.test"}}),
        access_class_groups=STAND_IN,
    )
    o = Oracle(_tuples(conn))
    assert _visible(o, "bob@corp.test") == EXPECTED["bob@acme.test"]
    assert _visible(o, "carol@acme.test") == {"P5"}  # unmapped now: only anonymous access


def test_space_filter() -> None:
    conn = _connector(FakeConfluence(), space_keys={"PUB"}, access_class_groups=STAND_IN)
    assert _visible(Oracle(_tuples(conn)), "carol@acme.test") == {"P5"}


def test_storage_to_text() -> None:
    assert storage_to_text("<h1>T</h1><p>a  &amp;\nb</p><ul><li>x</li></ul>") == "T\na & b\nx"


def test_restriction_added_to_a_parent_covers_its_subtree_in_one_change() -> None:
    fc = FakeConfluence()
    backend = MemoryBackend()
    sync = SyncEngine(backend, MemoryIndex(), embedder=HashingEmbedder(32))
    conn = _connector(fc, access_class_groups=STAND_IN)
    assert sync.reconcile("confluence", conn.snapshot()).errors == []

    def sees(u: str, p: str) -> bool:
        a = ids.encode(f"confluence/acme.atlassian.net/page/{p}")
        return backend.check_docs(u, [a], Consistency.FULL)[a]

    assert sees("bob@acme.test", "P1")
    fc.nodes["P1"].restrict_users = ["a-olga"]
    fc.nodes["P1"].version += 1
    before = backend.writes
    rep = sync.reconcile("confluence", conn.snapshot())
    assert rep.errors == []
    assert backend.writes - before == 1  # one change set on P1's container
    assert not sees("bob@acme.test", "P1")
    assert not sees("carol@acme.test", "P3")
    assert sees("olga@acme.test", "P1")


def test_changes_since_reapplies_modified_pages() -> None:
    from datetime import UTC, datetime

    fc = FakeConfluence()
    backend = MemoryBackend()
    index = MemoryIndex()
    sync = SyncEngine(backend, index, embedder=HashingEmbedder(32))
    conn = _connector(fc, access_class_groups=STAND_IN)
    sync.reconcile("confluence", conn.snapshot())
    fc.nodes["P5"].text = "<p>New welcome text.</p>"
    fc.nodes["P5"].version += 1
    fc.modified = ["P5", "P-gone"]
    rep = sync.apply("confluence", conn.changes(datetime.now(UTC)))
    assert rep.errors == []
    assert rep.chunks_written == 1
    assert len(rep.lag_ms) == 1


def test_reports_hierarchy_depth() -> None:
    conn = _connector(FakeConfluence(), access_class_groups=STAND_IN)
    _tuples(conn)
    assert conn.stats.max_depth == 4  # S1 > P1 > P2 > P3


def test_depth_tracker_handles_chains_and_cycles() -> None:
    from rag_authz.connectors._tree import DepthTracker

    t = DepthTracker()
    for i in range(30):
        t.add(f"n{i}", f"n{i - 1}" if i else None)
    t.add("x", "y")
    t.add("y", "x")  # a cycle must not hang
    assert t.max_depth() == 30


def test_changes_sync_a_new_parent_folder() -> None:
    from datetime import UTC, datetime

    from tests.fakes_confluence import FakeNode

    fc = FakeConfluence()
    backend = MemoryBackend()
    sync = SyncEngine(backend, MemoryIndex(), embedder=HashingEmbedder(32))
    conn = _connector(fc, access_class_groups=STAND_IN)
    sync.reconcile("confluence", conn.snapshot())
    fc.nodes["F2"] = FakeNode("folder", "S1", "New folder")
    fc.nodes["P7"] = FakeNode("page", "S1", "New page", ("folder", "F2"), "<p>Hi.</p>")
    fc.modified = ["P7"]
    assert sync.apply("confluence", conn.changes(datetime.now(UTC))).errors == []
    a = ids.encode("confluence/acme.atlassian.net/page/P7")
    assert backend.check_docs("bob@acme.test", [a], Consistency.FULL)[a]


def test_unexpected_restriction_shape_fails_instead_of_opening() -> None:
    from rag_authz.connectors._http import ConnectorError

    fc = FakeConfluence()
    original = fc._handle

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/P2/restriction/byOperation/read"):
            return httpx.Response(200, json={"operation": "read", "restrictions": {}})
        return original(req)

    fc._handle = handle  # type: ignore[method-assign]
    with pytest.raises(ConnectorError, match="unexpected restriction"):
        _tuples(_connector(fc, access_class_groups=STAND_IN))
