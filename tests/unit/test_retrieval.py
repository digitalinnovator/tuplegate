from collections.abc import Collection, Sequence

import pytest

from rag_authz import leakcheck
from rag_authz.audit import MemoryAuditSink
from rag_authz.authz.base import AuthzError, Consistency, ListResult
from rag_authz.authz.memory import MemoryBackend
from rag_authz.index.base import ChunkRecord, Hit
from rag_authz.index.exact import ExactIndex
from rag_authz.index.memory import MemoryIndex
from rag_authz.model import NodeAcl, Tuple, doc, user
from rag_authz.retrieval import AuthorizedRetriever, RetrievalConfig, Strategy
from rag_authz.synth import OrgSpec, SyntheticTruth, generate

SAFE = [
    Strategy.PRE_FILTER,
    Strategy.POST_FILTER,
    Strategy.ITERATIVE,
    Strategy.CACHED_PRE_FILTER,
]


@pytest.fixture(scope="module")
def small_org():  # type: ignore[no-untyped-def]
    return generate(OrgSpec(users=120, groups=25, roots=12, docs=600, queries=80, seed=3))


def _loaded(org) -> tuple[MemoryBackend, ExactIndex]:  # type: ignore[no-untyped-def]
    backend = MemoryBackend(org.tuples)
    index = ExactIndex(org.spec.dim)
    index.upsert(
        [
            ChunkRecord(c, d, t, v.tolist())
            for c, d, t, v in zip(
                org.chunk_ids, org.chunk_doc, org.chunk_text, org.vectors, strict=True
            )
        ]
    )
    return backend, index


@pytest.mark.parametrize("strategy", SAFE)
def test_every_safe_strategy_is_leak_free(small_org, strategy: Strategy) -> None:  # type: ignore[no-untyped-def]
    backend, index = _loaded(small_org)
    r = AuthorizedRetriever(backend, index)
    truth = SyntheticTruth(small_org, k=8)
    report = leakcheck.run(
        strategy.value,
        lambda p, k: r.retrieve(p.user, p.vector, k, strategy=strategy).hits,
        truth.probes(),
        truth,
        k=8,
    )
    assert report.leak_free, report.overall
    if strategy is not Strategy.POST_FILTER:
        # Exact index: pre-filter and iterative (with a budget covering the corpus) are exact.
        assert report.overall.recall_mean == pytest.approx(1.0)


def test_unfiltered_baseline_leaks_and_is_off_by_default(small_org) -> None:  # type: ignore[no-untyped-def]
    backend, index = _loaded(small_org)
    with pytest.raises(PermissionError):
        AuthorizedRetriever(backend, index).retrieve(
            "u00000@example.test", [1.0] * 64, 5, strategy=Strategy.UNFILTERED
        )
    r = AuthorizedRetriever(backend, index, RetrievalConfig(allow_unfiltered=True))
    truth = SyntheticTruth(small_org, k=8)
    report = leakcheck.run(
        "unfiltered",
        lambda p, k: r.retrieve(p.user, p.vector, k, strategy=Strategy.UNFILTERED).hits,
        truth.probes(),
        truth,
        k=8,
    )
    assert report.overall.query_leak_rate > 0.5


class FailingBackend(MemoryBackend):
    def check_docs(
        self, user_id: str, acl_ids: Sequence[str], consistency: Consistency
    ) -> dict[str, bool]:
        raise AuthzError("engine down")

    def list_docs(self, user_id: str, consistency: Consistency) -> ListResult:
        raise AuthzError("engine down")


@pytest.mark.parametrize("strategy", SAFE)
def test_engine_errors_fail_closed_and_are_audited(strategy: Strategy) -> None:
    index = MemoryIndex()
    index.upsert([ChunkRecord("c1", "d1", "secret", [1.0, 0.0])])
    audit = MemoryAuditSink()
    r = AuthorizedRetriever(FailingBackend(), index, audit=audit)
    with pytest.raises(AuthzError):
        r.retrieve("alice", [1.0, 0.0], 3, strategy=strategy)
    assert audit.records[-1].error is not None
    assert audit.records[-1].returned_doc_ids == []


class LyingIndex(MemoryIndex):
    """An adapter bug: ignores the ACL filter."""

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        acl_filter: Collection[str] | None = None,
        offset: int = 0,
    ) -> list[Hit]:
        return super().search(vector, k, offset=offset)


def test_pre_filter_drops_hits_outside_the_list_even_if_the_index_ignores_it() -> None:
    backend = MemoryBackend(NodeAcl(doc("ok"), viewers=frozenset({user("alice")})).tuples())
    index = LyingIndex()
    index.upsert(
        [ChunkRecord("c1", "secret", "s", [1.0, 0.0]), ChunkRecord("c2", "ok", "o", [0.9, 0.1])]
    )
    hits = AuthorizedRetriever(backend, index).retrieve("alice", [1.0, 0.0], 2).hits
    assert [h.doc_id for h in hits] == ["ok"]


def test_iterative_respects_candidate_budget() -> None:
    tuples: set[Tuple] = set(NodeAcl(doc("needle"), viewers=frozenset({user("alice")})).tuples())
    index = MemoryIndex()
    index.upsert([ChunkRecord(f"c{i}", f"hay{i}", "", [1.0, i / 1000]) for i in range(100)])
    index.upsert([ChunkRecord("n", "needle", "", [0.0, 1.0])])
    r = AuthorizedRetriever(
        MemoryBackend(tuples), index, RetrievalConfig(max_candidates=20, overfetch=2)
    )
    res = r.retrieve("alice", [1.0, 0.0], 1, strategy=Strategy.ITERATIVE)
    assert res.hits == []
    assert res.stats.candidates == 20
    r2 = AuthorizedRetriever(
        MemoryBackend(tuples), index, RetrievalConfig(max_candidates=200, overfetch=2)
    )
    assert [
        h.doc_id for h in r2.retrieve("alice", [1.0, 0.0], 1, strategy=Strategy.ITERATIVE).hits
    ] == ["needle"]


def test_audit_record_per_decision() -> None:
    backend = MemoryBackend(NodeAcl(doc("ok"), viewers=frozenset({user("alice")})).tuples())
    index = MemoryIndex()
    index.upsert([ChunkRecord("c1", "ok", "o", [1.0]), ChunkRecord("c2", "no", "n", [1.0])])
    audit = MemoryAuditSink()
    r = AuthorizedRetriever(backend, index, audit=audit)
    r.retrieve("alice", [1.0], 2, strategy=Strategy.POST_FILTER, request_id="r1")
    rec = audit.records[-1]
    assert rec.request_id == "r1"
    assert rec.returned_doc_ids == ["ok"]
    assert rec.denied_acl_ids == ["no"]
    assert rec.strategy == "post_filter"


@pytest.mark.parametrize("bad", ["", "*", "~github/octocat"])
def test_invalid_subjects_rejected(bad: str) -> None:
    r = AuthorizedRetriever(MemoryBackend(), MemoryIndex())
    with pytest.raises(ValueError, match="invalid query subject"):
        r.retrieve(bad, [1.0], 1)


def test_cached_pre_filter_never_leaks_a_revoke_and_learns_grants_after_ttl() -> None:
    from rag_authz.authz.memory import MemoryBackend
    from rag_authz.index.base import ChunkRecord
    from rag_authz.index.memory import MemoryIndex
    from rag_authz.model import NodeAcl, doc, user
    from rag_authz.retrieval import AuthorizedRetriever, RetrievalConfig

    b = MemoryBackend()
    a_acl = NodeAcl(doc("a"), viewers=frozenset({user("u")}))
    b.write(a_acl.tuples(), [])
    ix = MemoryIndex()
    ix.upsert([ChunkRecord("a#0", "a", "", [1.0, 0.0]), ChunkRecord("b#0", "b", "", [0.9, 0.1])])
    r = AuthorizedRetriever(b, ix, RetrievalConfig(strategy=Strategy.CACHED_PRE_FILTER))

    first = r.retrieve("u", [1.0, 0.0], 2)
    assert [h.doc_id for h in first.hits] == ["a"]
    assert first.stats.list_cache_hit is False
    b.write([], a_acl.tuples())  # revoke; the cached list still names "a"
    after = r.retrieve("u", [1.0, 0.0], 2)
    assert after.stats.list_cache_hit is True
    assert after.hits == []  # the final check denied it
    b.write(NodeAcl(doc("b"), viewers=frozenset({user("u")})).tuples(), [])  # grant
    assert r.retrieve("u", [1.0, 0.0], 2).hits == []  # not in the cached list yet
    r.invalidate("u")
    assert [h.doc_id for h in r.retrieve("u", [1.0, 0.0], 2).hits] == ["b"]
