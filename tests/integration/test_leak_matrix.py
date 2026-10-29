"""Every engine x index x strategy combination must be leak-free on a generated organisation.

This is the CI form of the benchmark's headline claim: strategies may differ in recall and
latency, never in what they can leak (ADR-0003 §1). Needs `make up-full`.
"""

from __future__ import annotations

from typing import Any

import pytest

from rag_authz import leakcheck
from rag_authz.authz.base import AuthzBackend
from rag_authz.index.base import ChunkRecord, VectorIndex
from rag_authz.retrieval import AuthorizedRetriever, Strategy
from rag_authz.synth import OrgSpec, SyntheticTruth, generate

pytestmark = pytest.mark.integration

ORG = generate(OrgSpec(seed=21, users=200, groups=40, roots=20, docs=1500, queries=60))


def _engine(request: pytest.FixtureRequest, name: str) -> AuthzBackend:
    b: AuthzBackend = request.getfixturevalue(name)
    return b


def _index(request: pytest.FixtureRequest, name: str) -> VectorIndex:
    factory: Any = request.getfixturevalue({"pgvector": "pgindex", "qdrant": "qdindex"}[name])
    ix: VectorIndex = factory(ORG.spec.dim)
    return ix


@pytest.mark.parametrize(
    "strategy",
    [
        Strategy.PRE_FILTER,
        Strategy.POST_FILTER,
        Strategy.ITERATIVE,
        Strategy.CACHED_PRE_FILTER,
    ],
)
@pytest.mark.parametrize("index_name", ["pgvector", "qdrant"])
@pytest.mark.parametrize("engine_name", ["openfga", "spicedb"])
def test_leak_free(
    request: pytest.FixtureRequest, engine_name: str, index_name: str, strategy: Strategy
) -> None:
    engine = _engine(request, engine_name)
    index = _index(request, index_name)
    engine.write(ORG.tuples, [])
    index.upsert(
        [
            ChunkRecord(c, d, t, v.tolist())
            for c, d, t, v in zip(
                ORG.chunk_ids, ORG.chunk_doc, ORG.chunk_text, ORG.vectors, strict=True
            )
        ]
    )
    truth = SyntheticTruth(ORG, k=10)
    retriever = AuthorizedRetriever(engine, index)
    report = leakcheck.run(
        strategy.value,
        lambda p, k: retriever.retrieve(p.user, p.vector, k, strategy=strategy).hits,
        truth.probes(),
        truth,
        k=10,
    )
    assert report.leak_free, report.overall
    if strategy is not Strategy.POST_FILTER:
        assert report.overall.recall_mean is not None
        assert report.overall.recall_mean > 0.8, report.overall
