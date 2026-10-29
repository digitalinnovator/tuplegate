# GitHub connector against a real organisation (2026-10-29)

The first real-tenant run. `scripts/validate_github.py` synced a real organisation owned by the maintainer, using the connector permissions-only through OpenFGA 1.21.0. For every (person, repository) pair it then compared our engine's verdict with GitHub's own effective-permission endpoint (`GET /repos/{org}/{repo}/collaborators/{user}/permission`). GitHub, not our oracle, is the referee here.

## Organisation

- 16 repositories: 15 private, 1 public.
- 6 members and 8 outside collaborators.
- 1 team.
- Base permission `write`.
- The sync produced 61 tuples, with 0 unmapped principals (identities passed through as logins).

## Result

| Pairs checked | Agree with GitHub | Over-grants | Under-grants | Engine vs oracle disagreements |
|---:|---:|---:|---:|---:|
| 224 (96 member, 128 outside-collaborator) | **224** | 0 | 0 | 0 |

- **Allowed by GitHub:** 131 pairs.
- **Denied by GitHub:** 93 pairs. These are mostly outside collaborators on repositories they were not added to: the base permission correctly did not reach them.

## Not covered by this organisation

- **Nested teams.** It has one team. The child-team rule ("child teams inherit the parent's access") is still tested only against the fake API.
- **Internal visibility.** No enterprise.
- **Webhooks.** This was a snapshot run; webhook translation is tested against the fake API only.
- **File contents.** The run was permissions-only. File docs inherit their repository's access by construction.

Reproduce, with an org owner's token: `GITHUB_TOKEN=$(gh auth token) uv run python scripts/validate_github.py <org>`.
