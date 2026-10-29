# ADR-0004: The default strategy and group expansion, decided by the benchmark

- Status: Accepted
- Date: 2026-10-28
- Decides: ADR-0003 §6 (default strategy; group-expansion strategy at scale)

## Context

ADR-0003 fixed the safety invariant and left two choices to the benchmark: which strategy is the default, and how group expansion should work at scale (engine list per query, cached per-user allowed sets, or principal pushdown). The numbers are in [docs/benchmark/results.md](../benchmark/results.md), generated from committed JSON. Every filtered strategy had **zero leaking queries** in every configuration (4 engine × index combinations, 3 seeds, 2 workloads, 3 consistency modes). So the choice is about recall, latency and engine load only, as ADR-0003 intended.

What the runs showed (OpenFGA 1.21.0 and SpiceDB 1.56.2, pgvector 0.8.6 and Qdrant 1.19.1, one Intel Mac, k = 10):

1. **Post-filter loses most of the answer for typical users.**
   - Recall@10 is 0.34–0.42 on the default workload, where most users see 1–5% of the corpus, and 0.42 in the 5–20% band of the skewed workload.
   - Overfetch 10 only raises it to 0.47 while doubling latency.
   - It reaches 0.92 only for users who see half the corpus or more.
2. **Iterative recovers recall but is engine-bound.**
   - Recall is 0.92–0.97.
   - On OpenFGA, p95 is 1.6–3.9 s, because batch-check is capped at 50 items per request and narrow-reach users need many rounds. On SpiceDB, with 1,000-item bulk checks, it is 0.2–0.3 s.
3. **Pre-filter is exact, and its cost is the engine's list, which grows with reach.**
   - Recall is 1.000.
   - OpenFGA p95 is 54–90 ms on the default workload and 207 / 454 / 1,193 ms across the skewed 5–20% / 20–50% / ≥50% bands.
   - SpiceDB lists more slowly: 144–179 ms on the default workload, and 347 / 1,001 / 2,894 ms across the skewed bands.
   - With pgvector's iterative scans off, pre-filter recall collapses to 0.34, the same as post-filter, because HNSW hands only `ef_search` rows to the filter. `iterative_scan` is not optional.
4. **A cached allowed list removes that cost.**
   - Reusing a user's list for a TTL, then checking the hits it returns with the engine, gives recall 1.000 at p95 24–72 ms on the default workload and 117–152 ms on the skewed one, including 136 / 177 ms for users who see half the corpus or more.
   - Pushing thousands of ids into the index filter turned out to be cheap. The engine list was the expensive part.
5. **Consistency matters only on SpiceDB, and ZedTokens fix it.**
   - With `minimize_latency`, a revoked document kept coming back in 11–15 of 20 trials, for up to 4.9 s (the 5 s quantization window).
   - `at_least_as_fresh` (the ZedToken of our own revoking write) and `full` had zero leaking responses.
   - OpenFGA, with caches off by default, leaked in no mode.

## Decision

### 1. The default stays `pre_filter`

It is the only strategy that is exact with no state, its latency is acceptable for the reach most users have, and it is the simplest to audit. A library default should be correct before it is fast.

### 2. `cached_pre_filter` is the recommended production setting

- Every returned hit is re-checked by the engine during the request. A stale list therefore costs recall (a new grant appears after the TTL), never safety (a revoke bites on the next query). The revocation benchmark measured zero leaked responses for the cached arm under `full` and `at_least_as_fresh`.
- The TTL (`list_cache_ttl_s`, default 10 s) bounds how long a new grant stays invisible. Operators trade that against the share of queries that pay a cold list.
- The benchmark measures the warm path. A cold query costs one `pre_filter`.

### 3. Group expansion: the engine expands; a cached list is the only shortcut

- **Arm 1, engine list per query** (`pre_filter`): the default.
- **Arm 2, cached per-user allowed sets** (`cached_pre_filter`): adopted, as above.
- **Arm 3, principal pushdown** (flattening effective principals into the vector payload): **not adopted for v0.1.**
  - Its cost is the one ADR-0002 exists to avoid. Revoking one folder grant over 10,000 documents took 1 tuple and 4–7 ms structured, against 10,000 tuples and 3.5–4.8 s flattened (measured on both engines).
  - Arm 2 already takes the engine off the hot path.
  - It stays parked for subtrees deeper than the engines resolve (docs/research/2026-10-28-resolution-depth.md).

### 4. Consistency defaults

- **`full`** stays the default.
- **On SpiceDB, recommend `at_least_as_fresh`.** It is as leak-free as `full` in the revocation benchmark, provided the sync worker and the retriever share a backend instance, so the retriever sees the latest write token.
- **Never `minimize_latency` on SpiceDB** for content that can be revoked, unless a 5-second leak window is acceptable.

## Consequences

- **README and sidecar** document `cached_pre_filter` as the recommended setting, `pre_filter` as the default, and the SpiceDB consistency rule.
- **Engine choice is a real trade-off,** now with numbers:
  - OpenFGA lists faster, and its 50-item batch-check cap makes check-heavy strategies slow;
  - SpiceDB checks faster, but its lists slow down with reach and it needs ZedTokens for prompt revokes.
  - With the cached strategy the difference mostly disappears.
- **Iterative's OpenFGA cost** could be cut by raising `OPENFGA_MAX_CHECKS_PER_BATCH_CHECK`. That is an operator setting, and the benchmark keeps the documented default.
- **Untested at scale.** v0.1 did not run organisations above 10,000 documents (plan.md, deviations). List size and cache memory grow with reach × users.

## Alternatives considered

- **An adaptive strategy (`auto`): built, measured, rejected.**
  - It chose per subject: cached pre-filter while the allowed list was short (≤ 2,000 docs), iterative otherwise. It rested on the expectation that long index filters dominate for broad-reach users.
  - Finding 4 refuted that. To know a user's reach it must hold the list anyway, so for broad users it paid for the list and for iterative.
  - Skewed workload: p95 792 ms on OpenFGA against 117 ms for `cached_pre_filter`; 95 ms against 152 ms on SpiceDB, but at recall 0.993 instead of 1.000. On the default workload it matched the cached arm.
  - Its rows are in `benchmarks/results/2026-10-28-*-s7-cache.json` and `2026-10-28-skewed-*-s7.json`. The strategy was removed from the code.
- **`iterative` as the default.** Good recall, but multi-second p95 on OpenFGA with default settings, and recall below 1.0 by construction (the candidate budget).
- **`post_filter` as the default.** It is what most "permission-aware RAG" examples do. The benchmark shows it returns about a third of the answer for typical users.
