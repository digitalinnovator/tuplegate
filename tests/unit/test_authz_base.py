from tuplegate.authz.base import safe_batches
from tuplegate.model import (
    NodeAcl,
    Relation,
    Tuple,
    container,
    doc,
    members,
    parent_tuple,
    public,
    user,
)


def _phase_of(batch_index: int, batches: list[tuple[list[Tuple], list[Tuple]]]) -> set[str]:
    w, d = batches[batch_index]
    return {f"+{t.relation}" for t in w} | {f"-{t.relation}" for t in d}


def test_small_change_set_is_one_atomic_request() -> None:
    w = list(NodeAcl(doc("d"), viewers=frozenset({user("a")})).tuples())
    d = [Tuple(doc("d"), Relation.VIEWER, user("b"))]
    assert list(safe_batches(w, d, 100)) == [(w, d)]


def test_large_change_set_narrows_before_widening() -> None:
    revokes = [Tuple(doc("d"), Relation.VIEWER, user(f"old{i}")) for i in range(5)]
    grants = [Tuple(doc("d"), Relation.VIEWER, user(f"new{i}")) for i in range(5)]
    add_restriction = [Tuple(doc("d"), Relation.RESTRICTED, public())]
    lift_restriction = [Tuple(doc("e"), Relation.RESTRICTED, public())]
    move = [parent_tuple(doc("d"), container("new"))]
    batches = list(
        safe_batches(
            grants
            + add_restriction
            + move
            + [Tuple(doc("d"), Relation.RESTRICTION_MEMBER, members("x"))],
            revokes + lift_restriction,
            max_per_request=4,
        )
    )
    phases = [_phase_of(i, batches) for i in range(len(batches))]
    flat = [p for ps in phases for p in sorted(ps)]
    first_widening = min(
        i for i, ps in enumerate(phases) if ps & {"+viewer", "-restricted", "+restriction_member"}
    )
    last_narrowing = max(i for i, ps in enumerate(phases) if ps & {"-viewer", "+restricted"})
    assert last_narrowing < first_widening, flat
    assert all(len(w) + len(d) <= 4 for w, d in batches)
