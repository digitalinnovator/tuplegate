"""In-memory backend over the oracle. For unit tests and small demos, not production."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from tuplegate import ids
from tuplegate.authz.base import Consistency, ListResult, validate_subject
from tuplegate.model import ObjectRef, ObjectType, Tuple, doc
from tuplegate.oracle import Oracle


class MemoryBackend:
    name = "memory"

    def __init__(self, tuples: Collection[Tuple] = ()) -> None:
        self._tuples: set[Tuple] = set(tuples)
        self._oracle: Oracle | None = None
        self._acl_map: dict[str, str] | None = None
        self.writes = 0

    def ensure_schema(self) -> None:
        return None

    def _current(self) -> Oracle:
        if self._oracle is None:
            self._oracle = Oracle(self._tuples)
            self._acl_map = None
        return self._oracle

    def _acl_to_doc(self) -> dict[str, str]:
        if self._acl_map is None:
            self._acl_map = {
                ids.encode(t.object.id): t.object.id
                for t in self._tuples
                if t.object.type is ObjectType.DOC
            }
        return self._acl_map

    def read(self, obj: ObjectRef) -> frozenset[Tuple]:
        # Match on the encoded id, as the engines do, so raw digest ids find their tuples.
        key = ids.encode(obj.id)
        return frozenset(
            t for t in self._tuples if t.object.type is obj.type and ids.encode(t.object.id) == key
        )

    def write(self, writes: Collection[Tuple], deletes: Collection[Tuple]) -> None:
        self._tuples.difference_update(deletes)
        self._tuples.update(writes)
        self._oracle = None
        self._acl_map = None
        self.writes += 1

    def check_docs(
        self, user_id: str, acl_ids: Sequence[str], consistency: Consistency
    ) -> dict[str, bool]:
        validate_subject(user_id)
        oracle = self._current()
        known = self._acl_to_doc()
        out: dict[str, bool] = {}
        for a in acl_ids:
            d = known.get(a)
            out[a] = d is not None and oracle.can_view(user_id, doc(d))
        return out

    def list_docs(self, user_id: str, consistency: Consistency) -> ListResult:
        validate_subject(user_id)
        return ListResult(frozenset(self._visible(user_id)), complete=True)

    def _visible(self, user_id: str) -> set[str]:
        return {ids.encode(d) for d in self._current().visible(user_id, ObjectType.DOC)}

    def close(self) -> None:
        return None

    @property
    def tuples(self) -> frozenset[Tuple]:
        return frozenset(self._tuples)
