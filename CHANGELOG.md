# Changelog

## 0.1.0 (unreleased)

First release. Everything below is new.

- **Model:** one schema for both engines (four types, five relations); an id codec valid for both; expiring grants.
- **Engines:** OpenFGA (HTTP) and SpiceDB (gRPC) backends, each differentially tested against the oracle on generated organisations. Consistency modes `full`, `minimize_latency` and `at_least_as_fresh` (SpiceDB ZedTokens).
- **Indexes:** pgvector (iterative scans) and Qdrant (REST; optional ACORN).
- **Strategies:** `pre_filter`, `post_filter`, `iterative` and `cached_pre_filter`, behind one safety invariant. `unfiltered` exists as the benchmark's leak baseline only.
- **Connectors:**
  - GitHub, with webhook translation and signature verification;
  - Google Drive, with shared drives, limited-access folders, link-only and metadata-only grants, Directory groups and the Changes API;
  - Confluence Cloud, with all three space-access modes, intersective view restrictions and CQL change detection.
- **Sync engine:** per-object diffs, tuples before chunks, reconcile with a guarded sweep, an expiry sweep and lag metrics.
- **Sidecar:** `POST /v1/retrieve` with caller tokens, fail-closed errors and per-decision audit records (with the caller); `rag-authz serve`.
- **Integrations:** LangChain and LlamaIndex retrievers bound to one subject.
- **Benchmarks:** filtering strategies under skew, revocation lag and revoke cost, sidecar overhead. All committed with their results.
