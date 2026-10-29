"""A fake GitHub REST API with mutable state, served through httpx.MockTransport.

The fixture organisation "acme":

- owners: olga. members: olga, alice, bob, carol. dave is an outside collaborator (unmapped).
- teams: engineering (bob) > backend (carol). backend is a child of engineering.
- repos:
  - api (private): team backend; outside collaborator dave.
  - handbook (internal): all org members.
  - website (public).
  - payroll (private): direct collaborator alice.
- base permission: none.

Expected visibility: olga everything; alice handbook, website, payroll; bob handbook, website
(bob is in the *parent* team, and access does not flow up); carol api, handbook, website;
eve (not in the org) website only.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@dataclass
class FakeRepo:
    visibility: str
    teams: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeGitHub:
    org: str = "acme"
    base_permission: str | None = "none"
    owners: list[str] = field(default_factory=lambda: ["olga"])
    members: list[str] = field(default_factory=lambda: ["olga", "alice", "bob", "carol"])
    # slug -> (parent slug or None, immediate members)
    teams: dict[str, tuple[str | None, list[str]]] = field(
        default_factory=lambda: {
            "engineering": (None, ["bob"]),
            "backend": ("engineering", ["carol"]),
        }
    )
    repos: dict[str, FakeRepo] = field(
        default_factory=lambda: {
            "api": FakeRepo(
                "private",
                teams=["backend"],
                collaborators=["dave"],
                files={
                    "README.md": "The api service. Deploy keys rotate every 30 days "
                    "via the vault job.",
                    "src/app.py": "def rotate_deploy_keys():\n    return vault.rotate('deploy')\n",
                },
            ),
            "handbook": FakeRepo(
                "internal",
                files={"onboarding.md": "Welcome. Ask in #help about deploy keys and laptops."},
            ),
            "website": FakeRepo("public", files={"index.md": "Acme builds deploy tooling."}),
            "payroll": FakeRepo(
                "private",
                collaborators=["alice"],
                files={"salaries.md": "Salary bands and bonus plan for 2026. Confidential."},
            ),
        }
    )
    page_size: int = 2
    requests: int = 0

    def _children(self, slug: str) -> list[str]:
        return [s for s, (p, _) in self.teams.items() if p == slug]

    def _all_team_members(self, slug: str) -> list[tuple[str, bool]]:
        # Like the real endpoint: includes child-team members, flagged inherited=true.
        out = [(m, False) for m in self.teams[slug][1]]
        for c in self._children(slug):
            out += [(m, True) for m, _ in self._all_team_members(c)]
        return out

    # --- routing ---------------------------------------------------------------------------

    def _page(self, req: httpx.Request, items: list[Any]) -> httpx.Response:
        q = parse_qs(req.url.query.decode())
        page = int(q.get("page", ["1"])[0])
        size = self.page_size
        chunk = items[(page - 1) * size : page * size]
        headers = {}
        if page * size < len(items):
            nxt = req.url.copy_merge_params({"page": str(page + 1)})
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests += 1
        parts = [p for p in req.url.path.split("/") if p]
        q = parse_qs(req.url.query.decode())
        org = self.org
        match parts:
            case ["orgs", o] if o == org:
                body: dict[str, Any] = {"login": org}
                if self.base_permission is not None:
                    body["default_repository_permission"] = self.base_permission
                return httpx.Response(200, json=body)
            case ["orgs", o, "members"] if o == org:
                who = self.owners if q.get("role") == ["admin"] else self.members
                return self._page(req, [{"login": m} for m in who])
            case ["orgs", o, "teams"] if o == org:
                return self._page(
                    req,
                    [
                        {"slug": s, "parent": {"slug": p} if p else None}
                        for s, (p, _) in self.teams.items()
                    ],
                )
            case ["orgs", o, "teams", slug, "members"] if o == org and slug in self.teams:
                return self._page(
                    req, [{"login": m, "inherited": inh} for m, inh in self._all_team_members(slug)]
                )
            case ["orgs", o, "repos"] if o == org:
                return self._page(req, [self._repo_json(n) for n in self.repos])
            case ["repos", o, name] if o == org and name in self.repos:
                return httpx.Response(200, json=self._repo_json(name))
            case ["repos", o, name, "collaborators"] if o == org and name in self.repos:
                return self._page(req, [{"login": c} for c in self.repos[name].collaborators])
            case ["repos", o, name, "teams"] if o == org and name in self.repos:
                return self._page(req, [{"slug": t} for t in self.repos[name].teams])
            case ["repos", o, name, "git", "trees", _branch] if o == org and name in self.repos:
                files = self.repos[name].files
                if not files:
                    return httpx.Response(409, json={"message": "Git Repository is empty."})
                tree = [
                    {"path": p, "type": "blob", "sha": _sha(t), "size": len(t)}
                    for p, t in files.items()
                ]
                return httpx.Response(200, json={"tree": tree, "truncated": False})
            case ["repos", o, name, "git", "blobs", sha] if o == org and name in self.repos:
                for t in self.repos[name].files.values():
                    if _sha(t) == sha:
                        return httpx.Response(
                            200,
                            json={
                                "content": base64.b64encode(t.encode()).decode(),
                                "encoding": "base64",
                            },
                        )
        return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

    def _repo_json(self, name: str) -> dict[str, Any]:
        r = self.repos[name]
        return {
            "name": name,
            "visibility": r.visibility,
            "private": r.visibility != "public",
            "default_branch": "main",
            "html_url": f"https://github.com/{self.org}/{name}",
        }

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="https://api.github.test", transport=httpx.MockTransport(self.handler)
        )


IDENTITIES = {
    "github": {
        "olga": "olga@acme.test",
        "alice": "alice@acme.test",
        "bob": "bob@acme.test",
        "carol": "carol@acme.test",
        # dave is an outside collaborator with no corporate identity: unmapped.
    }
}
