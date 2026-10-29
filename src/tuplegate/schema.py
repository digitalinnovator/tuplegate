"""The authorization schema, defined once and rendered for each engine.

One definition produces the OpenFGA JSON model (what the API accepts), the OpenFGA DSL and
the SpiceDB schema (what people read). The rendered files are committed under ``schema/``
and a golden test fails if they drift from this module. The in-memory oracle (``oracle.py``)
implements the same semantics independently, and differential tests hold all three to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tuplegate.model import CAN_VIEW, ObjectType, Relation

# --- A small expression IR covering the userset rewrites we use -----------------------------


@dataclass(frozen=True)
class SubjectType:
    type: ObjectType
    wildcard: bool = False
    relation: str | None = None


@dataclass(frozen=True)
class Direct:
    allowed: tuple[SubjectType, ...]


@dataclass(frozen=True)
class Computed:
    relation: str


@dataclass(frozen=True)
class FromParent:
    """Tuple-to-userset: ``relation`` evaluated on the object(s) named by ``via``."""

    via: str
    relation: str


@dataclass(frozen=True)
class Union:
    children: tuple[Expr, ...]


@dataclass(frozen=True)
class Difference:
    base: Expr
    subtract: Expr


Expr = Direct | Computed | FromParent | Union | Difference

USER = SubjectType(ObjectType.USER)
ANY_USER = SubjectType(ObjectType.USER, wildcard=True)
GROUP_MEMBERS = SubjectType(ObjectType.GROUP, relation=Relation.MEMBER)
CONTAINER = SubjectType(ObjectType.CONTAINER)


def _node_relations() -> dict[str, Expr]:
    return {
        Relation.PARENT: Direct((CONTAINER,)),
        Relation.VIEWER: Direct((USER, ANY_USER, GROUP_MEMBERS)),
        Relation.RESTRICTED: Direct((ANY_USER,)),
        Relation.RESTRICTION_MEMBER: Direct((USER, GROUP_MEMBERS)),
        "granted": Union((Computed(Relation.VIEWER), FromParent(Relation.PARENT, "granted"))),
        "blocked_here": Difference(
            Computed(Relation.RESTRICTED), Computed(Relation.RESTRICTION_MEMBER)
        ),
        "blocked": Union((Computed("blocked_here"), FromParent(Relation.PARENT, "blocked"))),
        CAN_VIEW: Difference(Computed("granted"), Computed("blocked")),
    }


SCHEMA: dict[ObjectType, dict[str, Expr]] = {
    ObjectType.USER: {},
    ObjectType.GROUP: {Relation.MEMBER: Direct((USER, GROUP_MEMBERS))},
    ObjectType.CONTAINER: _node_relations(),
    ObjectType.DOC: _node_relations(),
}

# --- OpenFGA JSON ---------------------------------------------------------------------------


def _fga_expr(e: Expr) -> dict[str, Any]:
    match e:
        case Direct():
            return {"this": {}}
        case Computed(relation=r):
            return {"computedUserset": {"relation": r}}
        case FromParent(via=v, relation=r):
            return {
                "tupleToUserset": {
                    "tupleset": {"relation": v},
                    "computedUserset": {"relation": r},
                }
            }
        case Union(children=cs):
            return {"union": {"child": [_fga_expr(c) for c in cs]}}
        case Difference(base=b, subtract=s):
            return {"difference": {"base": _fga_expr(b), "subtract": _fga_expr(s)}}


def _fga_subject_type(s: SubjectType) -> dict[str, Any]:
    out: dict[str, Any] = {"type": str(s.type)}
    if s.wildcard:
        out["wildcard"] = {}
    if s.relation:
        out["relation"] = str(s.relation)
    return out


def openfga_model() -> dict[str, Any]:
    type_defs: list[dict[str, Any]] = []
    for t, rels in SCHEMA.items():
        td: dict[str, Any] = {"type": str(t)}
        if rels:
            td["relations"] = {str(name): _fga_expr(e) for name, e in rels.items()}
            td["metadata"] = {
                "relations": {
                    str(name): {
                        "directly_related_user_types": (
                            [_fga_subject_type(s) for s in e.allowed]
                            if isinstance(e, Direct)
                            else []
                        )
                    }
                    for name, e in rels.items()
                }
            }
        type_defs.append(td)
    return {"schema_version": "1.1", "type_definitions": type_defs}


# --- OpenFGA DSL (documentation; the API takes JSON) ----------------------------------------


def _dsl_expr(e: Expr, top: bool = True) -> str:
    match e:
        case Direct(allowed=a):
            parts = []
            for s in a:
                if s.wildcard:
                    parts.append(f"{s.type}:*")
                elif s.relation:
                    parts.append(f"{s.type}#{s.relation}")
                else:
                    parts.append(str(s.type))
            return "[" + ", ".join(parts) + "]"
        case Computed(relation=r):
            return str(r)
        case FromParent(via=v, relation=r):
            return f"{r} from {v}"
        case Union(children=cs):
            inner = " or ".join(_dsl_expr(c, top=False) for c in cs)
            return inner if top else f"({inner})"
        case Difference(base=b, subtract=s):
            inner = f"{_dsl_expr(b, top=False)} but not {_dsl_expr(s, top=False)}"
            return inner if top else f"({inner})"


def openfga_dsl() -> str:
    lines = ["model", "  schema 1.1"]
    for t, rels in SCHEMA.items():
        lines += ["", f"type {t}"]
        if rels:
            lines.append("  relations")
            lines += [f"    define {name}: {_dsl_expr(e)}" for name, e in rels.items()]
    return "\n".join(lines) + "\n"


# --- SpiceDB schema -------------------------------------------------------------------------


def _zed_expr(e: Expr, top: bool = True) -> str:
    match e:
        case Direct():
            raise ValueError("direct relations are declared, not expressed")
        case Computed(relation=r):
            return str(r)
        case FromParent(via=v, relation=r):
            return f"{v}->{r}"
        case Union(children=cs):
            inner = " + ".join(_zed_expr(c, top=False) for c in cs)
            return inner if top else f"({inner})"
        case Difference(base=b, subtract=s):
            inner = f"{_zed_expr(b, top=False)} - {_zed_expr(s, top=False)}"
            return inner if top else f"({inner})"


def spicedb_schema() -> str:
    blocks = []
    for t, rels in SCHEMA.items():
        if not rels:
            blocks.append(f"definition {t} {{}}")
            continue
        body = []
        for name, e in rels.items():
            if isinstance(e, Direct):
                allowed = " | ".join(
                    f"{s.type}:*"
                    if s.wildcard
                    else (f"{s.type}#{s.relation}" if s.relation else str(s.type))
                    for s in e.allowed
                )
                body.append(f"    relation {name}: {allowed}")
            else:
                body.append(f"    permission {name} = {_zed_expr(e)}")
        blocks.append(f"definition {t} {{\n" + "\n".join(body) + "\n}")
    return "\n\n".join(blocks) + "\n"
