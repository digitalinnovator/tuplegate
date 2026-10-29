# C4 level 2: containers

v0.1 is one Python package (`tuplegate`) that runs in two shapes: imported into an application (library mode) or behind a FastAPI process (sidecar mode, `tuplegate serve`). The boxes inside the boundary are modules, not services. Each is drawn separately because it has its own contract and ADR.

```mermaid
C4Container
    title Containers: tuplegate (v0.1)

    System_Ext(app, "RAG application", "LangChain / LlamaIndex / custom")
    System_Ext(sources, "Sources", "Google Drive, GitHub, Confluence")
    System_Ext(engine, "OpenFGA / SpiceDB", "Tuples + can_view")
    System_Ext(vdb, "pgvector / Qdrant", "Chunks with doc_id + acl_id")
    System_Ext(siem, "Audit sink", "JSON lines / SIEM")

    System_Boundary(s, "tuplegate") {
        Container(api, "Retrieval API", "FastAPI", "POST /v1/retrieve; caller tokens; the enforcement boundary in sidecar mode")
        Container(sdk, "Retrievers", "langchain-core; llama-index-core", "Retriever bound to one subject")
        Container(retr, "Retrieval engine", "Python", "pre_filter / post_filter / iterative / cached_pre_filter; safety invariant; audit (ADR-0003)")
        Container(authz, "Authz adapters", "httpx (OpenFGA); gRPC (SpiceDB); oracle (memory)", "list_docs, check_docs, read, write with safe ordering")
        Container(index, "Index adapters", "psycopg + pgvector; httpx (Qdrant REST); exact numpy", "Filtered search, replace_doc, delete_docs")
        Container(conn, "Connectors", "httpx", "GitHub (webhooks), Drive (Changes API), Confluence (CQL). Emit NodeAcl / GroupMembers / Delete events (ADR-0005)")
        Container(sync, "Sync engine", "Python", "Reconcile + incremental; per-object diff; sweep; expiry sweep; lag metrics")
        ContainerDb(state, "Sync state", "SQLite", "Seen objects, versions, generations, expiring grants")
        Container(model, "Canonical model + schema", "dataclasses", "4 types, 5 relations; renders OpenFGA JSON/DSL and SpiceDB (ADR-0002)")
        Container(oracle, "Oracle", "Python", "Executable spec; ground truth; differential tests")
        Container(leak, "Leak harness + synthetic org", "numpy", "Leak rate, recall@k, latency by selectivity bucket")
    }

    Rel(app, api, "retrieve(subject, vector, k)", "HTTP")
    Rel(app, sdk, "invoke(query)", "in-process")
    Rel(sdk, retr, "retrieve")
    Rel(api, retr, "retrieve")
    Rel(retr, authz, "list / batch-check")
    Rel(retr, index, "search with acl filter")
    Rel(retr, siem, "AuditRecord")
    Rel(conn, sources, "REST / GraphQL / webhooks")
    Rel(sync, conn, "snapshot(), changes()")
    Rel(sync, authz, "read + write diffs")
    Rel(sync, index, "replace_doc / delete_docs")
    Rel(sync, state, "cursors, generations")
    Rel(authz, engine, "HTTP / gRPC")
    Rel(index, vdb, "SQL / REST")
    Rel(leak, retr, "probes")
    Rel(leak, oracle, "ground truth")

    UpdateLayoutConfig($c4ShapeInRow="4", $c4BoundaryInRow="1")
```

## Request path: authorised retrieval

```mermaid
sequenceDiagram
    autonumber
    participant App as RAG app
    participant R as Retrieval engine
    participant E as OpenFGA / SpiceDB
    participant V as pgvector / Qdrant
    participant A as Audit sink
    App->>R: retrieve(subject, q, k, strategy)
    alt pre_filter (cached_pre_filter: list reused for a TTL, then batch-check of the top k)
        R->>E: streamed list-objects(doc, can_view, subject)
        E-->>R: acl_ids (+ complete?)
        R->>V: ANN(q, k) WHERE acl_id IN acl_ids
        V-->>R: hits (re-checked against acl_ids)
    else post_filter / iterative
        loop until k allowed or budget spent
            R->>V: ANN(q, window, offset)
            V-->>R: candidates
            R->>E: batch-check(subject, distinct new acl_ids)
            E-->>R: allowed / denied (errors deny)
        end
    end
    R->>A: AuditRecord(returned, denied, stats)
    R-->>App: hits
```

## Sync path: a revoke reaches the index and the engine

```mermaid
sequenceDiagram
    autonumber
    participant S as Source
    participant C as Connector
    participant Y as Sync engine
    participant E as Engine
    participant V as Vector index
    S-->>C: change (webhook / feed / reconcile)
    C->>Y: UpsertNode(NodeAcl) / DeleteNode / UpsertGroup
    Y->>E: read(object) → current tuples
    Y->>E: write(diff) — narrowing first if split
    Note over Y,E: revoke is live here for every strategy
    Y->>V: delete_docs / replace_doc (deletions only)
    Y->>Y: record lag = now − changed_at
```

Order matters on delete: tuples go first, so the document is unretrievable before its chunks are purged. A failed purge leaves orphan chunks that no strategy can return, because no tuple grants them. `replace_doc` then removes them before any re-ingest under the same id, so a later, wider ACL cannot expose them.
