# Resolution depth on our schema: OpenFGA v1.21.0 and SpiceDB v1.56.2 (measured 2026-10-28)

This closes the roadmap item "Engine resolution-depth defaults and how deep hierarchies fail". It was measured on the pinned Compose images with default settings, using our schema (`schema/`). The test is a chain of N containers, each inheriting from the one above, with a doc at the bottom. Reproduce with `tests/integration/test_resolution_depth.py`, which pins the results below so that an image bump that changes them fails CI.

## Results

**Deep grant.** The grant is at the top of the chain, so the oracle says *allow*.

| Engine | Check allows up to | Check denies from | List (ListObjects / LookupResources) |
|---|---:|---:|---|
| OpenFGA 1.21.0 | 24 containers | 25 | Lists the doc at every depth tested (up to 60) |
| SpiceDB 1.56.2 | 45 containers | 48 | **Fails the whole request from 22 containers** (`FAILED_PRECONDITION: max depth exceeded`). 21 resolves |

**Deep restriction.** The restriction is at the top, with a direct grant on the leaf, so the oracle says *deny*. Both engines deny at depths 10, 30 and 60, in both check and list. **Neither fails open.**

## What it means

- **Safety holds.** Past every limit, the engines deny or fail the request. Our backends turn a per-item check error into a denial and a list error into a failed request (ADR-0003 §1). No measured case returned an unauthorised doc.
- **Recall and availability do not.**
  - On OpenFGA, docs below 25 or more containers are denied by `post_filter` and `iterative` (Check) but returned by `pre_filter` (ListObjects). The strategies disagree on recall for deep docs, though not on safety.
  - On SpiceDB, one chain of 22 or more containers that a user can reach makes **every** `pre_filter` request for that user fail. LookupResources spends more dispatch depth per level than Check, because each level of our schema evaluates both `granted` and `blocked`.
- **Real sources:** GitHub is always 3 deep (org, repo, file). Drive and Confluence trees can exceed 21 levels in old, heavily nested tenants. The Drive and Confluence connectors now report `max_depth` and log a warning above 21.
- **Mitigations, in order of preference:**
  1. Raise SpiceDB's `--dispatch-max-depth` (default 50). It is a server flag, at the cost of deeper worst-case evaluation.
  2. Use `post_filter`/`iterative` for affected users on SpiceDB. Check still resolves up to 45.
  3. Flatten only the over-deep subtrees, as a derived cache (ADR-0003 §6, arm 3). This is parked.

## Not measured

- Group-nesting depth. GitHub teams and Google groups nest. The same budget applies, shared with the container chain, so a deep tree plus deep group nesting fails earlier.
- OpenFGA's `OPENFGA_RESOLVE_NODE_LIMIT` and breadth limits under wide fan-out.
