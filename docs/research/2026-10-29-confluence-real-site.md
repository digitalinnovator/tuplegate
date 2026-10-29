# Confluence connector against a real site (2026-10-29)

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

## Not covered

- **View restrictions:** the core intersective rule. The Free plan refuses every permission change: "User isn't authorized to modify permissions because they are using the Free Edition". This applies to page restrictions and space role assignments alike, through both the v1 and v2 APIs. Restrictions remain tested against the fake API only. Validating them needs a Standard-plan site (or trial) with at least two users.
- Anonymous access and CQL change detection on a live site.

Reproduce: `set -a; . ~/.config/tuplegate/confluence.env; set +a; uv run python scripts/validate_confluence.py`.
