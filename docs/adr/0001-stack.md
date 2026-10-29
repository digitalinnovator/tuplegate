# ADR-0001: Implementation stack and deployment shape

- Status: Accepted
- Date: 2026-09-27, amended 2026-10-28 (Qdrant over REST, sidecar and tracing as built, Google auth by callable)

## Context

The project syncs source ACLs (Google Drive, GitHub, Confluence) into a Zanzibar-style engine (OpenFGA or SpiceDB) and enforces them inside vector search (pgvector or Qdrant). It has two kinds of consumer:

- **RAG applications** that call it on every retrieval. Most are Python and use LangChain or LlamaIndex. Some are not Python at all.
- **The benchmark**, which drives synthetic organisations through every strategy, engine and store combination and must be reproducible from one command.

Constraints from the brief: typed code, tests from the first commit, CI on every push, lockfiles and pinned dependencies, a one-command dev setup, structured logs, OpenTelemetry where it fits, and no secrets in the repo. After the LiteLLM PyPI compromise (2026-03-24), every dependency is a liability to justify.

Qdrant removed JWT payload filters in 1.16 (docs/research/2026-09-27-engines-and-vector-stores.md), so per-point enforcement needs a trusted process that injects filters. That process is where the trust boundary sits.

## Decision

| Concern | Choice |
|---|---|
| Language | Python 3.12+ (CI tests 3.12 and 3.13) |
| Shape | A library core (`rag_authz`) plus a thin FastAPI sidecar (`rag_authz.server`, `rag-authz serve`) that exposes the same retrieve call. See "Trust boundary" below |
| Packaging | uv. `uv.lock` pins every environment we run (dev, CI, benchmark, the sidecar image) with hashes, and `exclude-newer` blocks packages published after the pin date |
| Dependency ranges | Library-style ranges in `pyproject.toml`, because the SDK and retrievers install into users' applications, where exact pins cause resolver conflicts. This differs from the sibling projects, which are applications |
| Optional extras | `pgvector` (psycopg 3, pgvector-python), `spicedb` (authzed), `server` (FastAPI, uvicorn, opentelemetry-api), `langchain` (langchain-core), `llamaindex` (llama-index-core, which is heavy), `bench` (numpy). Qdrant needs no extra (see the next row). Core depends only on httpx, pydantic and typer |
| OpenFGA client | Its HTTP API through httpx. We use six endpoints; the official SDK is aiohttp-based and brings more transitive dependencies |
| Qdrant client | Its REST API through httpx (added 2026-10-28). We use seven endpoints; `qdrant-client` would add grpcio, numpy and portalocker to every install that wants Qdrant |
| Google auth | The Drive connector takes a callable that returns an OAuth access token, so the library does not depend on `google-auth`. The module docstring shows the service-account pattern |
| SpiceDB client | The official `authzed` gRPC client (extra `spicedb`). gRPC is SpiceDB's primary API; AuthZed does not maintain HTTP clients. grpcio and protobuf install from wheels on every CI and dev platform we use, Intel macOS included (checked 2026-10-28) |
| Canonical model | Frozen, slotted dataclasses: hashable, so a sync diff is a set difference. Pydantic v2 only at I/O boundaries (config, HTTP API) |
| Sync state | SQLite through the stdlib `sqlite3` module, for a single sync worker in v0.1 |
| CLI | Typer |
| Logging and audit | Stdlib `logging` for operational logs. A separate `AuditSink` stream carries one JSON record per retrieval decision (ADR-0003) |
| Tracing | `rag_authz.tracing` opens `opentelemetry-api` spans around retrieve when the API is installed (the `server` extra) and is a no-op otherwise. Never the SDK |
| Dev services | Docker Compose with images pinned by version: pgvector 0.8.6 on Postgres 17, Qdrant 1.19.1, OpenFGA 1.21.0 (Postgres datastore), SpiceDB 1.56.2 (Postgres datastore) |
| Quality gates | ruff (lint and format), mypy `--strict`, pytest with Hypothesis; tests marked `integration` run against Compose |
| CI | GitHub Actions: lint, types and unit tests on 3.12 and 3.13; an integration job with service containers |
| Licence | Apache-2.0 |

### Trust boundary

Enforcement is only as strong as the process that holds the vector-store credentials. Two deployment modes, one code path:

- **Sidecar mode (the enforcement boundary).**
  - The sidecar holds the vector-store and engine credentials. The application holds only a sidecar credential, so it cannot query the index unfiltered.
  - The application asserts the end user's identity. v0.1 trusts an authenticated caller to assert the subject; verifying the end user's OIDC token is on the roadmap.
  - Callers authenticate with bearer tokens. The sidecar stores only SHA-256 digests (`rag-authz hash-token`), compares them in constant time, and writes the caller's name into every audit record.
  - The unfiltered baseline is not reachable over HTTP, whatever the retriever's configuration.
  - This is the mode for Qdrant after 1.16, and for any team that does not trust every application with direct index access.
- **Library mode.**
  - The application imports `rag_authz` and calls the engine and index directly. It is faster, with no extra hop, but the application is inside the trust boundary.
  - It is for trusted services, tests and the benchmark.

## Consequences

- One Python codebase serves the SDK, the LangChain/LlamaIndex retrievers, the sidecar and the benchmark. Retrieval strategies are written once.
- **The sidecar hop costs latency**, and the benchmark reports it separately from strategy cost. If it dominates, the Go alternative below comes back.
- Core installs with three small dependencies. Engine and store clients arrive only with the extra that needs them.
- SQLite limits v0.1 to one sync worker per source. That is enough for three connectors; horizontal sync moves to Postgres (see roadmap).
- Pinned Compose images make the benchmark reproducible, but they will go stale. Bumping them is a deliberate commit that re-runs the differential tests. OpenFGA must stay at 1.21.0 or later because of the 2026 exclusion and wildcard advisories.

## Alternatives considered

- **Go for the sidecar.**
  - OpenFGA and SpiceDB are Go, and a static binary is a clean sidecar.
  - But the retriever integrations and the benchmark tooling are Python, so we would maintain two codebases and two implementations of each strategy.
  - Revisit if the measured sidecar overhead matters.
- **The `openfga-sdk` Python package.** It works, but it is aiohttp-based and heavier than the six HTTP calls we need.
- **`qdrant-client`.** The supported client, with typed models. Rejected for the same reason as the OpenFGA SDK: its transitive dependencies outweigh the handful of REST calls we make. Revisit if Qdrant's REST API changes faster than we can track.
- **SpiceDB's HTTP gateway**, to keep one transport. It has no maintained clients and is a second-class path for streaming LookupResources.
- **Exact pins in `pyproject.toml`**, as in the sibling application repos. That is wrong for a package users install into their own environments; the lockfile gives the same reproducibility for our environments.
- **Postgres for sync state from day one.** It is needed for multiple sync workers, and unnecessary for v0.1. A Qdrant-only user should not need Postgres.
