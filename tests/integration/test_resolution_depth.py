"""Hierarchies deeper than an engine's resolution limit must never fail open.

Two shapes, each a chain of containers N levels deep (ADR-0002, "Engines cap resolution
depth"):

- **Deep grant**: the grant sits at the top. The oracle says *allow*. An engine may deny past
  its depth limit (lost recall, closed) or allow (correct); either is safe.
- **Deep restriction**: the restriction sits at the top and the user holds a direct grant on
  the leaf. The oracle says *deny*. An engine that gave up evaluating ``blocked`` and allowed
  would leak, so both check and list must deny (or fail the request).

Measured on OpenFGA v1.21.0 and SpiceDB v1.56.2 (docs/research/2026-10-28-resolution-depth.md).
Needs `make up-full`.
"""

from __future__ import annotations

import pytest

from tuplegate import ids
from tuplegate.authz.base import AuthzBackend, AuthzError, Consistency
from tuplegate.model import NodeAcl, Tuple, container, doc, user

pytestmark = pytest.mark.integration


def _chain(name: str, depth: int, *, restrict_top: bool) -> tuple[set[Tuple], str]:
    tuples: set[Tuple] = set()
    top = container(f"{name}/0")
    if restrict_top:
        tuples |= NodeAcl(top, restriction=frozenset({user("someone-else@x")})).tuples()
    else:
        tuples |= NodeAcl(top, viewers=frozenset({user("u@x")})).tuples()
    prev = top
    for i in range(1, depth):
        node = container(f"{name}/{i}")
        tuples |= NodeAcl(node, parent=prev).tuples()
        prev = node
    leaf = doc(f"{name}/leaf")
    viewers = frozenset({user("u@x")}) if restrict_top else frozenset()
    tuples |= NodeAcl(leaf, parent=prev, viewers=viewers).tuples()
    return tuples, ids.encode(leaf.id)


def _listed(engine: AuthzBackend) -> frozenset[str] | None:
    try:
        return engine.list_docs("u@x", Consistency.FULL).acl_ids
    except AuthzError:
        return None  # the whole request failed: closed


@pytest.mark.parametrize("depth", [10, 30, 60])
@pytest.mark.parametrize("engine_name", ["openfga", "spicedb"])
def test_deep_restrictions_never_fail_open(
    request: pytest.FixtureRequest, engine_name: str, depth: int
) -> None:
    engine: AuthzBackend = request.getfixturevalue(engine_name)
    tuples, leaf = _chain(f"r{depth}", depth, restrict_top=True)
    engine.write(tuples, [])
    assert engine.check_docs("u@x", [leaf], Consistency.FULL) == {leaf: False}
    listed = _listed(engine)
    assert listed is None or leaf not in listed


# Measured 2026-10-28 on the pinned images; a change here means an engine bump changed
# behaviour and docs/research/2026-10-28-resolution-depth.md needs updating.
DEEP_GRANT_BEHAVIOUR = {
    # Check denies from 25 containers; streamed ListObjects still lists the doc.
    "openfga": (False, True),
    # Check denies from 48; LookupResources fails the whole request from 22.
    "spicedb": (False, None),
}


@pytest.mark.parametrize("engine_name", ["openfga", "spicedb"])
def test_deep_grants_fail_closed_as_measured(
    request: pytest.FixtureRequest, engine_name: str
) -> None:
    engine: AuthzBackend = request.getfixturevalue(engine_name)
    shallow, shallow_leaf = _chain("g10", 10, restrict_top=False)
    deep, deep_leaf = _chain("g60", 60, restrict_top=False)
    engine.write(shallow | deep, [])
    assert engine.check_docs("u@x", [shallow_leaf], Consistency.FULL) == {shallow_leaf: True}
    checked = engine.check_docs("u@x", [deep_leaf], Consistency.FULL)[deep_leaf]
    listed = _listed(engine)
    seen = None if listed is None else deep_leaf in listed
    assert (checked, seen) == DEEP_GRANT_BEHAVIOUR[engine_name]
