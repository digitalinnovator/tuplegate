# Source ACL semantics and competitors: verified facts (2026-09-27)

Checked against primary docs, vendor schemas and competitor source on 2026-09-27 to back ADR-0002. Tags: **[C]** confirmed in a primary doc, **[S]** from code or a secondary source, **[?]** unconfirmed. Re-check before quoting.

## Findings that change the brief

1. **PipesHub already syncs source ACLs and enforces them at query time** (Apache-2.0, 3,777★, pushed 2026-09-26) [S, code]. Evidence:
   - It stores permission edges in ArangoDB or Neo4j (`backend/python/app/models/permission.py`, with entity types USER, GROUP, DOMAIN, ANYONE, ANYONE_WITH_LINK and others).
   - The Drive connector reads `permissionDetails`.
   - The Confluence connector reads space permissions, access classes and page READ restrictions, and syncs restriction changes from the audit log.
   - It has a GitHub teams connector.
   - Retrieval computes accessible record ids and pushes a Qdrant `must virtualRecordId in [...]` pre-filter.
   - Gaps found in those files:
     - No `allowFileDiscovery`: Drive `anyone` is one category, so link-only sharing looks public.
     - No `inheritedPermissionsDisabled`.
     - No Directory nested-group expansion.
     - No published leak-rate or propagation-lag numbers.
   - **Consequence:** "no open-source project does permission sync" is false. The defensible position is narrower:
     - an app-agnostic sidecar rather than a search app;
     - Zanzibar engines (OpenFGA/SpiceDB) instead of a private graph store;
     - more than one filtering strategy;
     - differential correctness tests;
     - measured deletion and revocation lag.
2. **SWIRL** does federated search with no index and no ACL sync. The Confluence and GitHub providers use one shared credential, so results reflect that account's access [S, code].
3. **Dust** is app-level only (Dust Spaces). It skips Confluence pages with view restrictions and their children, and I found no per-file Drive ACL [C, docs + code].
4. **Revocation-lag floors come from the sources, not only from us.**
   - Google group-membership changes reach the Reports API in "tens of minutes (can also go up to a couple of hours)".
   - GitHub has **no webhook for base-permission changes**.
   - Confluence restriction changes **do not update `lastModified`**.
   - Any published lag number must separate source-feed lag from sidecar lag.
5. **Drive permission changes on a parent in a shared drive do not produce change entries for descendants.** Clients "must either propagate the new capabilities or refetch each item". A model that keeps the hierarchy as `parent` tuples propagates with one tuple write; a flattened model has to rewrite every descendant.

## Google Drive (API v3) and Admin SDK

- **Permission types and roles [C]:**
  - `type` is one of `user|group|domain|anyone`. `role` is one of `owner|organizer|fileOrganizer|writer|commenter|reader`, and every role reads content.
  - A permission with **`view: "metadata"`** exposes metadata only (limited-access parents). It must not map to content read.
  - `view: "published"` means `publishedReader`.
  - Grants of type `user` and `group` can carry an `expirationTime`.
  - File `permissions` are not populated for shared-drive items, so call `permissions.list` with `supportsAllDrives`.
- **`allowFileDiscovery` [C]:** "Whether the permission allows the file to be discovered through search. This is only applicable for permissions of type `domain` or `anyone`." The Drive audit log names the two cases: `people_with_link`/`people_within_domain_with_link` (discovery false) versus `public_on_the_web`/`public_in_the_domain` (true). Link-only access also needs the resource key on first open.
- **Limited access, `inheritedPermissionsDisabled` [C]:**
  - GA since 2025-02-19. "When `true`, only the `owner` role, the `organizer` role, and users with direct folder permissions can access it."
  - `permissionDetails[].inherited` is always populated. `inheritedFrom` is populated only for shared-drive items.
- **Changes API [C]:**
  - A parent change produces no per-descendant entries (see finding 5).
  - Loss of access appears as `removed=true`.
  - Each change log belongs to one user or one shared drive.
  - The Reports API has Drive ACL events, e.g. `change_user_access` and `change_user_access_hierarchy_reconciled`. Their lag is "near real time (couple of minutes)".
- **Directory API [C]:**
  - Member types are `USER|GROUP|EXTERNAL|CUSTOMER`. `GROUP` is a nested group; `CUSTOMER` is "all users in a domain".
  - `members.list` takes `includeDerivedMembership`. `members.hasMember` resolves nesting only within one domain.
  - Membership changes: Reports API `ADD_GROUP_MEMBER`/`REMOVE_GROUP_MEMBER`, with push channels that last at most 6h and are "not 100% reliable".
- **Quotas [C]:**
  - Drive quotas are in quota units since 2026-05-01: 1,000,000 per minute per project and 325,000 per minute per user. A `get` costs 5 and a `list` costs 100.
  - Directory API: 2,400 queries per minute per user.

## GitHub

- **Team members [C]:** `GET /orgs/{org}/teams/{slug}/members` "will include the members of child teams". Each member has `role` and an **`inherited`** flag that marks memberships coming from a child team. GraphQL `Team.members(membership: IMMEDIATE|CHILD_TEAM|ALL)`, `childTeams`, `parentTeam` and `ancestors` all exist.
- **Nesting [C]:** "Child teams inherit the parent's access permissions." "Members of a child team are not direct members of the parent team." Secret teams cannot nest.
- **Base permission [C]:**
  - `default_repository_permission` is one of `read|write|admin|none`, default `read`. "Base permissions do not apply to outside collaborators."
  - Reading it requires an org owner, or `admin:org` for classic tokens.
  - **Internal repos have a minimum of read for org members even when base is `none`.**
- **Internal visibility [C]:**
  - "Organization members have read permissions to all internal repositories in an enterprise, including those in organizations they are not a member of. Internal repositories are not visible to people outside of the enterprise, including outside collaborators."
  - EMU guest collaborators can see internal repos only in orgs they belong to.
- **Collaborators [C]:**
  - `affiliation=direct` returns every collaborator with a repo-level grant. `outside` returns outside collaborators. `all` (the default) adds team, base-permission and owner access.
  - `GET /repos/{o}/{r}/teams` lists teams with access. Whether it includes inherited child teams is [?]; we model nesting ourselves.
- **Webhooks [C]:**
  - `member`, `membership`, `team` (`added_to_repository`, `removed_from_repository`, `edited`, `deleted`), `team_add`, `organization` (`member_added`, `member_removed`), `repository` (`privatized`, `publicized`, `deleted`, `transferred`, `edited`) and `public`.
  - **Base-permission changes have no webhook.** They show only as the audit event `org.update_default_repository_permission`, and the audit-log API needs Enterprise Cloud.
- **Rate limits [C]:**
  - PAT: 5,000/h. App installation: 5,000–12,500/h, or 15,000 on Enterprise Cloud.
  - Secondary limits: 100 concurrent requests, 900 points per minute per endpoint.
  - A conditional request that returns `304` "does not count against your primary rate limit".
- **Identity [C]:**
  - `Organization.samlIdentityProvider.externalIdentities` → `samlIdentity { nameId, emails }` and `scimIdentity`, plus `user`.
  - Visible to org owners, or to Apps with Members read.

## Confluence Cloud

- **Roles [C]:**
  - Role-based space access is GA and the default on new sites. `GET /space-role-mode` returns `PRE_ROLES|ROLES_TRANSITION|ROLES`.
  - v2 `GET /spaces/{id}/permissions` returns principals of type `user|group|role`.
  - `GET /spaces/{id}/role-assignments` returns principals of type `USER|GROUP|ACCESS_CLASS`. The access classes are `anonymous-users`, `authenticated-users`, `all-licensed-users`, `all-product-admins` and `jsm-project-admins`.
  - The connector must handle all three modes.
- **Restrictions [C]:**
  - v1 only: `GET /wiki/rest/api/content/{id}/restriction`.
  - "View restrictions added to the parent item or folder are inherited down to all content items nested under it. Editing doesn't work this way." "A content item cannot have greater access than its container."
  - So a user must pass **every** restricted ancestor's view restriction: intersective, not additive.
  - `POST /wiki/rest/api/content/{id}/permission/check` evaluates the effective permission for any subject (it needs Confluence Admin). That makes it a live-source test oracle.
- **Groups [C]:** "Atlassian Cloud doesn't support nested groups."
- **Email [C]:** `User.email` may be empty under privacy settings. `GET /wiki/rest/api/user/email` and `/email/bulk` work regardless, with scope `read:email-address:confluence`.
- **Change detection [C/S]:**
  - Webhooks exist: `content_permissions_updated` and `space_permissions_updated` (Connect), and `avi:confluence:permissions_updated:*` (Forge).
  - There are no group-membership events, and cascading deletions emit no events.
  - Restriction changes do not update `lastModified` [S]. The audit API covers group membership and permission changes.
- **Rate limits [C]:** points-based since 2026-03-02. Identity and permission GETs cost 2 points. The default app pool is 65,000 points per hour. API-token traffic stays on burst limits.
