from __future__ import annotations

import pytest

from tuplegate.authz.memory import MemoryBackend
from tuplegate.index.base import ChunkRecord
from tuplegate.index.memory import MemoryIndex
from tuplegate.integrations.llamaindex import AuthzLlamaRetriever
from tuplegate.model import NodeAcl, doc, user
from tuplegate.retrieval import AuthorizedRetriever, Strategy
from tuplegate.testing import HashingEmbedder

EMB = HashingEmbedder(32)


def _engine() -> AuthorizedRetriever:
    b = MemoryBackend()
    b.write(NodeAcl(doc("alpha"), viewers=frozenset({user("alice@x")})).tuples(), [])
    b.write(NodeAcl(doc("beta"), viewers=frozenset({user("bob@x")})).tuples(), [])
    ix = MemoryIndex()
    for d, text in (("alpha", "deploy keys rotate monthly"), ("beta", "deploy keys live in vault")):
        ix.upsert([ChunkRecord(f"{d}#0", d, text, EMB.embed_one(text), {"title": d})])
    return AuthorizedRetriever(b, ix)


@pytest.mark.parametrize("strategy", [None, Strategy.POST_FILTER, Strategy.ITERATIVE])
def test_each_subject_sees_only_their_docs(strategy: Strategy | None) -> None:
    engine = _engine()
    for who, expected in (("alice@x", ["alpha"]), ("bob@x", ["beta"]), ("eve@x", [])):
        r = AuthzLlamaRetriever(engine, EMB.embed_one, subject=who, k=5, strategy=strategy)
        nodes = r.retrieve("deploy keys")
        assert [n.node.metadata["doc_id"] for n in nodes] == expected
        for n in nodes:
            assert n.node.metadata["title"] == expected[0]
            assert n.score is not None


@pytest.mark.parametrize("subject", ["", "*", "~gdrive/x"])
def test_refuses_unclaimable_subjects(subject: str) -> None:
    with pytest.raises(ValueError, match="invalid query subject"):
        AuthzLlamaRetriever(_engine(), EMB.embed_one, subject=subject)
