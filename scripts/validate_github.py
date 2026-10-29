"""Validate the GitHub connector against a real org, with GitHub itself as the referee.

For every (person, repository) pair, where people are org members and outside collaborators,
GitHub's effective-permission endpoint
(``GET /repos/{org}/{repo}/collaborators/{user}/permission``) says whether that person can read
the repository. The same question goes to OpenFGA after an ACL-only sync of the org: one probe
doc per repository inherits the repository's access. Any disagreement is printed.

Read-only against GitHub. It needs a token that can read org members and every repository
(an org owner's). Prints aggregate counts; with ``--show-mismatches`` it also prints the
disagreeing logins and repositories. Keep those out of the repo.

    GITHUB_TOKEN=$(gh auth token) uv run python scripts/validate_github.py <org>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from typing import Any

import httpx

from tuplegate import ids
from tuplegate.authz.base import Consistency
from tuplegate.authz.openfga import OpenFGABackend
from tuplegate.connectors.github import GitHubConnector, repo_container
from tuplegate.identity import IdentityPassthrough
from tuplegate.model import NodeAcl, Tuple, UpsertGroup, UpsertNode, container, doc
from tuplegate.oracle import Oracle
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


def pages(c: httpx.Client, path: str, **params: Any) -> list[Any]:
    out: list[Any] = []
    url: str | None = path
    p: dict[str, Any] | None = {"per_page": 100, **params}
    while url:
        r = c.get(url, params=p)
        r.raise_for_status()
        out += r.json()
        url = r.links.get("next", {}).get("url")
        p = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("org")
    ap.add_argument("--openfga", default="http://127.0.0.1:58080")
    ap.add_argument("--show-mismatches", action="store_true")
    args = ap.parse_args()
    token = os.environ["GITHUB_TOKEN"]
    gh = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )
    org = args.org

    # 1. Sync the org (ACL only) through the real connector into a fresh OpenFGA store.
    conn = GitHubConnector(org, IdentityPassthrough(), token=token, include_content=False)
    events = list(conn.snapshot())
    tuples: set[Tuple] = set()
    for ev in events:
        if isinstance(ev, UpsertNode | UpsertGroup):
            tuples |= (ev.acl if isinstance(ev, UpsertNode) else ev.group).tuples()
    repos = pages(gh, f"/orgs/{org}/repos", type="all")
    probes = {r["name"]: doc(f"{repo_container(org, r['name'])}/__probe__") for r in repos}
    probe_events = [
        UpsertNode(NodeAcl(p, parent=container(repo_container(org, name))))
        for name, p in probes.items()
    ]
    fga = OpenFGABackend(args.openfga, store_name=f"validate-github-{uuid.uuid4().hex[:8]}")
    fga.ensure_schema()
    try:
        rep = SyncEngine(fga, _NoIndex()).reconcile("github", [*events, *probe_events])
        if rep.errors:
            print("sync errors:", rep.errors[:5], file=sys.stderr)
            return 2

        # 2. Ask GitHub, per person and repository, and compare with OpenFGA and the oracle.
        people = {m["login"] for m in pages(gh, f"/orgs/{org}/members")}
        outside = {m["login"] for m in pages(gh, f"/orgs/{org}/outside_collaborators")}
        oracle = Oracle(frozenset(tuples) | {t for e in probe_events for t in e.acl.tuples()})
        counts: Counter[str] = Counter()
        mismatches: list[dict[str, Any]] = []
        for login in sorted(people | outside):
            subject = login.lower()
            acl_ids = [ids.encode(p.id) for p in probes.values()]
            engine = fga.check_docs(subject, acl_ids, Consistency.FULL)
            for r in repos:
                name = r["name"]
                resp = gh.get(f"/repos/{org}/{name}/collaborators/{login}/permission")
                resp.raise_for_status()
                perm = resp.json().get("permission", "none")
                truth = perm != "none" or not r.get("private", True)
                ours = engine[ids.encode(probes[name].id)]
                spec = oracle.can_view(subject, probes[name])
                kind = "member" if login in people else "outside"
                counts[f"pairs_{kind}"] += 1
                counts[f"truth_{'allow' if truth else 'deny'}"] += 1
                if ours != spec:
                    counts["engine_vs_oracle_disagree"] += 1
                if ours == truth:
                    counts["agree"] += 1
                else:
                    counts["over_grant" if ours else "under_grant"] += 1
                    mismatches.append(
                        {"login": login, "repo": name, "github": perm, "ours": ours, "kind": kind}
                    )
        summary = {
            "org_repos": len(repos),
            "members": len(people),
            "outside_collaborators": len(outside),
            "teams": conn.stats.teams,
            "base_permission": conn.stats.base_permission,
            "unmapped_principals": conn.stats.unmapped_principals,
            "tuples": len(tuples),
            **dict(counts),
        }
        print(json.dumps(summary, indent=2))
        if args.show_mismatches and mismatches:
            print(json.dumps(mismatches, indent=2))
        return 1 if counts["over_grant"] else 0
    finally:
        fga.delete_store()
        fga.close()


if __name__ == "__main__":
    raise SystemExit(main())
