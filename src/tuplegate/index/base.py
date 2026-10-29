"""The vector-index interface the retrieval strategies push filters into.

Every chunk carries two ids: ``doc_id`` (canonical, human-readable) and ``acl_id`` (its
encoded form, see ``tuplegate.ids``). Filters and authorization checks use ``acl_id`` so the
hot path never decodes. Adapters compute ``acl_id`` themselves at upsert time and never trust a
caller-supplied one.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    chunk_id: str
    doc_id: str
    text: str
    vector: Sequence[float]
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: str
    doc_id: str
    acl_id: str
    text: str
    score: float
    metadata: Mapping[str, str] = field(default_factory=dict)


class VectorIndex(Protocol):
    name: str

    def ensure_schema(self) -> None: ...

    def upsert(self, chunks: Sequence[ChunkRecord]) -> None: ...

    def replace_doc(self, doc_id: str, chunks: Sequence[ChunkRecord]) -> None:
        """Atomically swap every chunk of ``doc_id`` for ``chunks``.

        Replacing (not merging) matters for safety: stale chunks of an old version must not
        survive under a doc id whose ACL may since have widened.
        """
        ...

    def delete_docs(self, doc_ids: Collection[str]) -> int: ...

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        acl_filter: Collection[str] | None = None,
        offset: int = 0,
    ) -> list[Hit]:
        """Top ``k`` by similarity, skipping ``offset``; restricted to ``acl_filter`` if given."""
        ...

    def count(self) -> int: ...

    def close(self) -> None: ...
