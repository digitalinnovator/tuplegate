"""Qdrant adapter against a real Qdrant (`make up-full`)."""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest

from tuplegate import ids
from tuplegate.index.base import ChunkRecord
from tuplegate.index.qdrant import QdrantIndex

pytestmark = pytest.mark.integration


def _vec(rng: random.Random, dim: int) -> list[float]:
    return [rng.gauss(0, 1) for _ in range(dim)]


def test_filter_pushdown_replace_and_delete(qdindex: Callable[..., QdrantIndex]) -> None:
    ix = qdindex(8)
    ix.ensure_schema()  # idempotent
    rng = random.Random(1)
    ix.upsert([ChunkRecord(f"c{i}", f"doc/{i % 10}", f"t{i}", _vec(rng, 8)) for i in range(200)])
    assert ix.count() == 200
    allowed = {ids.encode("doc/3"), ids.encode("doc/7")}
    hits = ix.search(_vec(rng, 8), 50, acl_filter=allowed)
    assert len(hits) == 40
    assert {h.acl_id for h in hits} <= allowed
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    ix.replace_doc("doc/3", [ChunkRecord("new", "doc/3", "fresh", _vec(rng, 8), {"k": "v"})])
    got = ix.search(_vec(rng, 8), 50, acl_filter={ids.encode("doc/3")})
    assert [(h.chunk_id, h.text, dict(h.metadata)) for h in got] == [("new", "fresh", {"k": "v"})]
    assert ix.delete_docs(["doc/7"]) == 20
    assert ix.search(_vec(rng, 8), 50, acl_filter={ids.encode("doc/7")}) == []
    assert ix.search(_vec(rng, 8), 5, acl_filter=set()) == []
    ix.replace_doc("doc/3", [])
    assert ix.count() == 200 - 20 - 20


def test_offset_pages_without_overlap(qdindex: Callable[..., QdrantIndex]) -> None:
    ix = qdindex(8)
    rng = random.Random(3)
    ix.upsert([ChunkRecord(f"c{i}", f"doc/{i}", "", _vec(rng, 8)) for i in range(100)])
    q = _vec(rng, 8)
    first, second = ix.search(q, 10), ix.search(q, 10, offset=10)
    assert len(first) == len(second) == 10
    assert not {h.chunk_id for h in first} & {h.chunk_id for h in second}


@pytest.mark.parametrize("acorn", [False, True])
def test_selective_filters_fill_k(qdindex: Callable[..., QdrantIndex], acorn: bool) -> None:
    rng = random.Random(2)
    rows = [ChunkRecord(f"c{i}", f"doc/{i}", "", _vec(rng, 16)) for i in range(4000)]
    allowed = {ids.encode(f"doc/{i}") for i in range(0, 4000, 100)}  # 1% selectivity
    ix = qdindex(16, acorn=acorn)
    ix.upsert(rows)
    hits = ix.search(_vec(rng, 16), 10, acl_filter=allowed)
    assert len(hits) == 10
    assert {h.acl_id for h in hits} <= allowed
