# ADR-0002: ACL normalisation model

- Status: Accepted
- Date: 2026-09-27, amended 2026-10-28 (mapping rows corrected as the Drive and Confluence connectors were built; see "Amendments")

## Context

Three sources express access in three different ways (details and citations in docs/research/2026-09-27-source-acl-semantics.md):

- **Google Drive** is additive:
  - Grants go to users, groups, domains and "anyone", and they inherit down folders.
  - Limited-access folders (`inheritedPermissionsDisabled`) break inheritance.
  - `domain`/`anyone` grants may be link-only (`allowFileDiscovery: false`).
  - Some grants are metadata-only (`view: "metadata"`) or expire (`expirationTime`).
  - Google groups nest, and `CUSTOMER` members mean "everyone in the domain".
- **GitHub** is additive:
  - Access comes from an org base permission, org owners, nested teams (child teams inherit the parent's access), direct and outside collaborators, and internal/public visibility.
  - Files inherit their repository's access; there are no per-file ACLs.
- **Confluence Cloud** is additive at the space level (space permissions or roles, including access classes such as `anonymous-users`), then **intersective** below it.
  - A page's view restriction applies to every descendant, so a user must pass the restriction of every restricted ancestor.
  - Groups do not nest.

The engines differ too:

- **SpiceDB** object ids allow only `[a-zA-Z0-9/_|\-=+]`.
- **OpenFGA** forbids `:`, `#` and whitespace, and caps `type:id` at 256 bytes.
- **OpenFGA's weighted-graph migration** will reject recursion inside `but not`/`and`.

The central design question is whether to **flatten** ACLs at sync time or **preserve their structure**. Flattening means computing each document's effective principals and storing them, as Elastic DLS and most commercial products do. Preserving structure means storing grants where the source stores them and letting the engine evaluate inheritance. Two facts decide it:

- The Drive Changes API does not emit entries for descendants when a parent's permissions change in a shared drive; clients "must either propagate … or refetch each item".
- Azure AI Search says inherited SharePoint permission changes "require an explicit refresh".

A flattened model turns every folder-level revoke into a rewrite of every descendant. That rewrite is the lag and the leak window this project exists to measure and shrink.

## Decision

### 1. One small schema for every source

Four types, five stored relations and one permission. It is defined once in `src/rag_authz/schema.py` and rendered to `schema/openfga.fga`, `schema/openfga.json` and `schema/spicedb.zed`. A golden test keeps the committed files in sync.

```
type user
type group        member: [user, group#member]
type container    parent: [container]
type doc          viewer: [user, user:*, group#member]
                  restricted: [user:*]
                  restriction_member: [user, group#member]
                  granted      = viewer or granted from parent
                  blocked_here = restricted but not restriction_member
                  blocked      = blocked_here or blocked from parent
                  can_view     = granted but not blocked
```

(`container` and `doc` carry the same relations; `doc` is the only type retrieval asks about.)

Recursion lives only in unions (`granted`, `blocked`). Exclusion applies only to non-recursive relations, the shape OpenFGA's weighted-graph migration requires. A unit test enforces this.

### 2. Preserve structure; never flatten in the canonical form

- **Hierarchy.** A `parent` tuple is written only when the source says the node *inherits*. Breaking inheritance (a Drive limited-access folder) is the absence of that tuple, so toggling it is one tuple write.
- **Additive grants** are `viewer` tuples on the node where the source stores them. A folder grant is one tuple, however many files sit below it.
- **Intersective restrictions** (Confluence) are a `restricted@user:*` marker plus `restriction_member` tuples on the restricted node. `blocked` propagates down `parent`, so a restriction added to a page covers its whole subtree with one change set.
- **Group nesting** is `group:parent#member@group:child#member`. A GitHub child team is a member of its parent team, which matches "child teams inherit the parent's access".

### 3. Documents are the authorization unit; chunks never enter the engine

Every vector chunk carries its document's id twice: `doc_id` (canonical, readable) and `acl_id` (the encoded form below). Tuple count scales with documents, not chunks. Re-chunking or re-embedding never touches the engine.

### 4. Identity resolution happens at sync time and fails closed

- Canonical users are whatever identity the querying application passes (normally the IdP subject or e-mail).
- Connectors map source principals (GitHub login, Atlassian accountId) through an `IdentityResolver`.
- A principal that cannot be mapped is written as `~<source>/<id>`. The retrieval API refuses any subject starting with `~`, so an unmapped principal's grants exist for audit and can never be claimed.
- The count of unmapped principals is a sync metric, because it measures recall lost to identity gaps.
- Changing the mapping requires an ACL-only resync of that source; nothing is re-embedded.

### 5. One id codec for both engines

`rag_authz.ids.encode`:

- keeps SpiceDB-safe characters;
- writes every other UTF-8 byte as `=XX`;
- replaces encodings longer than 200 characters with `=h` plus a SHA-256 digest.

The codec is injective, and its output is valid for both engines. Property tests cover both claims. Readable ids survive: `alice@example.com` becomes `alice=40example=2Ecom`.

### 6. Source mapping rules

| Source fact | Tuples |
|---|---|
| **Drive** `user`/`group` grant, any role, `view` not `metadata` | `viewer@user:…` / `viewer@group:…#member` |
| Drive `domain` or `anyone` grant with `allowFileDiscovery: true` | `viewer@group:gworkspace/domain/<d>#member` / `viewer@user:*` |
| Drive `domain`/`anyone` with `allowFileDiscovery: false` (link-only) | **None.** Link-only is not discovery; search must not make it so |
| Drive `view: "metadata"` | **None.** Metadata access is not content access |
| Drive `expirationTime` | Written; the sync engine's expiry sweep deletes it at expiry. Lag is at most the sweep interval |
| Drive folder with `inheritedPermissionsDisabled` | No `parent` tuple. Inherited `owner`/`organizer` grants are written as direct viewers, because Drive keeps their access to limited folders |
| Drive grants inherited from a parent | **None** on the child (shared-drive `permissionDetails[].inherited`). They arrive through the `parent` tuple |
| Directory group member `USER`/`EXTERNAL` / `GROUP` / `CUSTOMER` | `member@user:…` / `member@group:…#member` / `member@group:gworkspace/customer#member` |
| Directory users | `group:gworkspace/domain/<d>#member@user:…` per primary e-mail domain; `group:gworkspace/customer#member@group:gworkspace/domain/<d>#member` per domain |
| **GitHub** public repo | `viewer@user:*` |
| GitHub org owners | `container:<org>#viewer@group:<org>/owners#member`, always |
| GitHub base permission ≥ read | `container:<org>#viewer@group:<org>/members#member`. A base-permission change is this one tuple |
| GitHub repo, any visibility | `parent@container:<org>`. Outside collaborators are not in `<org>/members`, which matches "base permissions do not apply to outside collaborators" |
| GitHub internal repo | `viewer@group:<org>/members#member` (read minimum even when base is `none`). **Enterprise-wide internal visibility is not modelled in v0.1**: it under-grants, which fails closed |
| GitHub direct and outside collaborators, any role | `viewer@user:…` (all roles include read) |
| GitHub team with repo access | `viewer@group:<org>/team/<slug>#member` |
| GitHub child team | `group:<parent>#member@group:<child>#member` |
| GitHub file | `doc:<org>/<repo>/<path>#parent@container:<org>/<repo>` |
| **Confluence** space permission or role with read (user, group) | `viewer@…` on the space container |
| Confluence access class `anonymous-users` | `viewer@user:*` |
| Confluence access classes such as `all-licensed-users` | `viewer@group:<site>/access/<class>#member`. The group has **no members** unless the operator names a stand-in Confluence group (`access_class_groups`): the API cannot list these populations, so they fail closed |
| Confluence page | `container:<site>/page/<id>#parent@container:<parent node or space>`, plus `doc:<site>/page/<id>#parent@container:<site>/page/<id>` |
| Confluence folder, whiteboard, database or embed above a page | `container:<site>/<type>/<id>`, with its own restriction and parent |
| Confluence view restriction | On the node's container: `restricted@user:*` plus one `restriction_member` per user or group (groups keyed by id) |

### 7. Write ordering

- The sync engine diffs desired tuples against the engine's current tuples for each object and applies the diff in one request when it fits the engine's batch limit (OpenFGA 100, SpiceDB 1,000). Both engines apply a single request atomically.
- A diff that does not fit is split so that *narrowing* operations land first and *widening* operations last, with parent moves in between:
  - narrowing: delete `viewer`, `member`, `restriction_member`; add `restricted`;
  - widening: add `viewer`, `member`, `restriction_member`; delete `restricted`.
- A partially applied change can therefore under-grant but never over-grant.

### 8. Reconciliation is level-triggered

- Every connector has a full reconcile: snapshot, diff, and a sweep of objects no longer seen. That reconcile is what guarantees convergence.
- Incremental feeds (webhooks, change APIs, audit logs) only reduce lag.
- This is required, not optional:
  - GitHub has no webhook for base-permission changes.
  - Confluence restriction changes do not update `lastModified`.
  - Google group-membership events can lag by hours.

### 9. The oracle is the specification

`src/rag_authz/oracle.py` interprets the tuple set independently of any engine. It serves as the unit-test backend, the benchmark's ground truth, and the reference for differential tests: OpenFGA and SpiceDB must agree with it on every (user, doc) pair of a generated organisation.

### Amendments (2026-10-28)

Building the Drive and Confluence connectors changed four rows. The model did not change.

- **`CUSTOMER` is the whole account, not one domain.** Directory's `CUSTOMER` member means every user in the customer account, which can span several domains. It is now its own group, whose members are the per-domain groups.
- **Limited-access folders keep owners and organizers.** Drive documents this, and organizers' grants on a limited folder show up as inherited. Dropping every inherited grant would have locked organizers out, a recall bug the fixture test now pins.
- **A Confluence page is a container *and* a doc.** `parent` must point at a container, and pages are both retrievable and parents. The restriction sits on the container, and the doc hangs below it, so a page's restriction covers itself and its subtree with one change set.
- **Confluence access classes fail closed.** `authenticated-users` and `all-licensed-users` cannot be enumerated through the API (docs/research/2026-10-28-confluence-api.md). An operator who knows that, say, `confluence-users` is everyone licensed maps the class to that group explicitly.

## Consequences

- **A folder-, space- or team-level revoke is a constant number of tuple writes**, however many documents sit below. Propagation lag becomes source-feed lag plus one write. This is the property to demonstrate against flattening products.
- **The cost moves to query time.**
  - The engine walks the hierarchy and evaluates exclusion on every check and list.
  - OpenFGA documents exclusion as "more expensive", and has an open issue where `but not` drove ListObjects to its 3s deadline.
  - ADR-0003's benchmark measures this, and materialization stays available as a derived cache (never the source of truth).
- **Engines cap resolution depth.** Hierarchy depth plus group-nesting depth must stay under the configured limit. A deeper chain fails closed (denied), not open. The connector must report the maximum depth it sees. The exact defaults per engine are still to be verified and tested (roadmap).
- Link-only and metadata-only Drive grants are deliberately invisible to retrieval. Some users will expect "anyone with the link" to be searchable; that is a documented choice, not a bug.
- Internal GitHub repos under-grant for enterprise members outside the org until an enterprise connector exists.
- The oracle and both engines must evolve together. Any schema change updates `schema.py`, the oracle, the golden files and the differential tests in one commit.

## Alternatives considered

- **Flatten to effective principals per document at sync time** (Elastic DLS, Azure AI Search, most commercial products). Queries become simple, and the result can be stored directly in the vector payload. But inherited changes require rewriting every descendant, and intersective restrictions need per-level evaluation anyway. Rejected as the canonical form; kept as a candidate *derived* filter representation for ADR-0003's benchmark.
- **Source-specific types** (`drive_folder`, `gh_repo`, `confluence_page`). They read better, but every new source changes the schema and multiplies the differential-test surface.
- **Identity links as tuples** (`identity:github/octocat#holder@user:alice`). Mapping changes would need no resync, at the cost of one more hop on every check. Deferred until identity churn is shown to matter.
- **Treat link-shared files as public.** Some products do; it leaks link-only files into search. Rejected: fail closed.
- **OpenFGA conditions and SpiceDB caveats for expiring grants.** Exact, but they need time context on every request and behave differently in each engine. Deferred to the roadmap; the sweep bounds lag in the meantime.
