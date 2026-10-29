"""The oracle is the specification, so these tests read as the ACL semantics in examples."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_authz.model import (
    GroupMembers,
    NodeAcl,
    Tuple,
    container,
    doc,
    group,
    members,
    public,
    user,
)
from rag_authz.oracle import CycleError, Oracle


def build(*parts: NodeAcl | GroupMembers) -> Oracle:
    tuples: set[Tuple] = set()
    for p in parts:
        tuples |= p.tuples()
    return Oracle(tuples)


def test_direct_grant_and_inheritance() -> None:
    o = build(
        NodeAcl(container("repo"), viewers=frozenset({user("alice")})),
        NodeAcl(doc("readme"), parent=container("repo")),
    )
    assert o.can_view("alice", doc("readme"))
    assert not o.can_view("bob", doc("readme"))


def test_inheritance_break_drops_ancestor_grants() -> None:
    # Drive limited-access folder: no parent tuple, only its own grants apply.
    o = build(
        NodeAcl(container("drive"), viewers=frozenset({members("all")})),
        GroupMembers(group("all"), frozenset({user("alice"), user("bob")})),
        NodeAcl(container("hr"), parent=None, viewers=frozenset({user("carol")})),
        NodeAcl(doc("salaries"), parent=container("hr")),
    )
    assert not o.can_view("alice", doc("salaries"))
    assert o.can_view("carol", doc("salaries"))


def test_nested_groups_are_transitive() -> None:
    # GitHub: child team members inherit the parent team's access.
    o = build(
        GroupMembers(group("eng"), frozenset({members("backend")})),
        GroupMembers(group("backend"), frozenset({members("db")})),
        GroupMembers(group("db"), frozenset({user("dana")})),
        NodeAcl(doc("design"), viewers=frozenset({members("eng")})),
    )
    assert o.can_view("dana", doc("design"))
    assert o.groups_of("dana") == {"db", "backend", "eng"}


def test_group_cycles_resolve_to_union() -> None:
    o = build(
        GroupMembers(group("a"), frozenset({members("b"), user("u1")})),
        GroupMembers(group("b"), frozenset({members("a"), user("u2")})),
        NodeAcl(doc("d"), viewers=frozenset({members("a")})),
    )
    assert o.can_view("u1", doc("d"))
    assert o.can_view("u2", doc("d"))
    bulk = o.bulk(["u1", "u2", "u3"])
    assert bulk.can_view_mask(doc("d")) == 0b011


def test_public_wildcard_grants_everyone() -> None:
    o = build(NodeAcl(doc("blog"), viewers=frozenset({public()})))
    assert o.can_view("anyone@else", doc("blog"))


def test_restrictions_are_intersective_down_the_tree() -> None:
    # Confluence: a user must pass the view restriction of every restricted ancestor.
    o = build(
        NodeAcl(container("space"), viewers=frozenset({members("staff")})),
        GroupMembers(group("staff"), frozenset({user("a"), user("b"), user("c")})),
        NodeAcl(
            container("p1"),
            parent=container("space"),
            restriction=frozenset({user("a"), user("b")}),
        ),
        NodeAcl(doc("p2"), parent=container("p1"), restriction=frozenset({user("b"), user("c")})),
    )
    assert not o.can_view("a", doc("p2"))  # fails p2's restriction
    assert o.can_view("b", doc("p2"))  # passes both
    assert not o.can_view("c", doc("p2"))  # fails p1's restriction
    assert o.can_view("a", container("p1"))


def test_restriction_does_not_grant() -> None:
    o = build(NodeAcl(doc("d"), restriction=frozenset({user("a")})))
    assert not o.can_view("a", doc("d"))  # passing a restriction is not a grant


def test_direct_grant_cannot_escape_ancestor_restriction() -> None:
    o = build(
        NodeAcl(container("p"), restriction=frozenset({user("a")})),
        NodeAcl(doc("child"), parent=container("p"), viewers=frozenset({user("z")})),
    )
    assert not o.can_view("z", doc("child"))


def test_unknown_doc_and_orphan_parent_fail_closed() -> None:
    o = build(NodeAcl(doc("d"), parent=container("deleted-folder")))
    assert not o.can_view("a", doc("d"))
    assert not o.can_view("a", doc("never-synced"))


def test_invalid_subjects_refused() -> None:
    o = build(NodeAcl(doc("d"), viewers=frozenset({public()})))
    for bad in ("*", "~github/octocat"):
        with pytest.raises(ValueError, match="not a valid query subject"):
            o.can_view(bad, doc("d"))


def test_parent_cycle_detected() -> None:
    o = build(
        NodeAcl(container("a"), parent=container("b")),
        NodeAcl(container("b"), parent=container("a")),
    )
    with pytest.raises(CycleError):
        o.can_view("u", container("a"))


def test_deep_hierarchy_does_not_recurse() -> None:
    parts: list[NodeAcl | GroupMembers] = [NodeAcl(container("c0"), viewers=frozenset({user("u")}))]
    parts += [NodeAcl(container(f"c{i}"), parent=container(f"c{i - 1}")) for i in range(1, 5000)]
    parts.append(NodeAcl(doc("leaf"), parent=container("c4999")))
    o = build(*parts)
    assert o.can_view("u", doc("leaf"))
    assert o.bulk(["u", "v"]).can_view_mask(doc("leaf")) == 0b01


# --- bulk bitsets agree with per-user evaluation on random organisations ---------------------

USERS = [f"u{i}" for i in range(6)]
GROUPS = [f"g{i}" for i in range(4)]
NODES = [f"n{i}" for i in range(8)]


@st.composite
def random_org(draw: st.DrawFn) -> Oracle:
    subjects = st.one_of(
        st.sampled_from(USERS).map(user),
        st.sampled_from(GROUPS).map(members),
    )
    parts: list[NodeAcl | GroupMembers] = []
    for g in GROUPS:
        ms = draw(st.frozensets(subjects, max_size=4))
        parts.append(GroupMembers(group(g), frozenset(m for m in ms if m.id != g)))
    for i, n in enumerate(NODES):
        # Parents only point at earlier nodes, so the hierarchy is a forest.
        parent = draw(st.one_of(st.none(), st.sampled_from(NODES[:i]))) if i else None
        viewers = draw(st.frozensets(st.one_of(subjects, st.just(public())), max_size=3))
        restriction = draw(st.one_of(st.none(), st.frozensets(subjects, max_size=3)))
        make = doc if i % 2 else container
        if make is doc:
            parts.append(
                NodeAcl(doc(n), None if parent is None else container(parent), viewers, restriction)
            )
            parts.append(
                NodeAcl(
                    container(n),
                    None if parent is None else container(parent),
                    viewers,
                    restriction,
                )
            )
        else:
            parts.append(
                NodeAcl(
                    container(n),
                    None if parent is None else container(parent),
                    viewers,
                    restriction,
                )
            )
    return build(*parts)


@settings(max_examples=200, deadline=None)
@given(random_org())
def test_bulk_matches_per_user(o: Oracle) -> None:
    bulk = o.bulk(USERS)
    for n in NODES:
        for ref in (container(n), doc(n)):
            mask = bulk.can_view_mask(ref)
            for i, u in enumerate(USERS):
                assert bool(mask >> i & 1) == o.can_view(u, ref), (u, ref)
