# Confluence connector against a real site (2026-10-29)

Two runs on the same site. The first was on the Free plan, which forbids permission changes. The second was on a Standard trial with a second, ordinary user and the view-restriction fixture (`scripts/confluence_fixture.py`). The second run supersedes the first wherever they overlap.

## Run 2: Standard trial, view restrictions, two people

**Fixture:** space TGR, built by `scripts/confluence_fixture.py`.

- **Pages:**
  - an open page;
  - a page restricted to the second user's group (R2), with one restricted child (R3, restricted to the owner's group) and one unrestricted child (R4);
  - a page restricted to a group holding both people (R5), with a child restricted to the owner only (R6);
  - a folder restricted to the second user's group, holding an unrestricted page (R7);
  - a page restricted to an empty group (R8).
- **Who can reach the space:** the second user is an ordinary licensed user and reaches it only through the `all-licensed-users` access class. The owner holds a direct Admin role.
- **Site total:** 5 spaces, 23 pages, 2 folders, 7 restricted nodes.

**A Confluence rule worth knowing:** the API refuses a restriction that locks out the caller ("Provided ContentRestrictions evicts current user (you)… Must include yourself"). The fixture therefore always names the owner, and the denial cases are the second user's.

**The second user hides their e-mail,** so they are mapped through an identity map (ADR-0002 §4), as a deployment would configure.

| Configuration | Subject | Pairs | Agree | Over-grants | Under-grants |
|---|---|---:|---:|---:|---:|
| `all-licensed-users` → `confluence-users-<site>` | owner (by e-mail) | 23 | **23** | 0 | 0 |
| | second user (identity map) | 23 | **23** | 0 | 0 |
| | 14 groups (synthetic member each) | 322 | 307 | 15 (the stand-in group, see run 1) | 0 |
| | 2 app accounts | 46 | 16 | 0 | 30 (unmapped by design) |
| Default (access classes fail closed) | owner | 23 | 21 | 0 | 2 |
| | second user | 23 | 6 | 0 | 17 |

- **The second user is allowed on 17 pages and denied on 6, and every verdict matches Confluence.** The denials include R3, where their access passes R2's restriction and is blocked by R3's (the intersective rule), R6 (restricted to the owner below a page they can see) and R8 (empty group).
- **The stand-in mapping is confirmed for a real non-admin user,** whose only access is the access class.
- **Without the stand-in, the default fails closed:** 0 over-grants, at the cost of every page reached only through `all-licensed-users`.

## Run 1: Free plan

`scripts/validate_confluence.py` synced a real Confluence Cloud site (Free plan, created for this test) through the connector into OpenFGA 1.21.0. For every page it then compared our verdict with Confluence's own permission check (`POST /wiki/rest/api/content/{id}/permission/check`).

## Site

- **Role mode:** `ROLES`.
- **Content:** 3 spaces (one personal, one onboarding, one test space) holding 12 pages and 1 folder, with a page under the folder. Deepest chain: 5 containers.
- **Accounts:** 3 in space role assignments: the owner, and 2 app accounts with no e-mail.
- **Groups:** 10, including the product-access group `confluence-users-<site>`.

## Results

| Configuration | Pairs | Agree | Over-grants | Under-grants |
|---|---:|---:|---:|---:|
| Default (access classes fail closed) | 156 | 132 | **0** | 24 (app accounts) |
| `all-licensed-users` → `confluence-users-<site>` stand-in | 156 | 120 | 12 (see below) | 24 (app accounts) |

- **The mapped user** (the owner, mapped by e-mail) agrees on 12 of 12 pages in both configurations.
- **The app accounts** have no e-mail, so they are unmapped and unclaimable by design (ADR-0002 §4). Those are the 24 under-grants.
- **Groups.** Each group is compared through a synthetic member against Confluence's group-subject check. The 12 disagreements with the stand-in are all the stand-in group. Confluence's group check counts only grants made to the group itself, not access-class grants (`all-licensed-users`) that reach its members. **This test cannot confirm or refute the stand-in.** That would need a second, non-admin user whose only access is the access class. The owner holds direct Admin roles, and the Free plan cannot change role assignments, so it was not possible here. The stand-in stays an explicit operator assumption, and the default stays fail-closed.

## What the real site changed

1. **E-mail lookup.** `GET /wiki/rest/api/user/email/bulk` answers an ordinary API token with **HTTP 400**: "This API is only available to approved Connect apps". The connector treated only 401, 403 and 404 as "not permitted" and aborted. It now falls back to per-account `GET /wiki/rest/api/user?accountId=`, which returns the e-mail when the profile allows. Group-member rows carry it in the same cases. This was found only because of the real site; the fake API had followed the documentation.
2. **Root pages.** Pages created at the top of a space have `parentId` = the space homepage, with `parentType: page`, not an empty parent. The connector already chains through the homepage to the space. This closes the roadmap item on root-page ids.
3. **Folders.** A page under a folder reports `parentType: folder`, and `GET /wiki/api/v2/folders/{id}` returns the folder's own parent, as the connector assumes.

## Not covered (either run)

- Anonymous access and CQL change detection on a live site.
- Restrictions where the owner is denied: Confluence will not let the caller create those.

(The Free plan refuses every permission change: "User isn't authorized to modify permissions because they are using the Free Edition". That is why run 2 needed the Standard trial.)

Reproduce: `set -a; . ~/.config/tuplegate/confluence.env; set +a; uv run python scripts/validate_confluence.py`.
