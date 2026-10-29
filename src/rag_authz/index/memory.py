"""Exact (brute-force) in-memory index. Unit tests and ground truth; no ANN approximation."""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence

from rag_authz import ids
from rag_authz.index.base import ChunkRecord, Hit


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryIndex:
    name = "memory"

    def __init__(self) -> None:
        self._chunks: dict[str, ChunkRecord] = {}

    def ensure_schema(self) -> None:
        return None

    def upsert(self, chunks: Sequence[ChunkRecord]) -> None:
        for c in chunks:
            self._chunks[c.chunk_id] = c

    def replace_doc(self, doc_id: str, chunks: Sequence[ChunkRecord]) -> None:
        if any(c.doc_id != doc_id for c in chunks):
            raise ValueError("replace_doc chunks must all belong to doc_id")
        self.delete_docs([doc_id])
        self.upsert(chunks)

    def delete_docs(self, doc_ids: Collection[str]) -> int:
        targets = set(doc_ids)
        doomed = [k for k, c in self._chunks.items() if c.doc_id in targets]
        for k in doomed:
            del self._chunks[k]
        return len(doomed)

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        acl_filter: Collection[str] | None = None,
        offset: int = 0,
    ) -> list[Hit]:
        allowed = set(acl_filter) if acl_filter is not None else None
        scored = []
        for c in self._chunks.values():
            acl = ids.encode(c.doc_id)
            if allowed is not None and acl not in allowed:
                continue
            scored.append((_cosine(vector, c.vector), c.chunk_id, c, acl))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [
            Hit(c.chunk_id, c.doc_id, acl, c.text, score, dict(c.metadata))
            for score, _, c, acl in scored[offset : offset + k]
        ]

    def count(self) -> int:
        return len(self._chunks)

    def close(self) -> None:
        return None
