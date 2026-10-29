"""Reference evaluator for the schema in ``schema.py``: the executable specification.

The oracle interprets a set of canonical tuples with plain Python and no engine. It serves
three purposes:

1. Ground truth for the leak-rate harness and the filtering benchmark.
2. The differential-testing baseline: OpenFGA and SpiceDB must agree with it on every
   (user, doc) pair of a generated organisation.
3. The in-memory authorization backend for unit tests.

Semantics (identical to the schema):

- ``granted(n)  = viewer(n) or granted(p) for any parent p``
- ``blocked(n)  = (restricted(n) and not restriction_member(n)) or blocked(p) for any parent p``
- ``can_view(n) = granted(n) and not blocked(n)``
- group membership is transitive through ``group#member`` subjects; cycles resolve to the least
  fixed point (members of a cycle are members of every group in it).

Two evaluation modes: per-user (``can_view``/``visible``) and bulk bitsets (``Bulk``), where
every user is a bit in a Python int so a 10,000-doc organisation is evaluated in one pass.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from tuplegate.model import (
    UNMAPPED_PREFIX,
    ObjectRef,
    ObjectType,
    Relation,
    SubjectRef,
    Tuple,
)


class CycleError(ValueError):
    """Parent tuples form a cycle. Connectors must never produce one."""


@dataclass
class _Index:
    members: dict[str, list[SubjectRef]] = field(default_factory=lambda: defaultdict(list))
    member_of: dict[SubjectRef, set[str]] = field(default_factory=lambda: defaultdict(set))
    viewers: dict[ObjectRef, list[SubjectRef]] = field(default_factory=lambda: defaultdict(list))
    parents: dict[ObjectRef, list[ObjectRef]] = field(default_factory=lambda: defaultdict(list))
    restricted: set[ObjectRef] = field(default_factory=set)
    restriction: dict[ObjectRef, list[SubjectRef]] = field(
        default_factory=lambda: defaultdict(list)
    )
    nodes: set[ObjectRef] = field(default_factory=set)


def _build(tuples: Iterable[Tuple]) -> _Index:
    ix = _Index()
    for t in tuples:
        obj = t.object
        if obj.type is ObjectType.GROUP:
            assert t.subject is not None
            ix.members[obj.id].append(t.subject)
            ix.member_of[t.subject].add(obj.id)
            continue
        ix.nodes.add(obj)
        match t.relation:
            case Relation.PARENT:
                assert t.parent_ref is not None
                ix.parents[obj].append(t.parent_ref)
                ix.nodes.add(t.parent_ref)
            case Relation.VIEWER:
                assert t.subject is not None
                ix.viewers[obj].append(t.subject)
            case Relation.RESTRICTED:
                ix.restricted.add(obj)
            case Relation.RESTRICTION_MEMBER:
                assert t.subject is not None
                ix.restriction[obj].append(t.subject)
            case _:
                raise ValueError(f"unexpected relation on a node: {t}")
    return ix


class Oracle:
    def __init__(self, tuples: Iterable[Tuple]) -> None:
        self._tuples = frozenset(tuples)
        self._ix = _build(self._tuples)

    @property
    def tuples(self) -> frozenset[Tuple]:
        return self._tuples

    # --- per-user evaluation ---------------------------------------------------------------

    def groups_of(self, user_id: str) -> frozenset[str]:
        """Every group the user is a (transitive) member of."""
        seen: set[str] = set()
        frontier = list(self._ix.member_of.get(SubjectRef(ObjectType.USER, user_id), ()))
        while frontier:
            g = frontier.pop()
            if g in seen:
                continue
            seen.add(g)
            frontier.extend(
                self._ix.member_of.get(SubjectRef(ObjectType.GROUP, g, Relation.MEMBER), ())
            )
        return frozenset(seen)

    def _evaluator(self, user_id: str) -> _UserEval:
        if user_id.startswith(UNMAPPED_PREFIX) or user_id == "*":
            raise ValueError(f"{user_id!r} is not a valid query subject")
        return _UserEval(self._ix, user_id, self.groups_of(user_id))

    def can_view(self, user_id: str, node: ObjectRef) -> bool:
        return self._evaluator(user_id).can_view(node)

    def visible(self, user_id: str, type_: ObjectType = ObjectType.DOC) -> frozenset[str]:
        ev = self._evaluator(user_id)
        return frozenset(n.id for n in self._ix.nodes if n.type is type_ and ev.can_view(n))

    # --- bulk evaluation -------------------------------------------------------------------

    def bulk(self, users: Sequence[str]) -> Bulk:
        return Bulk(self._ix, users)


class _UserEval:
    def __init__(self, ix: _Index, user_id: str, groups: frozenset[str]) -> None:
        self._ix = ix
        self._user = user_id
        self._groups = groups
        self._granted: dict[ObjectRef, bool] = {}
        self._blocked: dict[ObjectRef, bool] = {}

    def _matches(self, s: SubjectRef) -> bool:
        if s.type is ObjectType.USER:
            return s.is_wildcard or s.id == self._user
        return s.id in self._groups

    def _walk(self, node: ObjectRef, memo: dict[ObjectRef, bool], local: bool) -> bool:
        # Iterative post-order over the parent chain so deep hierarchies cannot hit the
        # recursion limit. ``local`` is this node's own contribution.
        if node in memo:
            return memo[node]
        stack: list[tuple[ObjectRef, bool]] = [(node, False)]
        on_path: set[ObjectRef] = set()
        while stack:
            n, expanded = stack.pop()
            if n in memo:
                continue
            if expanded:
                on_path.discard(n)
                here = self._local_granted(n) if local else self._local_blocked(n)
                memo[n] = here or any(memo[p] for p in self._ix.parents.get(n, ()))
                continue
            if n in on_path:
                raise CycleError(f"parent cycle through {n}")
            on_path.add(n)
            stack.append((n, True))
            stack.extend((p, False) for p in self._ix.parents.get(n, ()) if p not in memo)
        return memo[node]

    def _local_granted(self, n: ObjectRef) -> bool:
        return any(self._matches(s) for s in self._ix.viewers.get(n, ()))

    def _local_blocked(self, n: ObjectRef) -> bool:
        if n not in self._ix.restricted:
            return False
        return not any(self._matches(s) for s in self._ix.restriction.get(n, ()))

    def can_view(self, node: ObjectRef) -> bool:
        return self._walk(node, self._granted, local=True) and not self._walk(
            node, self._blocked, local=False
        )


class Bulk:
    """All-users-at-once evaluation. Bit ``i`` of a mask is ``users[i]``."""

    def __init__(self, ix: _Index, users: Sequence[str]) -> None:
        self._ix = ix
        self.users = list(users)
        self.index = {u: i for i, u in enumerate(self.users)}
        if len(self.index) != len(self.users):
            raise ValueError("duplicate users")
        self.all_mask = (1 << len(self.users)) - 1
        self._group_bits = self._groups_fixpoint()
        self._granted: dict[ObjectRef, int] = {}
        self._blocked: dict[ObjectRef, int] = {}

    def _groups_fixpoint(self) -> dict[str, int]:
        direct: dict[str, int] = defaultdict(int)
        children: dict[str, list[str]] = defaultdict(list)
        for g, subs in self._ix.members.items():
            for s in subs:
                if s.type is ObjectType.USER:
                    i = self.index.get(s.id)
                    if i is not None:
                        direct[g] |= 1 << i
                else:
                    children[g].append(s.id)
        bits = dict(direct)
        changed = True
        while changed:
            changed = False
            for g, cs in children.items():
                new = bits.get(g, 0)
                for c in cs:
                    new |= bits.get(c, 0)
                if new != bits.get(g, 0):
                    bits[g] = new
                    changed = True
        return bits

    def subject_bits(self, s: SubjectRef) -> int:
        if s.type is ObjectType.USER:
            if s.is_wildcard:
                return self.all_mask
            i = self.index.get(s.id)
            return 0 if i is None else 1 << i
        return self._group_bits.get(s.id, 0)

    def _walk(self, node: ObjectRef, memo: dict[ObjectRef, int], granted: bool) -> int:
        if node in memo:
            return memo[node]
        stack: list[tuple[ObjectRef, bool]] = [(node, False)]
        on_path: set[ObjectRef] = set()
        while stack:
            n, expanded = stack.pop()
            if n in memo:
                continue
            if expanded:
                on_path.discard(n)
                here = 0
                if granted:
                    for s in self._ix.viewers.get(n, ()):
                        here |= self.subject_bits(s)
                elif n in self._ix.restricted:
                    passing = 0
                    for s in self._ix.restriction.get(n, ()):
                        passing |= self.subject_bits(s)
                    here = self.all_mask & ~passing
                for p in self._ix.parents.get(n, ()):
                    here |= memo[p]
                memo[n] = here
                continue
            if n in on_path:
                raise CycleError(f"parent cycle through {n}")
            on_path.add(n)
            stack.append((n, True))
            stack.extend((p, False) for p in self._ix.parents.get(n, ()) if p not in memo)
        return memo[node]

    def can_view_mask(self, node: ObjectRef) -> int:
        return self._walk(node, self._granted, True) & ~self._walk(node, self._blocked, False)

    def doc_masks(self) -> dict[str, int]:
        return {n.id: self.can_view_mask(n) for n in self._ix.nodes if n.type is ObjectType.DOC}
