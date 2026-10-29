# Engines and vector stores: verified facts (2026-09-27)

Checked against primary sources on 2026-09-27 to back ADR-0001 to ADR-0003. Versions and limits go stale; re-check before quoting any of them in a README or post.

## Corrections to the project brief

- **The 0.2125 recall@k figure is not from ClickHouse.** The current ClickHouse article on scaling pgvector (updated 2026-06-18) has no recall numbers. The figure comes from **VecAdvisor** ([github.com/vecadvisor/vecadvisor](https://github.com/vecadvisor/vecadvisor/blob/main/docs/benchmarks/real-pgvector-benchmark.md), 4★, Apache-2.0). It is a toy benchmark: 4,096 rows, 32-dimensional synthetic vectors, selectivity 0.05 with an anti-correlated filter, k=10, `ef_search=40`, on `pgvector/pgvector:pg17`. Results: post-filter recall 0.2125 (returned k results 0% of the time, p95 177.8 ms); iterative scan 0.9625 (40.6 ms); partial index, partition and exact search 1.0.
- **VaultRAG's 81.8% is 9 of 11 cases** on a 10-document, 5-user corpus in a 1★ repo. Leak rate there means "anything the user may not see surfaced; one leak is a failure", counted per case. It is a weak anchor; cite it as an illustration, not as evidence.
- **Filtering strategies have been compared before**, for ABAC rather than ReBAC. arXiv [2606.19803](https://arxiv.org/abs/2606.19803) (SeQureDB '26, a vision paper) compares pre-filter, naive post-filter, iterative post-filter and parallel post-filter on pgvector, using the arXiv dataset (2.77M rows) and synthetic ABAC policies across selectivity levels. It reports "almost zero recall" for naive post-filtering. It does **not** model group-membership skew, a ReBAC engine in the loop, or revocation lag. arXiv [2605.01342](https://arxiv.org/abs/2605.01342) (Veda/EffVeda) partitions vectors by role combination; it is RBAC only and compares against global and oracle indexes. No head-to-head under skewed group membership was found. Our claim must be worded as that narrower first.

## OpenFGA

- Latest release **v1.21.0 (2026-09-20)**, image `openfga/openfga`. Ports: 8080 HTTP, 8081 gRPC, 3000 playground (on by default; disable it).
- **Pin ≥ 1.21.0.** 2026 advisories include GHSA-5278-rrxc-mgf7, where StreamedListObjects returned excluded objects on error (1.15–1.18.x, fixed in 1.19.0), and GHSA-g3pg-frfm-pr2m, where ListUsers over-included users with intersection, `but not` and a typed wildcard (≤1.18.0). Our model uses exclusion and a wildcard, so differential testing against the oracle is mandatory.
- **Weighted-graph migration** ([blog, 2026-07-21](https://openfga.dev/blog/weighted-graph-upcoming-changes)): recursion *inside* `and`/`but not` will be rejected at model build time. Our model keeps the recursion in unions (`granted`, `blocked`) and applies `but not` only to non-recursive relations (`blocked_here`, `can_view`), which is the shape the blog recommends. Passing `user:*` as the Check user against an exclusion now errors; we never do.
- **HTTP API** (all POST under `/stores/{store_id}/`):
  - `write` is transactional and takes `writes` plus `deletes`. `OPENFGA_MAX_TUPLES_PER_WRITE` defaults to **100**. `on_duplicate`/`on_missing` (`error|ignore`) exist since v1.10.0.
  - `read` filters by object alone.
  - `batch-check` exists since v1.8.0. `OPENFGA_MAX_CHECKS_PER_BATCH_CHECK` defaults to **50**. `correlation_id` is at most 36 characters of `[A-Za-z0-9-]`.
  - `list-objects` and `streamed-list-objects` (NDJSON, `{"result":{"object":…}}` per line). `changes` gives a changelog feed.
- **ListObjects:**
  - `OPENFGA_LIST_OBJECTS_MAX_RESULTS` defaults to **1000** and `OPENFGA_LIST_OBJECTS_DEADLINE` to **3s**.
  - Both variants **silently truncate** at the deadline. The streamed variant has no count cap.
  - Pagination issue [#2828](https://github.com/openfga/openfga/issues/2828) is open.
  - The docs say `and`/`but not` are "more expensive". Issue #1338 is open: `but not` drove ListObjects to 3s timeouts.
- **Consistency:** `MINIMIZE_LATENCY` (default) or `HIGHER_CONSISTENCY`, since v1.5.7. Every cache is off by default, including the check query cache, the iterator caches and the cache controller. The docs say that with caching disabled "all queries will have strong consistency".
- **Identifiers:**
  - Object matches `^[^\s]{2,256}$`, with exactly one `:` and no `#`. IDs may contain `@ . /`, but not `:`, `#` or whitespace.
  - User is at most 512 bytes. Type matches `^[^:#@\s]{1,254}$`, relation `^[^:#@\s]{1,50}$`.
- No official Python DSL-to-JSON transformer exists. The `openfga/cli` image has `fga model transform` and `fga model test`. The Python SDK `openfga-sdk` 0.10.4 is aiohttp-based.
- OpenFGA also serves experimental AuthZEN endpoints (`/stores/{id}/access/v1/evaluation(s)`, `search/*`), since about 1.11.

## SpiceDB

- Latest release **v1.56.2 (2026-09-11)**, image `authzed/spicedb`.
- **Identifiers:** object ID matches `^(([a-zA-Z0-9/_|\-=+]{1,})|\*)$`, max 1024 bytes. Type names are lowercase and at least 3 characters.
- **Consistency:** `minimize_latency` (the read default), `at_least_as_fresh` (ZedToken), `at_exact_snapshot` and `fully_consistent`. Revision quantization defaults to **5s**, with 10% max staleness. The ~5.5s post-revoke window is our derivation; the docs give no number. Using the ZedToken from the revoking write with `at_least_as_fresh` closes the window.
- **LookupResources:**
  - Streams, and results may contain duplicates.
  - Supports `optional_limit` plus `optional_cursor`. `--max-lookup-resources-limit` defaults to 1000 and caps only a non-zero limit.
  - The docs warn of performance problems above 10k results and suggest CheckBulkPermissions post-filtering or Materialize instead.
- **Batch limits:** CheckBulkPermissions is hard-capped at **10,000** items (a constant, not a flag). WriteRelationships defaults to **1000** updates per call.
- Exclusion with wildcard subjects works in LookupResources (CVE-2022-21646 fixed in v1.4.0).
- **AuthZed Materialize** precomputes configured permissions like a materialized view and can export permission sets (`LookupPermissionSets`, `WatchPermissionSets`) into your own index. It is **AuthZed Dedicated only**.
- The Python client `authzed` 1.25.0 is gRPC (`grpcio`, `protobuf`). The HTTP gateway (`--http-enabled`, :8443) exists, but AuthZed does not maintain HTTP clients.

## pgvector

- Latest **v0.8.6 (2026-07-29)**, images `pgvector/pgvector:pg17` and `pg18`.
- `hnsw.iterative_scan` = `off` (default) | `relaxed_order` | `strict_order` (since 0.8.0). `hnsw.max_scan_tuples` defaults to 20000 and `hnsw.ef_search` to 40.
- The README recommends a B-tree index on the filter column for low-match predicates (exact search), iterative scans, partial indexes, or `PARTITION BY LIST`. It has no guidance on `= ANY(array)`.

## Qdrant

- Latest **v1.19.1 (2026-09-04)**. JWT payload filters were deprecated in 1.15 and **removed in 1.16** ("API keys using it are rejected"). Per-point enforcement therefore needs trusted middleware.
- `MatchAny` has no documented size limit. Keyword payload indexes support `is_tenant`.
- `full_scan_threshold` defaults to 10000 KB. Below the estimated cardinality threshold, the planner uses the payload index plus a full scan; above it, a graph traversal.
- ACORN-1 (v1.16, `search_params.acorn`) is off by default: slower, with better recall on restrictive filters.
- `qdrant-client` 1.19.1 uses `query_points(...)`; there is no `search()`.

## Integrations

- `langchain-core` 1.6.5: `_get_relevant_documents(self, query, *, run_manager)` and `_aget_relevant_documents`. It now pulls in `langchain-protocol`, `langsmith` and `uuid-utils`.
- `llama-index-core` 0.14.25: `_retrieve(self, query_bundle) -> list[NodeWithScore]`. It is heavy (nltk, sqlalchemy, networkx, pillow, tiktoken), so it must stay an optional extra.
