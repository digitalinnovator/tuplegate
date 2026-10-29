from __future__ import annotations

from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from rag_authz.audit import MemoryAuditSink
from rag_authz.authz.base import AuthzError, Consistency, ListResult
from rag_authz.authz.memory import MemoryBackend
from rag_authz.config import Settings
from rag_authz.index.base import ChunkRecord
from rag_authz.index.memory import MemoryIndex
from rag_authz.model import NodeAcl, doc, user
from rag_authz.retrieval import AuthorizedRetriever, RetrievalConfig
from rag_authz.server import Callers, create_app, hash_token
from rag_authz.testing import HashingEmbedder

EMB = HashingEmbedder(32)
TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class BrokenBackend(MemoryBackend):
    def list_docs(self, user_id: str, consistency: Consistency) -> ListResult:
        raise AuthzError("engine down")

    def check_docs(
        self, user_id: str, acl_ids: Sequence[str], consistency: Consistency
    ) -> dict[str, bool]:
        raise AuthzError("engine down")


def _setup(
    backend: MemoryBackend | None = None, **cfg: object
) -> tuple[TestClient, MemoryAuditSink]:
    b = backend or MemoryBackend()
    for d, who in (("alpha", "alice@x"), ("beta", "bob@x")):
        b.write(NodeAcl(doc(d), viewers=frozenset({user(who)})).tuples(), [])
    ix = MemoryIndex()
    ix.upsert(
        [
            ChunkRecord(
                "alpha#0",
                "alpha",
                "deploy keys rotate monthly",
                EMB.embed_one("deploy keys rotate monthly"),
            ),
            ChunkRecord(
                "beta#0",
                "beta",
                "deploy keys are in the vault",
                EMB.embed_one("deploy keys are in the vault"),
            ),
        ]
    )
    sink = MemoryAuditSink()
    r = AuthorizedRetriever(b, ix, RetrievalConfig(**cfg), sink)  # type: ignore[arg-type]
    app = create_app(r, Callers({hash_token(TOKEN): "chat-app"}), embedder=EMB.embed_one)
    return TestClient(app), sink


def test_each_user_gets_only_their_docs_and_the_caller_is_audited() -> None:
    client, sink = _setup()
    for who, expected in (("alice@x", ["alpha"]), ("bob@x", ["beta"]), ("eve@x", [])):
        r = client.post(
            "/v1/retrieve", json={"subject": who, "query": "deploy keys", "k": 5}, headers=AUTH
        )
        assert r.status_code == 200, r.text
        assert [h["doc_id"] for h in r.json()["hits"]] == expected
    assert [rec.caller for rec in sink.records] == ["chat-app"] * 3


def test_vector_requests_and_strategies() -> None:
    client, _ = _setup()
    for strategy in ("pre_filter", "post_filter", "iterative"):
        r = client.post(
            "/v1/retrieve",
            json={"subject": "bob@x", "vector": EMB.embed_one("vault"), "strategy": strategy},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["stats"]["strategy"] == strategy
        assert [h["doc_id"] for h in r.json()["hits"]] == ["beta"]


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
def test_requires_a_caller_token(headers: dict[str, str]) -> None:
    client, sink = _setup()
    r = client.post("/v1/retrieve", json={"subject": "alice@x", "query": "x"}, headers=headers)
    assert r.status_code == 401
    assert sink.records == []


@pytest.mark.parametrize("subject", ["*", "~github/octocat"])
def test_refuses_unclaimable_subjects(subject: str) -> None:
    client, _ = _setup()
    r = client.post("/v1/retrieve", json={"subject": subject, "query": "x"}, headers=AUTH)
    assert r.status_code == 400


def test_unfiltered_is_not_reachable_over_http() -> None:
    client, _ = _setup(allow_unfiltered=True)
    r = client.post(
        "/v1/retrieve",
        json={"subject": "alice@x", "query": "x", "strategy": "unfiltered"},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_engine_errors_fail_closed() -> None:
    client, sink = _setup(BrokenBackend())
    r = client.post("/v1/retrieve", json={"subject": "alice@x", "query": "deploy"}, headers=AUTH)
    assert r.status_code == 503
    assert "hits" not in r.json()
    assert sink.records[-1].error is not None
    assert sink.records[-1].returned_doc_ids == []


def test_needs_vector_or_embedder() -> None:
    b = MemoryBackend()
    app = create_app(AuthorizedRetriever(b, MemoryIndex()), Callers({hash_token(TOKEN): "a"}))
    r = TestClient(app).post(
        "/v1/retrieve", json={"subject": "alice@x", "query": "x"}, headers=AUTH
    )
    assert r.status_code == 422


def test_refuses_to_start_without_callers() -> None:
    with pytest.raises(ValueError, match="caller token"):
        create_app(AuthorizedRetriever(MemoryBackend(), MemoryIndex()), Callers({}))


def test_health_endpoints() -> None:
    client, _ = _setup()
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {
            "RAG_AUTHZ_CALLERS": f"chat:{hash_token('t')}",
            "RAG_AUTHZ_ENGINE": "spicedb",
            "RAG_AUTHZ_DIM": "384",
            "RAG_AUTHZ_INDEX": "qdrant",
            "UNRELATED": "x",
        }
    )
    assert (s.engine, s.index, s.dim) == ("spicedb", "qdrant", 384)
    assert Callers.from_spec(s.callers).identify("t") == "chat"
    with pytest.raises(ValueError, match="name:sha256hex"):
        Callers.from_spec("chat:short")
