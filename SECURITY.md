# Security policy

rag-authz decides which documents a user may retrieve. A bug that returns a document the source would not show that user is a security vulnerability, and reports are welcome.

## Reporting a vulnerability

Please report it privately, through **Report a vulnerability** on this repository's Security tab (GitHub private vulnerability reporting). Do not open a public issue.

Include what you found, how to reproduce it (a tuple set, a connector fixture or a request is ideal), and what an attacker could retrieve. You should get a reply within a week.

## What counts

In scope:

- **Leaks:** any strategy, engine or index combination returning a hit the oracle (`rag_authz.oracle`) says the subject cannot view.
- **Mapping errors** that widen access, where a connector grants more than the source does. Under-granting is a bug, not a vulnerability.
- **Fail-open paths:** an engine, index or connector error that results in results being returned instead of nothing.
- **Sidecar authentication:** caller-token checks, subject validation, and reaching the unfiltered baseline over HTTP.

Out of scope:

- Vulnerabilities in OpenFGA, SpiceDB, pgvector, Qdrant or the source APIs. Report those to their maintainers. We do want to hear about engine bugs that our differential tests should have caught.
- **Documented limitations** (README, "Limitations"):
  - the sidecar trusts an authenticated caller to assert the end user;
  - lag bounded by the reconcile interval for changes a source does not publish.

## Supported versions

Only the latest release is supported while the project is pre-alpha.
