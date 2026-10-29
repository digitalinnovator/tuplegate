import pytest

from rag_authz.model import (
    NodeAcl,
    ObjectRef,
    ObjectType,
    Relation,
    SubjectRef,
    Tuple,
    container,
    doc,
    group,
    members,
    parent_tuple,
    public,
    user,
)


def test_node_acl_tuples() -> None:
    acl = NodeAcl(
        doc("d1"),
        parent=container("f1"),
        viewers=frozenset({user("a@x"), members("eng")}),
        restriction=frozenset({members("legal")}),
    )
    got = {str(t) for t in acl.tuples()}
    assert got == {
        "doc:d1#parent@container:f1",
        "doc:d1#viewer@user:a@x",
        "doc:d1#viewer@group:eng#member",
        "doc:d1#restricted@user:*",
        "doc:d1#restriction_member@group:legal#member",
    }


def test_empty_restriction_is_a_restriction_nobody_passes() -> None:
    acl = NodeAcl(doc("d1"), restriction=frozenset())
    assert {str(t) for t in acl.tuples()} == {"doc:d1#restricted@user:*"}


@pytest.mark.parametrize(
    ("obj", "rel", "subject"),
    [
        (doc("d"), Relation.RESTRICTED, user("a")),  # only user:*
        (doc("d"), Relation.RESTRICTION_MEMBER, public()),  # wildcard never passes a restriction
        (group("g"), Relation.MEMBER, public()),
        (group("g"), Relation.VIEWER, user("a")),
        (group("g"), Relation.MEMBER, members("g")),  # self-membership
    ],
)
def test_invalid_tuples_rejected(obj: ObjectRef, rel: Relation, subject: SubjectRef) -> None:
    with pytest.raises(ValueError, match=r"does not accept|cannot contain itself"):
        Tuple(obj, rel, subject)


def test_parent_rules() -> None:
    with pytest.raises(ValueError, match="must be a container"):
        parent_tuple(doc("d"), doc("e"))
    with pytest.raises(ValueError, match="own parent"):
        parent_tuple(container("c"), container("c"))
    with pytest.raises(ValueError, match="must be a container"):
        Tuple(doc("d"), Relation.PARENT, user("a"))


def test_subject_rules() -> None:
    with pytest.raises(ValueError, match="group#member"):
        SubjectRef(ObjectType.GROUP, "g")
    with pytest.raises(ValueError, match="wildcard"):
        SubjectRef(ObjectType.GROUP, "*", Relation.MEMBER)
    with pytest.raises(ValueError, match="only valid as a subject"):
        ObjectRef(ObjectType.DOC, "*")
