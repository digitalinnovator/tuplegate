"""Canonical ACL model: the source-neutral vocabulary every connector normalises into.

The model is deliberately tiny (ADR-0002). Four object types, five stored relations, and
every source-specific rule (Drive roles, GitHub team nesting, Confluence page restrictions)
is expressed by *which* tuples a connector writes, never by extending the schema.

- ``user``: a canonical identity, as the querying application names it (usually the IdP
  subject or e-mail). Source principals are mapped to canonical users at sync time.
- ``group``: anything with members: GitHub teams, Google groups, Confluence groups, domains.
  ``member`` may point at another group's members, which is how nesting is expressed.
- ``container``: anything that can have children: Drive folders and shared drives, GitHub
  orgs and repos, Confluence spaces and pages-with-children.
- ``doc``: a retrievable unit. Every vector chunk carries exactly one ``doc`` id.

Relations on ``container`` and ``doc``:

- ``parent``: written only when the node *inherits* from its structural parent. Breaking
  inheritance (Drive limited-access folders) is the absence of this tuple.
- ``viewer``: additive grants (user, ``user:*`` for public, or ``group#member``).
- ``restricted`` + ``restriction_member``: an intersective restriction. When a node carries
  ``restricted@user:*`` only users in ``restriction_member`` pass it, and the restriction
  applies to every descendant (Confluence view restrictions).

Computed: ``can_view = granted - blocked`` where ``granted = viewer + parent->granted`` and
``blocked = (restricted - restriction_member) + parent->blocked``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

WILDCARD = "*"
# Canonical ids for principals that could not be mapped to a canonical user start with this
# prefix. The retrieval API refuses it as a query subject, so an unmapped principal's grants
# can never be claimed by a caller.
UNMAPPED_PREFIX = "~"


def _check_id(id_: str) -> None:
    # NUL is reserved for the codec's raw digest form (ids.RAW_PREFIX); no source id has one.
    if "\x00" in id_ and not (id_.startswith("\x00raw:=h") and id_.count("\x00") == 1):
        raise ValueError(f"ids may not contain NUL: {id_!r}")


class ObjectType(StrEnum):
    USER = "user"
    GROUP = "group"
    CONTAINER = "container"
    DOC = "doc"


class Relation(StrEnum):
    MEMBER = "member"
    PARENT = "parent"
    VIEWER = "viewer"
    RESTRICTED = "restricted"
    RESTRICTION_MEMBER = "restriction_member"


# The permission every retrieval decision asks for.
CAN_VIEW = "can_view"

NODE_TYPES = frozenset({ObjectType.CONTAINER, ObjectType.DOC})


@dataclass(frozen=True, slots=True, order=True)
class ObjectRef:
    type: ObjectType
    id: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("object id must be non-empty")
        if self.id == WILDCARD:
            raise ValueError("'*' is only valid as a subject id")
        _check_id(self.id)

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass(frozen=True, slots=True, order=True)
class SubjectRef:
    """The subject side of a tuple: a user, all users (``user:*``) or a group's members."""

    type: ObjectType
    id: str
    relation: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("subject id must be non-empty")
        _check_id(self.id)
        if self.type is ObjectType.USER and self.relation is not None:
            raise ValueError("user subjects take no relation")
        if self.type is ObjectType.GROUP and self.relation != Relation.MEMBER:
            raise ValueError("group subjects must be group#member")
        if self.type not in (ObjectType.USER, ObjectType.GROUP):
            raise ValueError(f"subjects must be users or groups, not {self.type}")
        if self.id == WILDCARD and self.type is not ObjectType.USER:
            raise ValueError("only user:* is a valid wildcard")

    @property
    def is_wildcard(self) -> bool:
        return self.id == WILDCARD

    def __str__(self) -> str:
        base = f"{self.type}:{self.id}"
        return f"{base}#{self.relation}" if self.relation else base


def user(id_: str) -> SubjectRef:
    return SubjectRef(ObjectType.USER, id_)


def public() -> SubjectRef:
    return SubjectRef(ObjectType.USER, WILDCARD)


def members(group_id: str) -> SubjectRef:
    return SubjectRef(ObjectType.GROUP, group_id, Relation.MEMBER)


def container(id_: str) -> ObjectRef:
    return ObjectRef(ObjectType.CONTAINER, id_)


def doc(id_: str) -> ObjectRef:
    return ObjectRef(ObjectType.DOC, id_)


def group(id_: str) -> ObjectRef:
    return ObjectRef(ObjectType.GROUP, id_)


# Which subject shapes each relation accepts. Mirrors the engine schemas (schema.py) and is
# enforced at construction so a connector bug fails loudly instead of writing a tuple the
# engine would reject mid-batch.
_ALLOWED: dict[tuple[ObjectType, Relation], frozenset[tuple[ObjectType, bool, str | None]]] = {}
_GRANTEE = frozenset(
    {
        (ObjectType.USER, False, None),
        (ObjectType.USER, True, None),
        (ObjectType.GROUP, False, Relation.MEMBER),
    }
)
_MEMBER = frozenset({(ObjectType.USER, False, None), (ObjectType.GROUP, False, Relation.MEMBER)})
_ALLOWED[(ObjectType.GROUP, Relation.MEMBER)] = _MEMBER
for _t in NODE_TYPES:
    _ALLOWED[(_t, Relation.VIEWER)] = _GRANTEE
    _ALLOWED[(_t, Relation.RESTRICTED)] = frozenset({(ObjectType.USER, True, None)})
    _ALLOWED[(_t, Relation.RESTRICTION_MEMBER)] = _MEMBER


@dataclass(frozen=True, slots=True, order=True)
class Tuple:
    """One relationship. ``parent`` tuples point at a container through ``parent_ref``."""

    object: ObjectRef
    relation: Relation
    subject: SubjectRef | None = None
    parent_ref: ObjectRef | None = None

    def __post_init__(self) -> None:
        if self.relation is Relation.PARENT:
            if self.object.type not in NODE_TYPES:
                raise ValueError("only containers and docs have parents")
            if self.parent_ref is None or self.parent_ref.type is not ObjectType.CONTAINER:
                raise ValueError("parent must be a container")
            if self.subject is not None:
                raise ValueError("parent tuples take parent_ref, not subject")
            if self.parent_ref == self.object:
                raise ValueError("a node cannot be its own parent")
            return
        if self.subject is None or self.parent_ref is not None:
            raise ValueError(f"{self.relation} tuples take a subject")
        allowed = _ALLOWED.get((self.object.type, self.relation))
        shape = (self.subject.type, self.subject.is_wildcard, self.subject.relation)
        if allowed is None or shape not in allowed:
            raise ValueError(f"{self.object.type}#{self.relation} does not accept {self.subject}")
        if (
            self.object.type is ObjectType.GROUP
            and self.subject.type is ObjectType.GROUP
            and self.subject.id == self.object.id
        ):
            raise ValueError("a group cannot contain itself")

    def __str__(self) -> str:
        rhs = self.parent_ref if self.relation is Relation.PARENT else self.subject
        return f"{self.object}#{self.relation}@{rhs}"


def parent_tuple(child: ObjectRef, parent: ObjectRef) -> Tuple:
    return Tuple(child, Relation.PARENT, parent_ref=parent)


@dataclass(frozen=True, slots=True)
class NodeAcl:
    """The normalised ACL of one container or doc, as a connector sees it at one moment.

    ``restriction`` is ``None`` when the node applies no restriction of its own. An empty
    frozenset is a restriction nobody passes, which is legal and fails closed.

    ``expiring`` holds viewer grants that end at a known time (Drive ``expirationTime``). A
    grant already expired is never written, and the sync engine's expiry sweep deletes the rest
    when they fall due (ADR-0002 §6). A subject that is also in ``viewers`` keeps access.
    """

    ref: ObjectRef
    parent: ObjectRef | None = None
    viewers: frozenset[SubjectRef] = frozenset()
    restriction: frozenset[SubjectRef] | None = None
    expiring: frozenset[tuple[SubjectRef, datetime]] = frozenset()

    def __post_init__(self) -> None:
        if self.ref.type not in NODE_TYPES:
            raise ValueError("NodeAcl is for containers and docs")
        if any(exp.tzinfo is None for _, exp in self.expiring):
            raise ValueError("expiry times must be timezone-aware")

    def live_expiring(self, now: datetime) -> dict[SubjectRef, datetime]:
        """Expiring grants still in force at ``now``, latest expiry per subject."""
        out: dict[SubjectRef, datetime] = {}
        for s, exp in self.expiring:
            if exp > now and s not in self.viewers and (s not in out or exp > out[s]):
                out[s] = exp
        return out

    def tuples(self, now: datetime | None = None) -> frozenset[Tuple]:
        out: set[Tuple] = set()
        if self.parent is not None:
            out.add(parent_tuple(self.ref, self.parent))
        out.update(Tuple(self.ref, Relation.VIEWER, s) for s in self.viewers)
        if self.expiring:
            live = self.live_expiring(now or datetime.now(UTC))
            out.update(Tuple(self.ref, Relation.VIEWER, s) for s in live)
        if self.restriction is not None:
            out.add(Tuple(self.ref, Relation.RESTRICTED, public()))
            out.update(Tuple(self.ref, Relation.RESTRICTION_MEMBER, s) for s in self.restriction)
        return frozenset(out)


@dataclass(frozen=True, slots=True)
class GroupMembers:
    ref: ObjectRef
    members: frozenset[SubjectRef] = frozenset()

    def __post_init__(self) -> None:
        if self.ref.type is not ObjectType.GROUP:
            raise ValueError("GroupMembers is for groups")

    def tuples(self) -> frozenset[Tuple]:
        return frozenset(Tuple(self.ref, Relation.MEMBER, m) for m in self.members)


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """Text a connector fetched for a doc. Chunking and embedding are the caller's choice."""

    text: str
    version: str
    title: str = ""
    url: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


# --- Connector events -----------------------------------------------------------------------
# Each carries ``changed_at``: when the source says the change happened, if it says. The sync
# engine measures propagation lag from it.


@dataclass(frozen=True, slots=True)
class UpsertNode:
    acl: NodeAcl
    content: DocumentContent | None = None
    changed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeleteNode:
    ref: ObjectRef
    changed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UpsertGroup:
    group: GroupMembers
    changed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeleteGroup:
    ref: ObjectRef
    changed_at: datetime | None = None


SyncEvent = UpsertNode | DeleteNode | UpsertGroup | DeleteGroup
