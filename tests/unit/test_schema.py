from pathlib import Path

import pytest

from tuplegate import schema
from tuplegate.cli import SCHEMA_FILES, SchemaFormat, render_schema

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fmt", list(SchemaFormat))
def test_committed_schema_files_match_the_definition(fmt: SchemaFormat) -> None:
    committed = (ROOT / "schema" / SCHEMA_FILES[fmt]).read_text()
    assert committed == render_schema(fmt), "run: uv run tuplegate schema --write-dir schema"


def _refs(e: schema.Expr) -> set[str]:
    match e:
        case schema.Direct():
            return set()
        case schema.Computed(relation=r):
            return {str(r)}
        case schema.FromParent(relation=r):
            return {str(r)}
        case schema.Union(children=cs):
            return set().union(*(_refs(c) for c in cs))
        case schema.Difference(base=b, subtract=s):
            return _refs(b) | _refs(s)


def _recursive(rels: dict[str, schema.Expr]) -> set[str]:
    """Relations that can reach themselves through the rewrite graph."""
    out = set()
    for name in rels:
        seen: set[str] = set()
        frontier = list(_refs(rels[name]))
        while frontier:
            r = frontier.pop()
            if r == name:
                out.add(name)
                break
            if r in seen or r not in rels:
                continue
            seen.add(r)
            frontier.extend(_refs(rels[r]))
    return out


def test_no_recursion_inside_exclusion() -> None:
    # OpenFGA's weighted-graph migration rejects recursion inside `but not`/`and`
    # (openfga.dev/blog/weighted-graph-upcoming-changes). Keep recursion in unions only.
    for rels in schema.SCHEMA.values():
        recursive = _recursive(rels)
        for name, e in rels.items():
            if isinstance(e, schema.Difference):
                assert name not in recursive, name


def test_spicedb_type_names_are_valid() -> None:
    import re

    pattern = re.compile(r"^[a-z][a-z0-9_]{1,62}[a-z0-9]$")
    assert all(pattern.match(str(t)) for t in schema.SCHEMA)
