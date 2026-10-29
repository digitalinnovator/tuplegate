"""Ids too long to store readably (hashed by the codec) must sync, re-sync and delete.

Regression test: reads used to fail on hashed ids, which blocked every later sync of that doc
and skipped the reconcile sweep for its whole source. Needs `make up-full`.
"""

from __future__ import annotations

import pytest

from tuplegate import ids
from tuplegate.authz.base import AuthzBackend, Consistency
from tuplegate.index.memory import MemoryIndex
from tuplegate.model import DeleteNode, NodeAcl, UpsertNode, container, doc, members, user
from tuplegate.sync import SyncEngine

pytestmark = pytest.mark.integration

LONG = "github/acme/monorepo/" + "src/main/java/com/acme/" * 12 + "Thing.java"
LONG_GROUP = "github/acme/team/" + "platform-" * 30


@pytest.mark.parametrize("engine_name", ["openfga", "spicedb"])
def test_hashed_ids_roundtrip(request: pytest.FixtureRequest, engine_name: str) -> None:
    engine: AuthzBackend = request.getfixturevalue(engine_name)
    assert ids.is_hashed(ids.encode(LONG))
    sync = SyncEngine(engine, MemoryIndex())
    acl = NodeAcl(
        doc(LONG),
        parent=container("github/acme/monorepo"),
        viewers=frozenset({user("u@x"), members(LONG_GROUP)}),
    )
    assert sync.apply("github", [UpsertNode(acl)]).errors == []
    again = sync.apply("github", [UpsertNode(acl)])
    assert again.errors == []
    assert (again.tuples_written, again.tuples_deleted) == (0, 0)
    a = ids.encode(LONG)
    assert engine.check_docs("u@x", [a], Consistency.FULL) == {a: True}
    gone = sync.apply("github", [DeleteNode(doc(LONG))])
    assert gone.errors == []
    assert gone.tuples_deleted == 3
    assert engine.check_docs("u@x", [a], Consistency.FULL) == {a: False}
