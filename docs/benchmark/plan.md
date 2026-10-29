# Benchmark plan: filtering strategies under group-membership skew

Status (2026-10-28):

- **Built:**
  - the harness (`src/tuplegate/leakcheck.py`);
  - the generator (`src/tuplegate/synth.py`, now with stratified probe sampling);
  - the strategy runner (`benchmarks/filter_strategies.py`);
  - the revocation-lag and revoke-cost runner (`benchmarks/revocation_lag.py`);
  - the sidecar-overhead runner (`benchmarks/sidecar_overhead.py`);
  - the drivers (`benchmarks/run_matrix.sh`, `benchmarks/run_followups.sh`).
- **Run:** engine-backed results for OpenFGA and SpiceDB with pgvector and Qdrant. Every number is in [results.md](results.md), which is generated from the committed JSON.
- **Deviations from the protocol below:**
  - **Three seeds** were run on two engine × index combinations, not all four. A seed-7 run of every combination takes about 17 minutes.
  - **The `default` preset produces almost no broad-reach users,** so a `skewed` preset with stratified probes measures them (Q1).
  - **Q4 arm 3 (principal pushdown) is not built.** Arm 2 (cached allowed sets, `cached_pre_filter`) is.
  - **Larger organisations (Q2 scale) are not run.** The ground-truth matrix needs docs × users booleans, about 1 GB at 100k × 10k.

## The claim to test, worded narrowly

Filtering strategies have been compared before for ABAC on pgvector (arXiv 2606.19803, SeQureDB '26), and naive post-filtering is known to lose recall at low selectivity. Nobody has published a comparison that has all three of these (docs/research/2026-09-27-engines-and-vector-stores.md):

1. a **ReBAC engine in the loop** (OpenFGA/SpiceDB), so the cost of *asking* is measured, not only the cost of filtering;
2. **skewed group membership**, so one organisation contains both 1%-reach users and 50%-reach users;
3. **revocation lag and leak window**, measured end to end.

That combination is the "first" we may claim, and only once the numbers exist.

## Questions

1. **Recall and latency by user selectivity.** For each strategy, how do recall@k, short-result rate and p50/p95 latency vary across selectivity buckets (<1%, 1–5%, 5–20%, 20–50%, ≥50%)?
2. **Engine cost.** How do streamed ListObjects/LookupResources (pre-filter) and batch checks (post-filter, iterative) scale with organisation size, hierarchy depth, restriction rate and user reach? When does a pre-filter list hit the engine deadline (`list_complete = false`)?
3. **Consistency.** What latency does `MINIMIZE_LATENCY` save, and what leak window does it open after a revoke? For SpiceDB, the expected window is the 5s quantization window unless ZedTokens are used.
4. **Group expansion at scale** (the brief's open question, ADR-0003 §6). Compare three arms: an engine list per query, cached per-user allowed sets with a TTL, and principal pushdown into the vector payload. Measure recall, latency, and the revocation lag each one adds.
5. **Deployment overhead.** Sidecar over HTTP versus library mode.

## Workload: the synthetic organisation

No public dataset of enterprise ACL distributions exists; the landscape research found none. The generator therefore makes every assumption explicit and adjustable (`OrgSpec`), and the benchmark **sweeps** the ones that drive the result rather than presenting one setting as "realistic".

| Knob | Default | Sweep | Why it matters |
|---|---|---|---|
| `group_size_alpha` | 1.1 | 0.8, 1.1, 1.5 | Rank-size law of group sizes: a few department-sized groups, many small teams |
| `membership_alpha` | 0.8 | 0, 0.8, 1.2 | Power users in many groups. This is the skew that spreads per-user selectivity |
| `nested_group_fraction` | 0.25 | 0, 0.25, 0.5 | Nesting depth costs engine traversal |
| `company_wide_doc_share` | 0.02 | 0.005, 0.02, 0.1 | Sets every user's selectivity floor |
| `p_inheritance_break`, `p_restriction` | 0.03 each | 0, 0.03, 0.1 | Exclusion is the expensive path in OpenFGA ListObjects |
| `acl_topic_correlation` | 0.85 | 0.5, 0.85, 1.0 | Whether what you may see is clustered near what you ask about. Filtered ANN depends on it |
| Size (users / docs) | 2k / 10k | 2k/10k, 10k/100k, 50k/1M (stretch) | Engine and index scale |
| `p_out_of_scope_query` | 0.5 | fixed | Half the probes target topics the user cannot see, the probes most likely to surface leaks |

Vectors are topic centroids plus noise (dimension 64 by default). They are synthetic on purpose: the benchmark measures filtering, not embedding quality. Ground truth is exact: for each probe, the oracle's authorised set (bitset evaluation of the same tuples) intersected with brute-force cosine top-k.

## Arms

| Dimension | Values |
|---|---|
| Strategy | `pre_filter`, `post_filter` (overfetch 2, 4, 10), `iterative` (max_candidates 500, 1,000, 5,000), `unfiltered` (leak baseline only) |
| Engine | oracle (in-memory reference), OpenFGA 1.21.0, SpiceDB 1.56.2 |
| Index | exact (numpy), pgvector 0.8.6 with `iterative_scan` off, relaxed_order or strict_order, Qdrant 1.19.1 (default, ACORN) |
| Consistency | full, minimize_latency |
| Group expansion (Q4) | engine list, cached allowed sets (TTL 1s, 10s, 60s), principal pushdown plus final check |

## Metrics (exact definitions)

- **Result leak rate:** unauthorised results / all returned results, judged by the oracle.
- **Query leak rate:** probes returning at least one unauthorised result / probes. This is the "one leak is a failure" number (VaultRAG's framing).
- **Recall@k:** |returned ∩ exact authorised top-k| / min(k, authorised chunks), over probes with at least one authorised chunk.
- **Short-result rate:** probes returning fewer than min(k, authorised chunks) results. It is the user-visible symptom of low recall.
- **Latency:** end-to-end p50/p95/p99 per probe, plus the authz/search split and engine calls per probe from `RetrievalStats`.
- **List truncation rate:** the share of pre-filter probes with `list_complete = false`.
- **Revocation lag** (Q3/Q4) has three parts:
  - (a) *sidecar lag*: engine write committed minus change observed;
  - (b) *enforcement lag*: first compliant response minus write committed, polling every 50 ms per strategy;
  - (c) their sum.
  Source-feed lag (Google groups: tens of minutes to hours; GitHub base permission: no webhook) is documented per source and kept out of (a) to (c).

## Protocol

- Pinned everything: `uv.lock`, Compose images by digest, generator seed. Every run writes a JSON file with the git commit, package version, Python, platform, CPU count, organisation stats and all arguments.
- A warm-up of 20 probes per strategy, then all probes. Three seeds per configuration; report medians with min/max.
- One machine, with services on localhost, disclosed in every table. Latency numbers from the in-memory backend are marked as not meaningful.
- Every number in a README or post links to a committed results JSON (repo rule: honest claims).

## Anchors and prior art (cite with their caveats)

- **VaultRAG:** removing the ACL predicate moved leak rate from 0% to 81.8%, which is 9 of 11 cases on a 10-document corpus (1★ repo). Illustrative only.
- **VecAdvisor:** post-filter recall@10 of 0.2125 versus 0.9625 with iterative scans, on 4,096 synthetic rows at 5% selectivity with an anti-correlated filter (4★ repo). The brief attributed this to ClickHouse; that attribution was wrong.
- **arXiv 2606.19803** (ABAC, pgvector, 2.77M rows, no skew) and **arXiv 2605.01342** (Veda, RBAC lattice indexes).

## Threats to validity

- **Synthetic distributions.** The sweeps bound the answer; they do not prove any real organisation looks like the default. A calibration against a real tenant's anonymised ACL statistics would be the strongest improvement. It has to come from a user volunteering data.
- Single-machine runs understate network latency to managed engines. The sidecar-hop arm (Q5) partly covers this.
- Engine defaults (deadlines, caches, batch limits) shape the results. They are pinned in `deploy/compose.yaml` and reported with each run.

## First reference run (in-memory, not a published result)

`benchmarks/results/2026-09-27-smoke-memory-exact.md`: 500 users, 3,000 docs, 200 probes, k=10.

- Every filtered strategy leaked nothing. The unfiltered baseline leaked in 99% of probes.
- Post-filter (overfetch 4) recall was 0.43 in the 1–5% selectivity bucket and 0.74 in the 20–50% bucket.
- Iterative and pre-filter were at 0.99 or better everywhere.
- This validates the harness and the skew mechanism. Latency needs the engine-backed run.
