"""Content and ACL changes must never show content to a reader the source would not show it to.

Regression tests for review findings on the sync engine (2026-10-28).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tuplegate.authz.memory import MemoryBackend
from tuplegate.index.memory import MemoryIndex
from tuplegate.model import DocumentContent, NodeAcl, UpsertNode, doc, public, user
from tuplegate.retrieval import AuthorizedRetriever, Strategy
from tuplegate.sync import SyncEngine, SyncState
from tuplegate.testing import HashingEmbedder

EMB = HashingEmbedder(32)
D = doc("drive/salaries")
Q = EMB.embed_one("salary table")


class FlakyEmbedder:
    def __init__(self) -> None:
        self.fail = False

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("429 from the embedding API")
        return EMB(texts)


def _texts(r: AuthorizedRetriever, who: str) -> set[str]:
    return {h.text for h in r.retrieve(who, Q, 5, strategy=Strategy.PRE_FILTER).hits}


class WatchingBackend(MemoryBackend):
    def __init__(self, index: MemoryIndex, watcher: list[set[str]]) -> None:
        super().__init__()
        self._ix = index
        self._watch = watcher

    def write(self, writes: object, deletes: object) -> None:  # type: ignore[override]
        super().write(writes, deletes)  # type: ignore[arg-type]
        r = AuthorizedRetriever(self, self._ix)
        self._watch.append(_texts(r, "bob@x"))


def test_widening_never_exposes_the_old_content() -> None:
    seen: list[set[str]] = []
    ix = MemoryIndex()
    b = WatchingBackend(ix, seen)
    sync = SyncEngine(b, ix, embedder=EMB)
    v1 = NodeAcl(D, viewers=frozenset({user("legal@x")}))
    sync.apply("drive", [UpsertNode(v1, DocumentContent("SECRET salary table", "v1"))])
    seen.clear()
    v2 = NodeAcl(D, viewers=frozenset({public()}))
    sync.apply("drive", [UpsertNode(v2, DocumentContent("redacted salary table", "v2"))])
    # At the moment the widening tuple landed, bob could see nothing old.
    assert all("SECRET salary table" not in s for s in seen)
    assert _texts(AuthorizedRetriever(b, ix), "bob@x") == {"redacted salary table"}


def test_embedder_failure_still_applies_the_acl_and_hides_old_content() -> None:
    emb = FlakyEmbedder()
    b, ix = MemoryBackend(), MemoryIndex()
    sync = SyncEngine(b, ix, embedder=emb)
    r = AuthorizedRetriever(b, ix)
    sync.apply(
        "drive",
        [
            UpsertNode(
                NodeAcl(D, viewers=frozenset({user("legal@x")})),
                DocumentContent("SECRET salary table", "v1"),
            )
        ],
    )
    emb.fail = True
    rep = sync.apply(
        "drive",
        [
            UpsertNode(
                NodeAcl(D, viewers=frozenset({public()})),
                DocumentContent("redacted salary table", "v2"),
            )
        ],
    )
    assert rep.errors  # reported, so the event is retried
    assert _texts(r, "bob@x") == set()  # no stale secret under the wider ACL
    # A revoke is not blocked by the outage either.
    rep = sync.apply(
        "drive",
        [UpsertNode(NodeAcl(D, viewers=frozenset({user("legal@x")})), DocumentContent("x", "v3"))],
    )
    assert rep.errors
    assert _texts(r, "bob@x") == set()
    emb.fail = False
    sync.apply(
        "drive",
        [
            UpsertNode(
                NodeAcl(D, viewers=frozenset({public()})),
                DocumentContent("redacted salary table", "v2"),
            )
        ],
    )
    assert _texts(r, "bob@x") == {"redacted salary table"}


def test_missing_content_with_a_wider_acl_purges_old_chunks() -> None:
    b, ix = MemoryBackend(), MemoryIndex()
    sync = SyncEngine(b, ix, embedder=EMB)
    sync.apply(
        "drive",
        [
            UpsertNode(
                NodeAcl(D, viewers=frozenset({user("legal@x")})),
                DocumentContent("SECRET salary table", "v1"),
            )
        ],
    )
    # The file grew past max_file_bytes: the connector sends the ACL without content.
    sync.apply("drive", [UpsertNode(NodeAcl(D, viewers=frozenset({public()})))])
    assert _texts(AuthorizedRetriever(b, ix), "bob@x") == set()
    assert ix.count() == 0


def test_narrowing_swaps_content_without_a_gap() -> None:
    b, ix = MemoryBackend(), MemoryIndex()
    sync = SyncEngine(b, ix, embedder=EMB)
    both = frozenset({user("legal@x"), user("bob@x")})
    sync.apply(
        "drive", [UpsertNode(NodeAcl(D, viewers=both), DocumentContent("salary table v1", "v1"))]
    )
    rep = sync.apply(
        "drive",
        [
            UpsertNode(
                NodeAcl(D, viewers=frozenset({user("legal@x")})),
                DocumentContent("salary table v2", "v2"),
            )
        ],
    )
    assert rep.chunks_deleted == 0  # replace_doc, not purge-and-insert
    r = AuthorizedRetriever(b, ix)
    assert _texts(r, "bob@x") == set()
    assert _texts(r, "legal@x") == {"salary table v2"}


def test_expiring_grants_are_recorded_before_they_are_written() -> None:
    from datetime import UTC, datetime, timedelta

    class CrashingBackend(MemoryBackend):
        def write(self, writes: object, deletes: object) -> None:  # type: ignore[override]
            super().write(writes, deletes)  # type: ignore[arg-type]
            raise SystemExit("killed after the engine write")

    state = SyncState()
    b = CrashingBackend()
    sync = SyncEngine(b, MemoryIndex(), state)
    soon = datetime.now(UTC) + timedelta(minutes=1)
    with pytest.raises(SystemExit):
        sync._apply_one(
            "drive",
            UpsertNode(NodeAcl(D, expiring=frozenset({(user("guest@x"), soon)}))),
            1,
            sync_report(),
        )
    state._db.rollback()  # what a killed process would lose
    assert [t.subject for t, _ in state.due("drive", soon + timedelta(seconds=1))] == [
        user("guest@x")
    ]


def sync_report() -> object:
    from tuplegate.sync import SyncReport

    return SyncReport("drive")


def test_one_failing_sweep_does_not_abort_the_rest() -> None:
    class FlakyDelete(MemoryBackend):
        def write(self, writes: object, deletes: object) -> None:  # type: ignore[override]
            if any(t.object.id == "drive/a" for t in deletes):  # type: ignore[attr-defined]
                raise RuntimeError("engine hiccup")
            super().write(writes, deletes)  # type: ignore[arg-type]

    b = FlakyDelete()
    sync = SyncEngine(b, MemoryIndex())
    acls = [NodeAcl(doc(f"drive/{n}"), viewers=frozenset({user("u@x")})) for n in "abc"]
    sync.reconcile("drive", [UpsertNode(a) for a in acls])
    rep = sync.reconcile("drive", [])
    assert rep.swept == 2
    assert len(rep.errors) == 1
