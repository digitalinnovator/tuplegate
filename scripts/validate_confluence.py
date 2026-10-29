"""Validate the Confluence connector against a real site, with Confluence itself as the referee.

The connector syncs the site into a fresh OpenFGA store. Then, for every page, Confluence's own
permission check (``POST /wiki/rest/api/content/{id}/permission/check``, which needs Confluence
admin) answers for each subject:

- every user account that appears in a space's role assignments;
- every group, compared with a synthetic user who is a member of that group only.

Our answer comes from OpenFGA for the same subject. Any disagreement is printed. Read-only
against Confluence.

    set -a; . ~/.config/tuplegate/confluence.env; set +a
    uv run python scripts/validate_confluence.py \
        --stand-in all-licensed-users=confluence-users-<site>
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections import Counter
from typing import Any

import httpx

from tuplegate import ids
from tuplegate.authz.base import Consistency
from tuplegate.authz.openfga import OpenFGABackend
from tuplegate.connectors.confluence import ConfluenceConnector
from tuplegate.identity import MappingResolver
from tuplegate.model import GroupMembers, UpsertGroup, UpsertNode, group, user
from tuplegate.sync import SyncEngine


class _NoIndex:
    name = "none"

    def ensure_schema(self) -> None: ...
    def upsert(self, chunks: Any) -> None: ...
    def replace_doc(self, doc_id: str, chunks: Any) -> None: ...
    def delete_docs(self, doc_ids: Any) -> int:
        return 0

    def search(self, vector: Any, k: int, *, acl_filter: Any = None, offset: int = 0) -> list[Any]:
        return []

    def count(self) -> int:
        return 0

    def close(self) -> None: ...


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--openfga", default="http://127.0.0.1:58080")
    ap.add_argument(
        "--stand-in",
        action="append",
        default=[],
        help="access-class=group-name (e.g. all-licensed-users=confluence-users-<site>)",
    )
    ap.add_argument("--show-mismatches", action="store_true")
    args = ap.parse_args()
    base = os.environ["TUPLEGATE_CONFLUENCE_URL"].rstrip("/")
    auth = (os.environ["TUPLEGATE_CONFLUENCE_EMAIL"], os.environ["TUPLEGATE_CONFLUENCE_API_TOKEN"])
    c = httpx.Client(base_url=base, auth=auth, timeout=30.0)

    groups = {g["name"]: g["id"] for g in c.get("/wiki/rest/api/group").json()["results"]}
    stand_in = {}
    for spec in args.stand_in:
        cls, _, name = spec.partition("=")
        stand_in[cls] = groups[name]

    accounts: set[str] = set()
    # Every licensed person (members of any group) plus accounts with direct space roles.
    for gid in groups.values():
        rows = c.get(f"/wiki/rest/api/group/{gid}/membersByGroupId").json()["results"]
        accounts |= {r["accountId"] for r in rows if r.get("accountType") == "atlassian"}
    for sp in c.get("/wiki/api/v2/spaces", params={"limit": 250}).json()["results"]:
        for a in c.get(f"/wiki/api/v2/spaces/{sp['id']}/role-assignments").json()["results"]:
            if a["principal"]["principalType"] == "USER":
                accounts.add(a["principal"]["principalId"])
    canonical: dict[str, str] = {}  # account -> the id our engine is asked about
    kinds: dict[str, str] = {}
    hidden: dict[str, str] = {}
    for acct in accounts:
        u = c.get("/wiki/rest/api/user", params={"accountId": acct}).json()
        if u.get("email"):
            canonical[acct], kinds[acct] = u["email"].lower(), "user_by_email"
        elif u.get("accountType") == "atlassian":
            # A person whose profile hides the e-mail: what an identity map is for.
            hidden[acct] = canonical[acct] = f"{acct}@identity-map.validate.test"
            kinds[acct] = "user_by_identity_map"
        else:
            kinds[acct] = "app_account"  # unmapped, unclaimable by design

    conn = ConfluenceConnector(
        base,
        email=auth[0],
        api_token=auth[1],
        identity=MappingResolver({"confluence": hidden}),
        access_class_groups=stand_in,
        include_content=False,
    )
    events = list(conn.snapshot())
    pages = [e.acl.ref for e in events if isinstance(e, UpsertNode) and e.acl.ref.type == "doc"]
    # One synthetic member per group: "a user whose only membership is this group".
    probes = {gid: f"probe-{gid}@validate.test" for gid in groups.values()}
    probe_events = [
        UpsertGroup(GroupMembers(group(conn.group_id(gid)), frozenset({user(p)})))
        for gid, p in probes.items()
    ]
    fga = OpenFGABackend(args.openfga, store_name=f"validate-confluence-{uuid.uuid4().hex[:8]}")
    fga.ensure_schema()
    try:
        rep = SyncEngine(fga, _NoIndex()).reconcile("confluence", events)
        # Probe memberships are added on top of the real ones, not instead of them.
        for ev in probe_events:
            current = fga.read(ev.group.ref)
            fga.write(ev.group.tuples() - current, [])
        if rep.errors:
            print("sync errors:", rep.errors[:5])
            return 2

        counts: Counter[str] = Counter()
        mismatches: list[dict[str, Any]] = []
        acl_ids = [ids.encode(p.id) for p in pages]

        def compare(
            kind: str, subject: dict[str, str], ours_for: dict[str, bool] | None, label: str
        ) -> None:
            for p, a in zip(pages, acl_ids, strict=True):
                page_id = p.id.rsplit("/", 1)[-1]
                r = c.post(
                    f"/wiki/rest/api/content/{page_id}/permission/check",
                    json={"subject": subject, "operation": "read"},
                )
                r.raise_for_status()
                truth = bool(r.json().get("hasPermission"))
                ours = bool(ours_for and ours_for[a])
                counts[f"{kind}_pairs"] += 1
                counts[f"truth_{'allow' if truth else 'deny'}"] += 1
                if ours == truth:
                    counts["agree"] += 1
                else:
                    counts["over_grant" if ours else "under_grant"] += 1
                    counts[f"{kind}_{'over' if ours else 'under'}_grant"] += 1
                    mismatches.append(
                        {"kind": kind, "subject": label, "page": page_id, "confluence": truth}
                    )

        for acct in sorted(accounts):
            who = canonical.get(acct)
            ours = fga.check_docs(who, acl_ids, Consistency.FULL) if who else None
            compare(kinds[acct], {"type": "user", "identifier": acct}, ours, kinds[acct])
        for name, gid in sorted(groups.items()):
            ours = fga.check_docs(probes[gid], acl_ids, Consistency.FULL)
            compare("group", {"type": "group", "identifier": gid}, ours, name)

        print(
            json.dumps(
                {
                    "mode": conn.stats.mode,
                    "spaces": conn.stats.spaces,
                    "pages": conn.stats.pages,
                    "other_nodes": conn.stats.other_nodes,
                    "restricted_nodes": conn.stats.restricted_nodes,
                    "groups": conn.stats.groups,
                    "max_depth": conn.stats.max_depth,
                    "unmapped_access_classes": conn.stats.unmapped_access_classes,
                    "unmapped_principals": conn.stats.unmapped_principals,
                    "accounts": len(accounts),
                    "accounts_by_kind": dict(Counter(kinds.values())),
                    **dict(counts),
                },
                indent=2,
            )
        )
        if args.show_mismatches and mismatches:
            print(json.dumps(mismatches, indent=2))
        return 1 if counts["over_grant"] else 0
    finally:
        fga.delete_store()
        fga.close()


if __name__ == "__main__":
    raise SystemExit(main())
