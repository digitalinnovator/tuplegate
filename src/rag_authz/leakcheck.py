"""Leak-rate harness: run probes through a retriever and score them against ground truth.

Metrics (docs/benchmark/plan.md defines them precisely):

- **result leak rate**: unauthorised results / all returned results.
- **query leak rate**: probes with at least one unauthorised result / probes. This is the
  VaultRAG-style "one leak is a failure" number.
- **recall@k**: |returned ∩ exact authorised top-k| / min(k, authorised chunks), averaged over
  probes whose user can see at least one chunk.
- **short rate**: probes that returned fewer than min(k, authorised chunks) results.
- latency percentiles, all of the above split by user selectivity bucket and probe intent.

The harness is engine-agnostic: ``retrieve`` is any callable, so the same probes score the
in-process SDK, the sidecar over HTTP, or someone else's retriever.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from rag_authz.index.base import Hit

SELECTIVITY_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<1%", 0.0, 0.01),
    ("1-5%", 0.01, 0.05),
    ("5-20%", 0.05, 0.20),
    ("20-50%", 0.20, 0.50),
    (">=50%", 0.50, 1.01),
)


@dataclass(frozen=True)
class Probe:
    user: str
    vector: Sequence[float]
    intent: str = "in_scope"


class GroundTruth(Protocol):
    def allowed(self, user: str, doc_id: str) -> bool: ...

    def expected(self, probe_index: int) -> Sequence[str]:
        """Chunk ids of the exact authorised top-k for this probe."""
        ...

    def authorized_chunks(self, user: str) -> int: ...

    def selectivity(self, user: str) -> float: ...


@dataclass
class ProbeOutcome:
    index: int
    user: str
    intent: str
    bucket: str
    returned: int
    leaked_doc_ids: list[str]
    recall: float | None
    short: bool
    latency_ms: float


@dataclass
class Summary:
    probes: int
    results: int
    leaked_results: int
    result_leak_rate: float
    query_leak_rate: float
    recall_mean: float | None
    short_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float


@dataclass
class LeakReport:
    label: str
    k: int
    overall: Summary
    by_bucket: dict[str, Summary]
    by_intent: dict[str, Summary]
    outcomes: list[ProbeOutcome] = field(repr=False)
    errors: int = 0

    def to_dict(self, include_outcomes: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not include_outcomes:
            d.pop("outcomes")
        return d

    @property
    def leak_free(self) -> bool:
        return self.overall.leaked_results == 0 and self.errors == 0


def _bucket(sel: float) -> str:
    for name, lo, hi in SELECTIVITY_BUCKETS:
        if lo <= sel < hi:
            return name
    return SELECTIVITY_BUCKETS[-1][0]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[int(p) - 1]


def summarise(outcomes: Sequence[ProbeOutcome]) -> Summary:
    results = sum(o.returned for o in outcomes)
    leaked = sum(len(o.leaked_doc_ids) for o in outcomes)
    recalls = [o.recall for o in outcomes if o.recall is not None]
    lat = sorted(o.latency_ms for o in outcomes)
    n = len(outcomes)
    return Summary(
        probes=n,
        results=results,
        leaked_results=leaked,
        result_leak_rate=leaked / results if results else 0.0,
        query_leak_rate=sum(1 for o in outcomes if o.leaked_doc_ids) / n if n else 0.0,
        recall_mean=statistics.fmean(recalls) if recalls else None,
        short_rate=sum(1 for o in outcomes if o.short) / n if n else 0.0,
        latency_p50_ms=_pct(lat, 50),
        latency_p95_ms=_pct(lat, 95),
        latency_p99_ms=_pct(lat, 99),
    )


RetrieveFn = Callable[[Probe, int], Sequence[Hit]]


def run(
    label: str,
    retrieve: RetrieveFn,
    probes: Sequence[Probe],
    truth: GroundTruth,
    k: int,
) -> LeakReport:
    outcomes: list[ProbeOutcome] = []
    errors = 0
    for i, p in enumerate(probes):
        t0 = time.perf_counter()
        try:
            hits = list(retrieve(p, k))
        except Exception:
            # An error is not a leak, but a harness that swallowed errors silently would
            # report a perfect score for a retriever that never answers.
            errors += 1
            hits = []
        latency = (time.perf_counter() - t0) * 1000.0
        leaked = [h.doc_id for h in hits if not truth.allowed(p.user, h.doc_id)]
        n_auth = truth.authorized_chunks(p.user)
        want = min(k, n_auth)
        recall: float | None = None
        if want > 0:
            expected = set(truth.expected(i))
            got = {h.chunk_id for h in hits}
            recall = len(expected & got) / want
        outcomes.append(
            ProbeOutcome(
                index=i,
                user=p.user,
                intent=p.intent,
                bucket=_bucket(truth.selectivity(p.user)),
                returned=len(hits),
                leaked_doc_ids=leaked,
                recall=recall,
                short=len(hits) < want,
                latency_ms=latency,
            )
        )
    by_bucket: dict[str, list[ProbeOutcome]] = defaultdict(list)
    by_intent: dict[str, list[ProbeOutcome]] = defaultdict(list)
    for o in outcomes:
        by_bucket[o.bucket].append(o)
        by_intent[o.intent].append(o)
    order = [b[0] for b in SELECTIVITY_BUCKETS]
    return LeakReport(
        label=label,
        k=k,
        overall=summarise(outcomes),
        by_bucket={b: summarise(by_bucket[b]) for b in order if b in by_bucket},
        by_intent={name: summarise(v) for name, v in sorted(by_intent.items())},
        outcomes=outcomes,
        errors=errors,
    )
