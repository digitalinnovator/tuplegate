# ADR-0003: Filtering strategies and the safety invariant

- Status: Accepted. The default strategy and the group-expansion strategy were decided by the benchmark in [ADR-0004](0004-default-strategy.md) (2026-10-28), which also added `cached_pre_filter` (and rejected an adaptive strategy).
- Date: 2026-09-27

## Context

A permission-aware retriever has to combine two indexes that know nothing about each other: an authorization engine that answers "may user U view doc D?", and an ANN index that answers "what is near vector q?". The ways to combine them trade recall, latency and engine load differently:

- **Pre-filter.** Ask the engine for every doc U can view, then run a filtered ANN search.
  - Recall is independent of selectivity, but the list grows with U's reach.
  - OpenFGA's ListObjects stops silently at its deadline (3s by default), and its non-streamed form caps at 1,000 results.
  - SpiceDB warns of performance problems above 10k results.
  - pgvector's HNSW without iterative scans returns at most `ef_search` (40) candidates before the filter.
- **Post-filter.** Run ANN top-(k × overfetch), then batch-check the candidates.
  - The engine load is constant, but recall collapses when U can see a small fraction of the corpus.
  - arXiv 2606.19803 reports "almost zero recall" for naive post-filtering at low selectivity (ABAC on pgvector).
  - The VecAdvisor toy benchmark measured 0.2125 recall@10 at 5% selectivity.
- **Iterative.** Post-filter in growing windows until k authorised hits or a candidate budget.
  - SpiceDB's own guidance for large result sets (CheckBulkPermissions with paging).

**Group-membership skew** makes the choice per user rather than per deployment. A typical engineer sees a few percent of the corpus while an admin sees half. So one organisation contains both the users post-filtering fails and the users pre-filtering is expensive for.

A second axis is **consistency**:

- SpiceDB's default `minimize_latency` serves reads from a revision quantized to 5s, so a revoke can keep working for about 5 seconds unless the caller passes the revoking write's ZedToken.
- OpenFGA's caches are off by default, which makes it strongly consistent unless an operator turns them on.

## Decision

### 1. One safety invariant for every strategy

> A hit is returned only if the authorization engine said, during this request, that the subject can view the hit's document.

- Pre-filter gets that from the engine's list and re-checks every hit against it, so an index adapter that ignores the filter cannot leak.
- Post-filter and iterative get it from a batch check.
- Anything else **denies**: an engine error, a per-item check error, an error line mid-stream, or an unknown doc id.
- The request fails and the audit record carries the error. Nothing ever falls back to unfiltered results.
- Strategies may differ in recall, latency and engine load. **They may not differ in what they can leak**, and a unit test runs every strategy through the leak harness.

### 2. The strategies (`tuplegate.retrieval.Strategy`)

| Strategy | Mechanism | Knobs |
|---|---|---|
| `pre_filter` | Streamed ListObjects / LookupResources → push `acl_id IN (...)` into the index | `list_complete` is reported. A truncated list costs recall, never correctness |
| `post_filter` | ANN top-(k × `overfetch`) → one batch check of the distinct docs | `overfetch` (default 4) |
| `iterative` | Windows of k × `overfetch`, doubling, skipping already-checked docs, until k hits or `max_candidates` | `overfetch`, `max_candidates` (default 1,000) |
| `unfiltered` | ANN only. **The benchmark's leak baseline.** Refused unless `allow_unfiltered=True` | none |

### 3. Consistency defaults to full

- `Consistency.FULL` maps to OpenFGA `HIGHER_CONSISTENCY` and SpiceDB `fully_consistent`. `MINIMIZE_LATENCY` is opt-in.
- The benchmark reports both the latency it saves and the leak window it opens after a revoke.
- The SpiceDB ZedToken pattern (`at_least_as_fresh` with the latest revoking write's token) is the planned middle ground.

### 4. Index pushdown details

- **pgvector.**
  - `acl_id = ANY($1)` on a B-tree-indexed column, with `hnsw.iterative_scan = relaxed_order` by default.
  - The client re-sorts the returned rows, because relaxed order may be slightly out of order.
  - `off` and `strict_order` are benchmark arms.
- **Qdrant (planned).** A keyword payload index on `acl_id` and a `MatchAny` filter; ACORN (`search_params.acorn`) is a benchmark arm.

### 5. Every decision is audited

One `AuditRecord` per retrieval: request id, subject, strategy, consistency, returned doc ids, **denied** ACL ids (the signal for probing), candidate count, engine calls, allowed-list size, `list_complete`, latency and error. Records carry ids, never content.

### 6. What the benchmark must decide (follow-up ADR)

- **Default strategy.** Fixed pre-filter, or adaptive: pick by the size of the user's allowed set, which the engine can bound cheaply for most users.
- **Group-expansion strategy at scale** (the brief's open question). Three arms:
  1. **Engine list per query.** The engine does all expansion.
  2. **Materialised per-user allowed sets** with a TTL. Faster, but staleness is a leak window, and that window is measured.
  3. **Principal pushdown.** Store each doc's effective principals in the vector payload, as Elastic DLS and Azure do, and expand only the *user's* groups at query time. Fastest filter, but it needs flattening, which ADR-0002 rejects as the canonical form. It would be a derived cache with measured revocation lag.

The safety invariant constrains all three. Arms 2 and 3 must pair with a final batch check of the top k (cheap: k checks) to keep the invariant, so their speed-up applies to recall and latency only.

## Consequences

- Safety is a property of the retriever, not of the tuning. Anyone can pick the fastest strategy for their skew without re-auditing leak behaviour.
- The default is pre-filter until the benchmark says otherwise. It is the only strategy whose recall does not depend on selectivity. Its cost for broad-access users is exactly what the benchmark measures.
- `FULL` consistency costs latency on every call, deliberately. Users who opt out of it get a measured number, not a vague warning.
- Pre-filter with a truncated list degrades silently in recall unless someone reads `list_complete`, so the audit record and the stats both carry it.

## Alternatives considered

- **Partition the index per tenant or group** (Qdrant `is_tenant`, pgvector partitions). It works for a few disjoint tenants. It fails for thousands of overlapping groups, where every vector would be duplicated into every group that can see it.
- **Access-aware indexes** (Veda/EffVeda, arXiv 2605.01342). Research-grade and RBAC-only. Worth benchmarking later if it ships an implementation.
- **AuthZed Materialize.** It precomputes permission sets and exports them to an index. It is AuthZed Dedicated only, so it is not an option for a permissively licensed default. Our arm 2 or 3 is the open equivalent to benchmark.
- **One strategy only.** It would be simpler to explain. It would also be wrong for half of every skewed organisation, which is the benchmark's headline.
