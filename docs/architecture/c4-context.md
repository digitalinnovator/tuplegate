# C4 level 1: system context

The sidecar sits between a RAG application and the vector store. It never sees a model prompt or generates an answer. It decides which chunks a user may retrieve and keeps that decision in step with the source systems.

```mermaid
C4Context
    title System context: permission-aware retrieval sidecar (v0.1)

    Person(user, "End user", "Asks questions through a RAG app")
    Person(sec, "Platform / security engineer", "Configures sources, reads audit, runs leak tests in CI")

    System(app, "RAG application", "LangChain, LlamaIndex or custom; owns chunking, embedding and generation")
    System(sidecar, "rag-authz sidecar", "Syncs source ACLs into ReBAC tuples; enforces them inside vector search; audits every decision")

    System_Ext(gdrive, "Google Drive + Admin SDK", "Files, permissions, groups")
    System_Ext(github, "GitHub", "Orgs, teams, repos, files")
    System_Ext(confluence, "Confluence Cloud", "Spaces, pages, restrictions, groups")
    System_Ext(idp, "Identity provider", "Canonical user identity; source identity mapping")
    System_Ext(engine, "OpenFGA or SpiceDB", "Zanzibar-style authorization engine")
    System_Ext(vdb, "pgvector or Qdrant", "Vector index with chunk payloads")
    System_Ext(siem, "Log pipeline / SIEM", "Audit records")

    Rel(user, app, "Asks")
    Rel(app, sidecar, "retrieve(subject, query vector, k)")
    Rel(sidecar, gdrive, "Reads ACLs, changes, content")
    Rel(sidecar, github, "Reads ACLs, webhooks, content")
    Rel(sidecar, confluence, "Reads ACLs, audit log, content")
    Rel(sidecar, idp, "Maps source principals to users")
    Rel(sidecar, engine, "Writes tuples; list / batch-check")
    Rel(sidecar, vdb, "Filtered search; deletes")
    Rel(sidecar, siem, "One record per decision")
    Rel(sec, sidecar, "Configures; runs leak-rate tests")

    UpdateLayoutConfig($c4ShapeInRow="4", $c4BoundaryInRow="1")
```

## Boundaries that matter

- **Trust boundary (ADR-0001).** In sidecar mode, only the sidecar holds vector-store and engine credentials. The application cannot query the index unfiltered. In library mode, the application is inside the boundary.
- **Ownership of content.** The application owns chunking and embedding. The sidecar owns ACLs and deletions, and the contract between them is that every chunk carries `doc_id`. An optional ingest helper can drive the application's chunker and embedder from connector events, which keeps the demo to one command without taking an opinion on either.
- **Sources of lag.** Revocation lag has two parts: the source's own feed lag (for example, hours for Google group changes, and no webhook for GitHub base permission) and the sidecar's lag (one tuple write, ADR-0002). The benchmark reports them separately.
