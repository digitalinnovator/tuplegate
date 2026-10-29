"""The one interface OpenFGA, SpiceDB and the in-memory oracle sit behind.

Doc identifiers on this interface are *ACL ids*: the encoded form from ``tuplegate.ids``. The
vector index stores the same ACL id next to each chunk, so neither filter path decodes
anything on the hot path. User ids are canonical and encoded inside each backend.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from tuplegate import ids
from tuplegate.model import UNMAPPED_PREFIX, WILDCARD, ObjectRef, Relation, SubjectRef, Tuple


class AuthzError(RuntimeError):
    """The engine failed or returned something unusable. Callers fail closed."""


class Consistency(StrEnum):
    # Bypass caches and read the latest committed state. The default: a revoke must bite.
    FULL = "full"
    # Let the engine serve cached or quantized results. Faster, with a measured leak window.
    MINIMIZE_LATENCY = "minimize_latency"
    # At least as fresh as this backend's latest write (SpiceDB ZedTokens). Cheaper than FULL
    # with no leak window for revokes written through this process. Engines without write
    # tokens treat it as FULL.
    AT_LEAST_AS_FRESH = "at_least_as_fresh"


@dataclass(frozen=True, slots=True)
class ListResult:
    acl_ids: frozenset[str]
    # False when the engine stopped early (deadline or result cap). A truncated list costs
    # recall, never correctness: every id in it is still authorised.
    complete: bool


class AuthzBackend(Protocol):
    name: str

    def ensure_schema(self) -> None: ...

    def read(self, obj: ObjectRef) -> frozenset[Tuple]:
        """Every stored tuple whose object is ``obj``."""
        ...

    def write(self, writes: Collection[Tuple], deletes: Collection[Tuple]) -> None:
        """Apply one change set, atomically when the engine's batch limit allows."""
        ...

    def check_docs(
        self, user_id: str, acl_ids: Sequence[str], consistency: Consistency
    ) -> dict[str, bool]: ...

    def list_docs(self, user_id: str, consistency: Consistency) -> ListResult: ...

    def close(self) -> None: ...


def normalise(t: Tuple) -> Tuple:
    """The tuple as ``AuthzBackend.read`` returns it: ids too long to store readably are in
    their raw digest form. Diff desired tuples in this form against what the engine holds."""

    def obj(r: ObjectRef) -> ObjectRef:
        n = ids.normalise(r.id)
        return r if n == r.id else replace(r, id=n)

    def sub(s: SubjectRef) -> SubjectRef:
        n = ids.normalise(s.id)
        return s if n == s.id else replace(s, id=n)

    o = obj(t.object)
    s = sub(t.subject) if t.subject is not None else None
    p = obj(t.parent_ref) if t.parent_ref is not None else None
    if o is t.object and s is t.subject and p is t.parent_ref:
        return t
    return replace(t, object=o, subject=s, parent_ref=p)


def validate_subject(user_id: str) -> None:
    """Refuse subjects that could claim grants they should not have."""
    if not user_id or user_id == WILDCARD or user_id.startswith(UNMAPPED_PREFIX):
        raise ValueError(f"invalid query subject: {user_id!r}")


# --- Safe write ordering ---------------------------------------------------------------------
# A change set that fits one engine request is applied atomically. When it does not, narrowing
# operations go first and widening operations last, so a partially applied change set can
# under-grant (fail closed) but never over-grant.


def _phase(t: Tuple, is_write: bool) -> int:
    narrowing = 0
    structural = 1
    widening = 2
    if t.relation is Relation.PARENT:
        # Changing a parent both adds and removes inherited grants and restrictions.
        return structural
    if t.relation is Relation.RESTRICTED:
        return narrowing if is_write else widening
    return widening if is_write else narrowing


def safe_batches(
    writes: Iterable[Tuple], deletes: Iterable[Tuple], max_per_request: int
) -> Iterator[tuple[list[Tuple], list[Tuple]]]:
    """Split a change set into engine requests in narrowing -> structural -> widening order."""
    w = sorted(set(writes))
    d = sorted(set(deletes))
    if len(w) + len(d) <= max_per_request:
        if w or d:
            yield w, d
        return
    for phase in range(3):
        pw = [t for t in w if _phase(t, True) == phase]
        pd = [t for t in d if _phase(t, False) == phase]
        # Within the structural phase, deleting the old parent before adding the new one could
        # briefly drop inherited restrictions; adding first could briefly drop nothing but
        # can add grants. Keep both halves of a move in the same request where possible.
        ops = [(t, False) for t in pd] + [(t, True) for t in pw]
        for i in range(0, len(ops), max_per_request):
            chunk = ops[i : i + max_per_request]
            yield [t for t, is_w in chunk if is_w], [t for t, is_w in chunk if not is_w]
