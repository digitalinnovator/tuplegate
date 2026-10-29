from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tuplegate import ids
from tuplegate.authz.base import Consistency
from tuplegate.authz.memory import MemoryBackend
from tuplegate.index.memory import MemoryIndex
from tuplegate.model import NodeAcl, UpsertNode, doc, user
from tuplegate.sync import SyncEngine, SyncState

D = doc("drive/f1")
A = ids.encode(D.id)


def _sees(b: MemoryBackend, who: str) -> bool:
    return b.check_docs(who, [A], Consistency.FULL)[A]


def test_expired_grants_are_never_written_and_live_ones_are_swept() -> None:
    now = datetime.now(UTC)
    b = MemoryBackend()
    sync = SyncEngine(b, MemoryIndex())
    acl = NodeAcl(
        D,
        viewers=frozenset({user("owner@x")}),
        expiring=frozenset(
            {
                (user("past@x"), now - timedelta(minutes=1)),
                (user("soon@x"), now + timedelta(minutes=5)),
                # Also a permanent viewer: the expiry must not remove access.
                (user("owner@x"), now + timedelta(minutes=5)),
            }
        ),
    )
    sync.apply("drive", [UpsertNode(acl)])
    assert _sees(b, "owner@x")
    assert _sees(b, "soon@x")
    assert not _sees(b, "past@x")

    rep = sync.sweep_expired("drive", now + timedelta(minutes=1))
    assert rep.expired == 0
    assert _sees(b, "soon@x")

    rep = sync.sweep_expired("drive", now + timedelta(minutes=6))
    assert (rep.expired, rep.tuples_deleted) == (1, 1)
    assert not _sees(b, "soon@x")
    assert _sees(b, "owner@x")
    assert sync.sweep_expired("drive", now + timedelta(minutes=7)).expired == 0


def test_resync_replaces_recorded_expiries() -> None:
    now = datetime.now(UTC)
    b = MemoryBackend()
    sync = SyncEngine(b, MemoryIndex(), SyncState())
    sync.apply(
        "drive",
        [UpsertNode(NodeAcl(D, expiring=frozenset({(user("u@x"), now + timedelta(minutes=1))})))],
    )
    # The source extends the grant; the old due time must not revoke it.
    sync.apply(
        "drive",
        [UpsertNode(NodeAcl(D, expiring=frozenset({(user("u@x"), now + timedelta(days=1))})))],
    )
    assert sync.sweep_expired("drive", now + timedelta(minutes=2)).expired == 0
    assert _sees(b, "u@x")


def test_naive_expiry_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        NodeAcl(D, expiring=frozenset({(user("u@x"), datetime(2030, 1, 1))}))


def test_ids_too_long_to_store_readably_sync_idempotently_and_delete() -> None:
    from tuplegate.model import DeleteNode, container

    long_doc = doc("github/acme/monorepo/" + "src/main/java/com/acme/" * 12 + "Thing.java")
    assert ids.is_hashed(ids.encode(long_doc.id))
    b = MemoryBackend()
    sync = SyncEngine(b, MemoryIndex())
    acl = NodeAcl(
        long_doc, parent=container("github/acme/monorepo"), viewers=frozenset({user("u@x")})
    )
    assert sync.apply("github", [UpsertNode(acl)]).errors == []
    again = sync.apply("github", [UpsertNode(acl)])
    assert again.errors == []
    assert (again.tuples_written, again.tuples_deleted) == (0, 0)
    a = ids.encode(long_doc.id)
    assert b.check_docs("u@x", [a], Consistency.FULL)[a]
    gone = sync.apply("github", [DeleteNode(long_doc)])
    assert gone.errors == []
    assert gone.tuples_deleted == 2
    assert not b.check_docs("u@x", [a], Consistency.FULL)[a]
