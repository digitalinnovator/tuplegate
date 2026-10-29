"""The v0.1 vertical slice, in memory: GitHub → tuples → index → LangChain retriever → leaks.

The same flow runs against OpenFGA and pgvector in tests/integration/test_slice_services.py.
"""

from collections.abc import Sequence

import pytest
from langchain_core.embeddings import Embeddings

from rag_authz import leakcheck
from rag_authz.authz.memory import MemoryBackend
from rag_authz.connectors.github import GitHubConnector
from rag_authz.identity import MappingResolver
from rag_authz.index.base import Hit
from rag_authz.index.memory import MemoryIndex
from rag_authz.integrations.langchain import AuthzRetriever
from rag_authz.leakcheck import Probe
from rag_authz.model import doc
from rag_authz.oracle import Oracle
from rag_authz.retrieval import AuthorizedRetriever, Strategy
from rag_authz.sync import SyncEngine
from rag_authz.testing import HashingEmbedder
from tests.fakes import IDENTITIES, FakeGitHub

EMBED = HashingEmbedder(dim=128)
QUESTION = "how do deploy keys rotate?"


class HashEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return EMBED(texts)

    def embed_query(self, text: str) -> list[float]:
        return EMBED.embed_one(text)


class Slice:
    def __init__(self, gh: FakeGitHub) -> None:
        self.gh = gh
        self.authz = MemoryBackend()
        self.index = MemoryIndex()
        self.sync = SyncEngine(self.authz, self.index, embedder=EMBED)
        self.engine = AuthorizedRetriever(self.authz, self.index)
        self.retriever = AuthzRetriever(engine=self.engine, embeddings=HashEmbeddings(), k=3)

    def reconcile(self):  # type: ignore[no-untyped-def]
        conn = GitHubConnector("acme", MappingResolver(IDENTITIES), client=self.gh.client())
        return self.sync.reconcile("github", conn.snapshot())

    def ask(self, user: str, question: str = QUESTION) -> list[str]:
        return [d.metadata["doc_id"] for d in self.retriever.for_subject(user).invoke(question)]


@pytest.fixture
def s() -> Slice:
    sl = Slice(FakeGitHub())
    rep = sl.reconcile()
    assert rep.errors == []
    return sl


def test_two_users_same_question_different_answers(s: Slice) -> None:
    carol = s.ask("carol@acme.test")
    bob = s.ask("bob@acme.test")
    assert "github/acme/api/README.md" in carol
    assert all("/api/" not in d for d in bob)
    assert carol != bob


def test_revoke_changes_the_answer_after_reconcile(s: Slice) -> None:
    assert any("/api/" in d for d in s.ask("carol@acme.test"))
    s.gh.teams["backend"] = ("engineering", [])  # carol leaves the backend team
    rep = s.reconcile()
    assert rep.errors == []
    assert rep.tuples_deleted >= 1
    assert all("/api/" not in d for d in s.ask("carol@acme.test"))


def test_source_delete_purges_tuples_and_chunks(s: Slice) -> None:
    before = s.index.count()
    del s.gh.repos["api"].files["README.md"]
    rep = s.reconcile()
    assert rep.swept == 1
    assert rep.chunks_deleted >= 1
    assert s.index.count() < before
    assert s.authz.read(doc("github/acme/api/README.md")) == frozenset()


def test_repo_deletion_sweeps_container_and_files(s: Slice) -> None:
    del s.gh.repos["payroll"]
    rep = s.reconcile()
    assert rep.swept == 2  # the repo container and its one file
    assert "github/acme/payroll/salaries.md" not in s.ask("alice@acme.test", "salary bands bonus")


def test_unchanged_content_is_not_reembedded(s: Slice) -> None:
    rep = s.reconcile()
    assert rep.chunks_written == 0
    assert rep.unchanged_content == 5


def test_partial_snapshot_does_not_sweep(s: Slice) -> None:
    count = s.index.count()
    s.gh.org = "renamed"  # every org call now 404s mid-reconcile
    rep = s.reconcile()
    assert rep.sweep_skipped
    assert rep.errors
    assert s.index.count() == count


def test_unbound_retriever_refuses() -> None:
    sl = Slice(FakeGitHub())
    with pytest.raises(ValueError, match="invalid query subject"):
        sl.retriever.invoke(QUESTION)


class _Truth:
    def __init__(self, oracle: Oracle, s: Slice, probes: Sequence[Probe], k: int) -> None:
        self.o, self.s, self.k = oracle, s, k
        self._expected = []
        for p in probes:
            allowed = {
                h.chunk_id
                for h in s.index.search(p.vector, 10_000)
                if self.allowed(p.user, h.doc_id)
            }
            ranked = [h.chunk_id for h in s.index.search(p.vector, 10_000) if h.chunk_id in allowed]
            self._expected.append(ranked[:k])

    def allowed(self, user: str, doc_id: str) -> bool:
        return self.o.can_view(user, doc(doc_id))

    def expected(self, i: int) -> list[str]:
        return self._expected[i]

    def authorized_chunks(self, user: str) -> int:
        return sum(
            1
            for h in self.s.index.search([1.0] + [0.0] * 127, 10_000)
            if self.allowed(user, h.doc_id)
        )

    def selectivity(self, user: str) -> float:
        return len(self.o.visible(user)) / 5


@pytest.mark.parametrize(
    "strategy", [Strategy.PRE_FILTER, Strategy.POST_FILTER, Strategy.ITERATIVE]
)
def test_slice_is_leak_free_against_the_oracle(s: Slice, strategy: Strategy) -> None:
    oracle = Oracle(s.authz.tuples)
    users = [*IDENTITIES["github"].values(), "eve@elsewhere.test"]
    questions = [QUESTION, "salary bands bonus", "welcome laptops", "acme tooling", "vault rotate"]
    probes = [Probe(u, EMBED.embed_one(q)) for u in users for q in questions]
    truth = _Truth(oracle, s, probes, k=3)

    def retrieve(p: Probe, k: int) -> list[Hit]:
        return s.engine.retrieve(p.user, p.vector, k, strategy=strategy).hits

    report = leakcheck.run(strategy.value, retrieve, probes, truth, k=3)
    assert report.leak_free, report.overall
