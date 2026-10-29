# Roadmap

The v0.1 scope comes from the project brief. Anything outside it waits under "Later" until v0.1 ships with its benchmark.

## v0.1: status as of 2026-10-28

| Item | Status |
|---|---|
| Canonical ACL model, one schema rendered for OpenFGA and SpiceDB (ADR-0002) | Done. Golden files; no recursion inside exclusion |
| Oracle (executable spec, bulk bitset evaluation) | Done |
| Id codec valid for both engines | Done (property tests) |
| OpenFGA backend (HTTP) | Done. Differential tests against the oracle pass on the live engine. The stored model is reused across restarts (fixed 2026-10-28: the comparison now ignores server-filled defaults) |
| SpiceDB backend (gRPC, ZedTokens) | Done. Differential tests pass. `at_least_as_fresh` uses the latest write's ZedToken. Bulk checks are measured at 10,000 items max per call |
| pgvector adapter (iterative scans) | Done. Integration tests pass |
| Qdrant adapter (REST, keyword index on `acl_id`, `MatchAny`, ACORN arm) | Done. Integration tests pass |
| Retrieval strategies: pre-filter, post-filter, iterative, cached pre-filter, plus the gated unfiltered baseline | Done. Every strategy × engine × index is leak-free in `tests/integration/test_leak_matrix.py`. An adaptive strategy was built, measured and rejected (ADR-0004) |
| GitHub connector, with webhooks and signature verification | Done. Validated on a real org: 224/224 pairs agree with GitHub (2026-10-29). Webhooks and nested teams tested against the fake API |
| Google Drive connector (shared drives, limited access, link-only, metadata-only, expirations, Directory nesting incl. `CUSTOMER`, Changes API) | Done against a fake API. Not yet run against a real tenant |
| Confluence connector (all three space-access modes, intersective restrictions, folders as parents, access classes fail closed, CQL changes) | Done. Validated on a real site, including nested view restrictions and a restricted folder: 46/46 user × page verdicts match Confluence |
| Sync engine (per-object diff, tuples before chunks, reconcile plus guarded sweep, expiry sweep, lag samples) | Done |
| Incremental sync per source (ADR-0005) | Done: GitHub webhooks, Drive Changes API (items and drive membership), Confluence CQL. Gaps documented |
| Audit record per decision, with the calling application | Done |
| FastAPI sidecar (`POST /v1/retrieve`, caller tokens, OpenTelemetry spans) | Done. `tuplegate serve` |
| LangChain and LlamaIndex retrievers | Done |
| CI: lint, types, unit tests; integration against the full Compose stack | Workflow written; it has not run on GitHub yet because the repo has no remote |
| Engine-backed benchmark (Q1–Q5 in docs/benchmark/plan.md) and ADR-0004 on the default strategy | Done: [results.md](benchmark/results.md) and [ADR-0004](adr/0004-default-strategy.md). Deviations (fewer seeds, no 100k-doc runs, no principal-pushdown arm) are listed in plan.md |
| Demo: two users, same question, a revoke, the answer changes (GIF) | Done: `make demo`, docs/demo.gif |

## Verification items

Closed on 2026-10-28:

- **Resolution depth.**
  - Measured: OpenFGA Check denies from 25 containers deep.
  - SpiceDB LookupResources fails the whole request from 22.
  - Neither fails open on deep restrictions.
  - Pinned in tests (docs/research/2026-10-28-resolution-depth.md).
- **OpenFGA stored-model shape.** `_same_model` ignores server defaults. An integration test proves the model is reused.

Closed on 2026-10-29:

- **Streamed ListObjects deadline behaviour.** It ends silently with HTTP 200 and no error line. `list_deadline_s` must match the server (docs/research/2026-10-29-list-deadlines.md).
- **GitHub connector on a real org.** All 224 person × repository pairs matched GitHub's own permission endpoint (docs/research/2026-10-29-github-real-org.md).
- **Confluence connector on a real site.** On a Standard trial with a second, ordinary user and nested view restrictions, both people matched Confluence on all 23 pages, and the `all-licensed-users` stand-in was confirmed. Fixed the e-mail lookup that real sites refuse. Root-page ids confirmed (docs/research/2026-10-29-confluence-real-site.md).

Still open:

- **Drive against a real tenant.** It is tested only against a fake API built from the documented response shapes. A real run needs a Google Workspace tenant with admin rights, because shared drives and the Directory API do not exist for personal Google accounts. A Cloud project alone cannot stand in. The plan mirrors the other connectors: a fixture (shared drive, limited-access folder, link-only and domain sharing, an expiring grant, nested groups), with Drive itself as the referee by impersonating each test user through domain-wide delegation.
- **GitHub nested teams and webhooks** are still tested only against the fake API.
- **GitHub repo teams.** Find out whether `GET /repos/{o}/{r}/teams` lists child teams that inherit access. We model nesting ourselves either way.

## Later (parked)

- Benchmark at 100k+ docs (needs a sparse ground-truth matrix), and on a real tenant's anonymised ACL statistics.
- Principal pushdown as a derived filter (ADR-0003 §6, arm 3), especially for subtrees deeper than the engines resolve.
- Verify the end user's OIDC token in the sidecar, instead of trusting the caller's assertion.
- GitHub ETag polling for installs without webhooks. Use the Confluence audit log and Google Reports API as extra feeds once verified (ADR-0005).
- Enterprise-wide visibility of internal GitHub repos (an enterprise connector).
- Drive "My Drive" corpora (v0.1 syncs shared drives).
- Identity links as tuples, so identity mapping changes need no resync (ADR-0002 alternative).
- OpenFGA conditions and SpiceDB caveats for exact expiry.
- An overshare report: "who can retrieve docs labelled X" (the Copilot-style failure mode).
- Canary-probe leak tests for live deployments (plant canary docs with known ACLs, probe from outside users) as a GitHub Action.
- Postgres sync state for multiple sync workers.
- OpenFGA's AuthZEN endpoints as an alternative interface.
- Naming: chosen 2026-10-29 (was the working name "rag-authz"). `tuplegate` was free on PyPI and had no same-named GitHub repository that day. A trademark search is still to do before a public launch.
