"""Build the view-restriction fixture on a Confluence Cloud site (Standard plan or above).

Creates a space "TGR", four groups, and a page tree whose restrictions exercise every rule the
connector maps: a single restriction, nested restrictions (intersective), a restricted folder
above a page, a restriction to an empty group, and access that comes only through the
`all-licensed-users` access class. Then run `scripts/validate_confluence.py`.

Confluence will not let the caller restrict themselves out, so every restriction also names the
owner, and the denial cases are the second user's.

Needs the site owner's API token and a second, non-admin user who has logged in once.
Idempotent: re-running reuses what exists. `--delete` removes the space and the groups.

    set -a; . ~/.config/tuplegate/confluence.env; set +a
    uv run python scripts/confluence_fixture.py [--delete]
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

SPACE_KEY = "TGR"
GROUPS = ("tg-owner", "tg-second", "tg-both", "tg-empty")


class Site:
    def __init__(self) -> None:
        base = os.environ["TUPLEGATE_CONFLUENCE_URL"].rstrip("/")
        auth = (
            os.environ["TUPLEGATE_CONFLUENCE_EMAIL"],
            os.environ["TUPLEGATE_CONFLUENCE_API_TOKEN"],
        )
        self.c = httpx.Client(base_url=base, auth=auth, timeout=30.0)

    def ok(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            raise SystemExit(
                f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}"
            )
        return r.json() if r.content else None

    def groups(self) -> dict[str, str]:
        return {g["name"]: g["id"] for g in self.ok(self.c.get("/wiki/rest/api/group"))["results"]}

    def space(self) -> dict[str, Any] | None:
        rs = self.ok(self.c.get("/wiki/api/v2/spaces", params={"keys": SPACE_KEY}))["results"]
        return rs[0] if rs else None


def accounts(site: Site) -> tuple[str, str]:
    me = site.ok(site.c.get("/wiki/rest/api/user/current"))["accountId"]
    users = next(gid for name, gid in site.groups().items() if name.startswith("confluence-users"))
    rows = site.ok(site.c.get(f"/wiki/rest/api/group/{users}/membersByGroupId"))["results"]
    others = [
        r["accountId"] for r in rows if r.get("accountType") == "atlassian" and r["accountId"] != me
    ]
    if len(others) != 1:
        raise SystemExit(f"need exactly one other licensed user; found {len(others)}")
    return me, others[0]


def build(site: Site) -> None:
    owner, second = accounts(site)
    have = site.groups()
    for name in GROUPS:
        if name not in have:
            site.ok(site.c.post("/wiki/rest/api/group", json={"name": name}))
    gid = site.groups()
    members = {
        "tg-owner": [owner],
        "tg-second": [second],
        "tg-both": [owner, second],
        "tg-empty": [],
    }
    for name, accts in members.items():
        current = {
            r["accountId"]
            for r in site.ok(site.c.get(f"/wiki/rest/api/group/{gid[name]}/membersByGroupId"))[
                "results"
            ]
        }
        for a in accts:
            if a not in current:
                site.ok(
                    site.c.post(
                        "/wiki/rest/api/group/userByGroupId",
                        params={"groupId": gid[name]},
                        json={"accountId": a},
                    )
                )

    sp = site.space() or site.ok(
        site.c.post(
            "/wiki/api/v2/spaces",
            json={
                "key": SPACE_KEY,
                "name": "tuplegate test: restrictions",
                "description": {
                    "value": "Test fixture for tuplegate. Safe to delete.",
                    "representation": "plain",
                },
            },
        )
    )
    sid = sp["id"]
    existing = {
        p["title"]: p["id"]
        for p in site.ok(site.c.get(f"/wiki/api/v2/spaces/{sid}/pages", params={"limit": 250}))[
            "results"
        ]
    }

    def page(title: str, parent: str | None = None) -> str:
        if title in existing:
            return existing[title]
        body: dict[str, Any] = {
            "spaceId": sid,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": f"<p>{title}.</p>"},
        }
        if parent:
            body["parentId"] = parent
        return str(site.ok(site.c.post("/wiki/api/v2/pages", json=body))["id"])

    def restrict(
        content_id: str, users: tuple[str, ...] = (), groups: tuple[str, ...] = ()
    ) -> None:
        # One atomic call that replaces the read restriction. Confluence refuses restrictions
        # that lock out the caller ("Must include yourself"), so the owner is always named;
        # the denial cases are the second user's.
        body = [
            {
                "operation": "read",
                "restrictions": {
                    "user": [
                        {"type": "known", "accountId": a} for a in dict.fromkeys((owner, *users))
                    ],
                    "group": [{"type": "group", "id": gid[g]} for g in groups],
                },
            }
        ]
        site.ok(site.c.put(f"/wiki/rest/api/content/{content_id}/restriction", json=body))

    page("R1 open")
    r2 = page("R2 second only")
    restrict(r2, groups=("tg-second",))
    r3 = page("R3 owner only, under R2", r2)
    restrict(r3, groups=("tg-owner",))
    page("R4 unrestricted, under R2", r2)
    r5 = page("R5 both groups")
    restrict(r5, groups=("tg-both",))
    r6 = page("R6 owner only, under R5", r5)
    restrict(r6, users=(owner,))
    # Folders are not listed with pages; a re-run finds the folder through R7's parent.
    f_title = "F restricted to second"
    fid = None
    if "R7 under the restricted folder" in existing:
        r7 = site.ok(site.c.get(f"/wiki/api/v2/pages/{existing['R7 under the restricted folder']}"))
        fid = r7.get("parentId")
    if fid is None:
        fid = str(
            site.ok(site.c.post("/wiki/api/v2/folders", json={"spaceId": sid, "title": f_title}))[
                "id"
            ]
        )
    restrict(fid, groups=("tg-second",))
    page("R7 under the restricted folder", fid)
    r8 = page("R8 empty group")
    restrict(r8, groups=("tg-empty",))
    print(f"fixture ready in space {SPACE_KEY} ({sid}); owner and second user resolved")


def delete(site: Site) -> None:
    sp = site.space()
    if sp:
        site.ok(site.c.delete(f"/wiki/rest/api/space/{SPACE_KEY}"))
        print(f"deleting space {SPACE_KEY} (Confluence moves it to the trash asynchronously)")
    for name, gid in site.groups().items():
        if name in GROUPS:
            site.ok(site.c.delete("/wiki/rest/api/group/by-id", params={"id": gid}))
            print(f"deleted group {name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()
    site = Site()
    (delete if args.delete else build)(site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
