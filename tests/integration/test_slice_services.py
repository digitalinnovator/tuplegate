"""The v0.1 vertical slice against real services:
GitHub (fake API) → tuples in OpenFGA → pgvector pre-filter → LangChain retriever.

The leak-rate check over every engine, index and strategy is in test_leak_matrix.py.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from langchain_core.embeddings import Embeddings

from tests.fakes import IDENTITIES, FakeGitHub
from tuplegate.authz.openfga import OpenFGABackend
from tuplegate.connectors.github import GitHubConnector
from tuplegate.identity import MappingResolver
from tuplegate.index.pgvector import PgVectorIndex
from tuplegate.integrations.langchain import AuthzRetriever
from tuplegate.model import DeleteNode, doc
from tuplegate.retrieval import AuthorizedRetriever
from tuplegate.sync import SyncEngine
from tuplegate.testing import HashingEmbedder

pytestmark = pytest.mark.integration
EMBED = HashingEmbedder(dim=128)
QUESTION = "how do deploy keys rotate?"


class HashEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return EMBED(texts)

    def embed_query(self, text: str) -> list[float]:
        return EMBED.embed_one(text)


def test_github_to_openfga_to_pgvector_to_langchain(
    openfga: OpenFGABackend, pgindex: Callable[..., PgVectorIndex]
) -> None:
    gh = FakeGitHub()
    index = pgindex(128)
    sync = SyncEngine(openfga, index, embedder=EMBED)

    def reconcile() -> None:
        conn = GitHubConnector("acme", MappingResolver(IDENTITIES), client=gh.client())
        rep = sync.reconcile("github", conn.snapshot())
        assert rep.errors == []

    reconcile()
    retriever = AuthzRetriever(
        engine=AuthorizedRetriever(openfga, index), embeddings=HashEmbeddings(), k=3
    )

    def ask(user: str) -> list[str]:
        return [d.metadata["doc_id"] for d in retriever.for_subject(user).invoke(QUESTION)]

    carol, bob = ask("carol@acme.test"), ask("bob@acme.test")
    assert "github/acme/api/README.md" in carol
    assert all("/api/" not in d for d in bob)

    gh.teams["backend"] = ("engineering", [])  # revoke: carol leaves backend
    reconcile()
    assert all("/api/" not in d for d in ask("carol@acme.test"))

    # A source delete removes tuples first, then chunks, and reports its lag.
    rep = sync.apply(
        "github", [DeleteNode(doc("github/acme/website/index.md"), changed_at=datetime.now(UTC))]
    )
    assert rep.chunks_deleted >= 1
    assert rep.lag_summary()["n"] == 1
    assert openfga.read(doc("github/acme/website/index.md")) == frozenset()
