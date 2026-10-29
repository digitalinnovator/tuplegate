# Confluence Cloud REST: the calls the connector uses (2026-10-28)

Checked against Atlassian's published OpenAPI specs on 2026-10-28: v2 at `dac-static.atlassian.com/cloud/confluence/openapi-v2.v3.json` and v1 at `.../swagger.v3.json`. The developer.atlassian.com reference pages are generated from these specs. Tags: **[C]** confirmed in the spec or an Atlassian doc, **[I]** our inference, **[?]** unconfirmed. Re-check before quoting.

## Pagination

- **v2 [C]:** `cursor` plus `limit` (default 25, max 250). The next page is in `_links.next` (relative to the site) and in the `Link` header. "`_links.next` will not be present if there is no additional data available."
- **v1 [C]:** `start` plus `limit`. Whether `_links.next` is always present when more results exist is [?], so the connector stops when a page comes back shorter than `limit`.

## Space access

- `GET /wiki/api/v2/space-role-mode` returns `{"mode": "PRE_ROLES" | "ROLES_TRANSITION" | "ROLES"}` [C]. It needs scope `read:configuration:confluence`.
- **Pre-roles:** `GET /wiki/api/v2/spaces/{id}/permissions` [C].
  - Each entry is `{principal: {type: user|group|role, id}, operation: {key, targetType}}`.
  - "Can view the space" is `key = read` with `targetType = space`. That pair follows from the enums; the spec does not name it explicitly [I].
  - `role` principals carry no member list. The connector skips them and counts how many it skipped.
- **Roles:** `GET /wiki/api/v2/spaces/{id}/role-assignments` [C]. Each entry is `{principal: {principalType: USER|GROUP|ACCESS_CLASS, principalId}, roleId}`.
  - **Every role includes viewing** [C]. Atlassian's "Create and manage custom roles" says: "View content is required for any role, therefore it cannot be removed." So any assignment grants view.
- **Access class ids [C]:** `anonymous-users`, `authenticated-users`, `all-licensed-users`, `all-product-admins`, `jsm-project-admins`.
  - What they mean is only partly documented. "Anonymous users" means people who are not logged in. The other populations cannot be listed through the API [?].
  - Hence the fail-closed default: anything other than `anonymous-users` maps to an empty group unless the operator names a stand-in group.

## Content tree

- `GET /wiki/api/v2/spaces/{id}/pages?depth=all&status=current&body-format=storage` [C]. It returns `id, title, spaceId, parentId, parentType, version.number, body.storage.value`.
- `parentType` is one of `page | whiteboard | database | embed | folder` [C]. A page can sit under a folder, and a folder can carry a view restriction, so the connector syncs non-page ancestors as well (`GET /wiki/api/v2/{type}s/{id}`).
- There is no plain-text body format [C]. The connector strips the storage-format XHTML itself.

## View restrictions

- `GET /wiki/rest/api/content/{id}/restriction/byOperation/read` (v1) [C]. It returns `restrictions.user.results[].accountId` and `restrictions.group.results[]` with both `name` and `id`.
  - Group-specific restriction calls take the id, so **groups are keyed by id, not name**.
  - Users and groups page separately with `start`/`limit`.
- A newer v2 equivalent exists: `GET /wiki/api/v2/pages/{id}/permissions?operation=read`, which returns principals with `principalType` and `principalId` [C]. v0.1 uses the long-standing v1 call. The v2 call is a candidate once it has a track record.
- Semantics (from the 2026-09-27 note): a view restriction is inherited by all descendants, and a user must pass the restriction on every restricted ancestor.

## Groups and identities

- `GET /wiki/rest/api/group` returns `{id, name, ...}` [C]. Members come from `GET /wiki/rest/api/group/{groupId}/membersByGroupId` [C]; the older `/group/member?groupId=` call is gone from the spec. Each member has `accountId` and an `email` that is often empty because of privacy settings.
- `GET /wiki/rest/api/user/email/bulk?accountId=…` returns `[{accountId, email}]` [C].
  - It needs scope `read:email-address:confluence` (beta). Connect apps need Atlassian's approval to use it.
  - The connector falls back to an identity map when the call is refused.

## Change detection

- CQL `lastmodified` works through `GET /wiki/rest/api/search?cql=…` [C]. Dates are read in the API user's time zone, so the connector re-reads with an overlap.
- `GET /wiki/rest/api/audit` exists [C]. It needs Confluence admin and `read:audit-log:confluence`. Which permission and restriction changes it logs is [?], so v0.1 does not depend on it.
- From the 2026-09-27 note: restriction changes do not update `lastModified`, and group membership has no change events. **Reconcile is what converges them.**
