# What a list deadline does: OpenFGA 1.21.0 and SpiceDB 1.56.2 (measured 2026-10-29)

This closes the roadmap item "streamed ListObjects deadline behaviour".

## OpenFGA

A second OpenFGA 1.21.0 was started against the same datastore with `OPENFGA_LIST_OBJECTS_DEADLINE=50ms`, then asked to list for a user who can see all 10,000 docs of the skewed organisation.

- **The stream ends silently.** The response is HTTP 200 with no error line and no marker: 700–800 results instead of 10,000, or none at all on a cold call.
- **Every id it does send is authorised,** so a truncated list costs recall, never safety.
- **The only signal is time.** `OpenFGABackend` reports `complete=False` when the stream ran for at least 90% of `list_deadline_s`:
  - with `list_deadline_s=0.05`, all three test lists were reported incomplete;
  - with the default `3.0` against a 50 ms server, the same truncated lists were reported **complete**.
- **Rule:** set `RAG_AUTHZ_OPENFGA_LIST_DEADLINE_S` to the server's `OPENFGA_LIST_OBJECTS_DEADLINE`. The audit record's `list_complete` is only as good as that setting.

## SpiceDB

Our backend sets the gRPC deadline itself (`list_deadline_s`, default 3 s), and a cut-off stream raises `DEADLINE_EXCEEDED`, so `complete=False` is exact.

On the skewed organisation, listing for the broadest probe users took up to **2.92 s**, just under that deadline. Under load some lists can cross it, and the strategy then loses recall for that query. The `pre_filter` recall of 0.999 for SpiceDB in the skewed benchmark is consistent with that, or with HNSW approximation; the saved results do not distinguish the two. `cached_pre_filter` avoids the issue for warm users.
