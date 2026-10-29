# ADR-0005: Incremental sync, and what each source lets us promise

- Status: Accepted
- Date: 2026-10-28

## Context

ADR-0002 §8 made reconcile (full snapshot, diff, sweep) the convergence guarantee and treated change feeds as lag reducers. The brief asks for "incremental ACL and deletion sync" on all three connectors. The three sources offer very different feeds (docs/research/2026-09-27-source-acl-semantics.md, docs/research/2026-10-28-confluence-api.md):

| Source | Feed | Does not cover |
|---|---|---|
| Google Drive | Changes API per shared drive: item edits, permission changes, trash, removal | Descendants of a changed parent (no entries are emitted); Directory group membership (the Reports API lags tens of minutes to hours) |
| GitHub | Webhooks: `member`, `membership`, `team`, `team_add`, `organization`, `repository`, `public`, `push` | Base-permission changes (no webhook; the audit log needs Enterprise Cloud) |
| Confluence | CQL `lastmodified` search | Restriction changes (they do not touch `lastModified`), deletions (not searchable), group membership (no events) |

Two design questions: what an incremental event *contains*, and what happens when the feed and reality disagree.

## Decision

### 1. Feeds name what changed; the connector re-reads it

An incremental event never trusts the payload's ACL. The GitHub webhook translator, the Drive change handler and the Confluence CQL path all **re-read the affected object from the API** and emit the same `UpsertNode`/`UpsertGroup` a snapshot would. As a result:

- out-of-order, duplicated and replayed deliveries converge on current state;
- a forged GitHub delivery cannot change an ACL. It can only make us spend rate limit, and `verify_signature` rejects it anyway;
- incremental and snapshot code share one mapping, so the oracle-backed connector tests cover both.

### 2. Structure-preserving tuples make most feeds sufficient

Because grants live where the source stores them (ADR-0002 §2), a change to a parent is one event on the parent. Drive's missing descendant entries and GitHub's team-level changes therefore need nothing more than the one object the feed names. A flattening design would have to enumerate and rewrite descendants on every such event.

### 3. Failure handling is per source, and always fails closed

- **Lost access.** An item the connector can no longer read during incremental sync (Drive 404, GitHub repo 404) is emitted as a deletion. If our identity lost access, the index must lose it too.
- **Other errors** raise. The caller keeps the old feed position (Drive page token, Confluence timestamp) and retries the whole batch, because re-applying an already applied event is a no-op.
- **Repository deletion** (GitHub) deletes the repo container's tuples at once. Its files keep their chunks until the next reconcile sweep, but their only grant path is gone, so nobody can retrieve them in between. This is the "tuples before chunks" rule applied to a source that cannot list what it deleted.

### 4. Reconcile stays mandatory, and the gaps are named

Every deployment runs reconcile on a schedule. Its interval is the worst-case lag for exactly the changes in the "Does not cover" column. These gaps go into the README's limitations, next to the measured lag numbers, never hidden inside them:

- GitHub base-permission changes;
- Google group-membership changes;
- Confluence restriction changes, deletions and group changes.

### 5. Expiring grants are swept, not polled

Drive `expirationTime` grants are recorded in the sync state when written, and `sweep_expired` deletes exactly those tuples when they fall due. A grant that has already expired is never written. Worst-case lag is the sweep interval, and there is no source-feed lag at all, because the expiry time is known in advance.

## Consequences

- An incremental event costs one or a few API reads, not just a payload parse. That is the price of point 1, and it is small next to a reconcile.
- Lag reporting separates three things: source-feed lag (documented per source), sidecar lag (measured: change observed → engine write committed) and enforcement lag (measured: write committed → first compliant response). `benchmarks/revocation_lag.py` measures the last two.
- Confluence has the weakest incremental story. Its reconcile interval should be the shortest of the three; ACL-only reconciles are cheap, because unchanged page versions skip re-embedding.

## Alternatives considered

- **Apply webhook payloads directly.** Fewer API calls, but ordering bugs and forged deliveries become ACL bugs. Rejected.
- **GitHub ETag polling of the org's member and team lists.** Conditional requests that return 304 do not count against the primary rate limit, so this could cover webhook-less installs. Parked in the roadmap: webhooks cover v0.1, and polling is an additive feed.
- **The Confluence audit log as a restriction-change feed.** It needs Confluence admin, and which restriction changes it logs is unconfirmed (docs/research/2026-10-28-confluence-api.md). Parked until verified against a live site.
- **Google Reports API push channels for group membership.** Channels last at most 6 hours and are documented as "not 100% reliable", and the events lag hours. Reconcile is the honest answer for v0.1.
