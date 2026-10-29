from __future__ import annotations

import random
from collections.abc import Callable

import pytest

from rag_authz import ids
from rag_authz.index.base import ChunkRecord
from rag_authz.index.pgvector import PgVectorIndex

pytestmark = pytest.mark.integration


def _vec(rng: random.Random, dim: int) -> list[float]:
    return [rng.gauss(0, 1) for _ in range(dim)]


def test_filter_pushdown_replace_and_delete(pgindex: Callable[..., PgVectorIndex]) -> None:
    ix = pgindex(8)
    rng = random.Random(1)
    ix.upsert([ChunkRecord(f"c{i}", f"doc/{i % 10}", f"t{i}", _vec(rng, 8)) for i in range(200)])
    allowed = {ids.encode("doc/3"), ids.encode("doc/7")}
    hits = ix.search(_vec(rng, 8), 50, acl_filter=allowed)
    assert hits
    assert {h.acl_id for h in hits} <= allowed
    assert {h.doc_id for h in hits} <= {"doc/3", "doc/7"}
    ix.replace_doc("doc/3", [ChunkRecord("new", "doc/3", "fresh", _vec(rng, 8))])
    assert {h.chunk_id for h in ix.search(_vec(rng, 8), 50, acl_filter={ids.encode("doc/3")})} == {
        "new"
    }
    assert ix.delete_docs(["doc/7"]) == 20
    assert ix.search(_vec(rng, 8), 50, acl_filter={ids.encode("doc/7")}) == []
    assert ix.search(_vec(rng, 8), 5, acl_filter=set()) == []


def test_iterative_scan_recovers_selective_filters(pgindex: Callable[..., PgVectorIndex]) -> None:
    """With iterative scans off, HNSW hands at most ef_search rows to the filter."""
    rng = random.Random(2)
    rows = [ChunkRecord(f"c{i}", f"doc/{i}", "", _vec(rng, 16)) for i in range(4000)]
    allowed = {ids.encode(f"doc/{i}") for i in range(0, 4000, 100)}  # 1% selectivity
    q = _vec(rng, 16)
    off = pgindex(16, iterative_scan="off")
    off.upsert(rows)
    relaxed = pgindex(16, iterative_scan="relaxed_order")
    relaxed.upsert(rows)
    n_off = len(off.search(q, 10, acl_filter=allowed))
    n_relaxed = len(relaxed.search(q, 10, acl_filter=allowed))
    assert n_relaxed == 10
    assert n_off <= n_relaxed
