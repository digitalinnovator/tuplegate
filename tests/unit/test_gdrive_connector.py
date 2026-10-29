from __future__ import annotations

import httpx
import pytest

from tests.fakes_gdrive import FakeDrive
from tuplegate import ids
from tuplegate.authz.base import Consistency
from tuplegate.authz.memory import MemoryBackend
from tuplegate.connectors._http import ConnectorError
from tuplegate.connectors.gdrive import GoogleDriveConnector
from tuplegate.index.memory import MemoryIndex
from tuplegate.model import Tuple, UpsertGroup, UpsertNode, user
from tuplegate.oracle import Oracle
from tuplegate.sync import SyncEngine
from tuplegate.testing import HashingEmbedder

ALL = {"api-design", "secret.txt", "link.txt", "public.md", "domain.md", "guest.md", "all-hands.md"}
EXPECTED = {
    # organizer: limited access does not remove organizers
    "olga@acme.test": ALL,
    # direct grant on the limited folder; customer-wide and domain grants
    "alice@acme.test": {"secret.txt", "public.md", "domain.md", "guest.md", "all-hands.md"},
    # bob is in eng; carol is in backend, nested in eng. Neither sees the limited folder.
    "bob@acme.test": {"api-design", "link.txt", "public.md", "domain.md", "all-hands.md"},
    "carol@acme.test": {"api-design", "link.txt", "public.md", "domain.md", "all-hands.md"},
    # outside the account: public, plus an expiring direct grant. Metadata-only is not access.
    "eve@elsewhere.test": {"public.md", "guest.md"},
    "nobody@elsewhere.test": {"public.md"},
}


def _connector(fd: FakeDrive, **kw: object) -> GoogleDriveConnector:
    return GoogleDriveConnector(["D1"], lambda: "t", client=fd.client(), sleep=lambda s: None, **kw)  # type: ignore[arg-type]


def _tuples(conn: GoogleDriveConnector) -> frozenset[Tuple]:
    out: set[Tuple] = set()
    for ev in conn.snapshot():
        match ev:
            case UpsertNode(acl=acl):
                out |= acl.tuples()
            case UpsertGroup(group=g):
                out |= g.tuples()
    return frozenset(out)


def _visible(o: Oracle, u: str) -> set[str]:
    return {d.removeprefix("gdrive/") for d in o.visible(u)}


def test_snapshot_matches_drive_semantics() -> None:
    conn = _connector(FakeDrive())
    o = Oracle(_tuples(conn))
    for u, docs in EXPECTED.items():
        assert _visible(o, u) == docs, u
    s = conn.stats
    assert (s.drives, s.folders, s.files, s.groups) == (1, 2, 7, 3)
    assert s.link_only_grants == 1
    assert s.metadata_only_grants == 1
    assert s.expiring_grants == 1
    assert s.limited_access_items == 1


def test_folder_grants_are_stored_once_not_flattened() -> None:
    tuples = _tuples(_connector(FakeDrive()))
    # eng's access to api-design comes from D1 through two parent hops, not a copied grant.
    assert not any(t.object.id == "gdrive/api-design" and t.subject is not None for t in tuples)


def test_retries_rate_limits() -> None:
    fd = FakeDrive()
    fd.fail_once = {"/drive/v3/files", "/admin/directory/v1/users"}
    conn = _connector(fd)
    assert _visible(Oracle(_tuples(conn)), "bob@acme.test") == EXPECTED["bob@acme.test"]
    assert conn._http.retries == 2


def test_changes_feed_drives_incremental_sync() -> None:
    fd = FakeDrive()
    backend = MemoryBackend()
    sync = SyncEngine(backend, MemoryIndex(), embedder=HashingEmbedder(32))
    conn = _connector(fd)
    tokens = conn.start_tokens()
    assert sync.reconcile("gdrive", conn.snapshot()).errors == []

    def sees(u: str, d: str) -> bool:
        a = ids.encode(f"gdrive/{d}")
        return backend.check_docs(u, [a], Consistency.FULL)[a]

    assert sees("bob@acme.test", "api-design")
    # F_api turns into a limited-access folder: eng's inherited access stops at it.
    fd.files["F_api"].limited = True
    fd.record_change("F_api")
    # A file is trashed, another is deleted outright.
    fd.files["public.md"].trashed = True
    fd.record_change("public.md")
    del fd.files["link.txt"]
    fd.record_change("link.txt", removed=True)

    batch = conn.changes("D1", tokens["D1"])
    rep = sync.apply("gdrive", batch.events)
    assert rep.errors == []
    assert len(rep.lag_ms) == len(batch.events)
    assert not sees("bob@acme.test", "api-design")  # one parent tuple removed
    assert sees("olga@acme.test", "api-design")  # organizer survives limited access
    assert not sees("eve@elsewhere.test", "public.md")
    assert not sees("bob@acme.test", "link.txt")
    assert batch.next_token == str(len(fd.changes))
    assert conn.changes("D1", batch.next_token).events == []


def test_change_to_an_item_we_can_no_longer_read_is_a_delete() -> None:
    fd = FakeDrive()
    original = fd._handle

    def handle(req: httpx.Request) -> httpx.Response:
        # The connector's identity lost access between the change entry and the read.
        if req.url.path.endswith("/files/domain.md/permissions"):
            return httpx.Response(404)
        return original(req)

    fd._handle = handle  # type: ignore[method-assign]
    conn = _connector(fd)
    token = conn.start_tokens()["D1"]
    fd.record_change("domain.md")
    batch = conn.changes("D1", token)
    assert {type(e).__name__ for e in batch.events} == {"DeleteNode"}


def test_other_errors_in_the_feed_raise_so_the_token_is_kept() -> None:
    fd = FakeDrive()
    original = fd._handle

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/files/domain.md/permissions"):
            return httpx.Response(403, text="forbidden")
        return original(req)

    fd._handle = handle  # type: ignore[method-assign]
    conn = _connector(fd)
    token = conn.start_tokens()["D1"]
    fd.record_change("domain.md")
    with pytest.raises(ConnectorError):
        conn.changes("D1", token)


def test_identity_resolver_maps_and_counts_unmapped() -> None:
    from tuplegate.identity import MappingResolver

    resolver = MappingResolver({"gdrive": {"bob@acme.test": "bob@corp.test"}})
    conn = _connector(FakeDrive(), identity=resolver)
    tuples = _tuples(conn)
    assert any(t.subject == user("bob@corp.test") for t in tuples)
    assert conn.stats.unmapped_principals > 0


def test_reports_hierarchy_depth() -> None:
    conn = _connector(FakeDrive())
    _tuples(conn)
    # D1 > F_secret (limited: no parent tuple, so its chain restarts) / D1 > F_api
    assert conn.stats.max_depth == 2


def test_shared_drive_membership_revoke_is_one_event() -> None:
    fd = FakeDrive()
    backend = MemoryBackend()
    sync = SyncEngine(backend, MemoryIndex(), embedder=HashingEmbedder(32))
    conn = _connector(fd)
    token = conn.start_tokens()["D1"]
    sync.reconcile("gdrive", conn.snapshot())
    a = ids.encode("gdrive/api-design")
    assert backend.check_docs("carol@acme.test", [a], Consistency.FULL)[a]
    fd.drives["D1"] = [p for p in fd.drives["D1"] if p["id"] != "p-eng"]
    fd.record_drive_change()
    batch = conn.changes("D1", token)
    assert len(batch.events) == 1
    rep = sync.apply("gdrive", batch.events)
    assert rep.tuples_deleted == 1  # one tuple, however many files sit below
    assert not backend.check_docs("carol@acme.test", [a], Consistency.FULL)[a]


def test_suspended_group_members_lose_group_access() -> None:
    fd = FakeDrive()
    fd.suspended = {"carol@acme.test"}
    o = Oracle(_tuples(_connector(fd)))
    assert "api-design" not in _visible(o, "carol@acme.test")
    assert "api-design" in _visible(o, "bob@acme.test")
