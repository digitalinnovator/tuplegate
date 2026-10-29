"""Exact numpy index for benchmarks: the same contract as the ANN adapters, zero approximation.

Isolates what the *filtering strategy* costs in recall from what ANN approximation costs:
under exact search, post-filter still loses recall (the authorised docs are not in the
top k * overfetch), while pre-filter is exact by construction.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

import numpy as np
import numpy.typing as npt

from rag_authz import ids
from rag_authz.index.base import ChunkRecord, Hit


class ExactIndex:
    name = "exact"

    def __init__(self, dim: int) -> None:
        self._dim = dim
        self._records: dict[str, ChunkRecord] = {}
        self._matrix: npt.NDArray[np.float32] | None = None
        self._order: list[ChunkRecord] = []
        self._acl: npt.NDArray[np.str_] | None = None

    def ensure_schema(self) -> None:
        return None

    def _build(self) -> None:
        self._order = list(self._records.values())
        if not self._order:
            self._matrix = np.zeros((0, self._dim), dtype=np.float32)
            self._acl = np.array([], dtype=np.str_)
            return
        m = np.asarray([r.vector for r in self._order], dtype=np.float32)
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
        self._matrix = m
        self._acl = np.array([ids.encode(r.doc_id) for r in self._order])

    def upsert(self, chunks: Sequence[ChunkRecord]) -> None:
        for c in chunks:
            self._records[c.chunk_id] = c
        self._matrix = None

    def replace_doc(self, doc_id: str, chunks: Sequence[ChunkRecord]) -> None:
        self.delete_docs([doc_id])
        self.upsert(chunks)

    def delete_docs(self, doc_ids: Collection[str]) -> int:
        targets = set(doc_ids)
        doomed = [k for k, r in self._records.items() if r.doc_id in targets]
        for k in doomed:
            del self._records[k]
        self._matrix = None
        return len(doomed)

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        acl_filter: Collection[str] | None = None,
        offset: int = 0,
    ) -> list[Hit]:
        if self._matrix is None:
            self._build()
        assert self._matrix is not None
        assert self._acl is not None
        q = np.asarray(vector, dtype=np.float32)
        q /= max(float(np.linalg.norm(q)), 1e-12)
        scores = self._matrix @ q
        if acl_filter is not None:
            mask = np.isin(self._acl, np.fromiter(acl_filter, dtype=self._acl.dtype))
            scores = np.where(mask, scores, -np.inf)
        n = offset + k
        if n < len(scores):
            part = np.argpartition(-scores, n)[:n]
            top = part[np.argsort(-scores[part], kind="stable")]
        else:
            top = np.argsort(-scores, kind="stable")
        out = []
        for i in top[offset:n]:
            if not np.isfinite(scores[i]):
                break
            r = self._order[int(i)]
            out.append(
                Hit(
                    r.chunk_id,
                    r.doc_id,
                    str(self._acl[i]),
                    r.text,
                    float(scores[i]),
                    dict(r.metadata),
                )
            )
        return out

    def count(self) -> int:
        return len(self._records)

    def close(self) -> None:
        return None
