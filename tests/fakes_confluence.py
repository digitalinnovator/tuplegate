"""A fake Confluence Cloud REST API (v2 + the v1 calls we use), served through MockTransport.

Site acme.atlassian.net. Accounts a-olga, a-alice, a-bob, a-carol have e-mails @acme.test;
a-dave hides his e-mail (privacy setting), so he stays unmapped.

- groups: g-eng (bob, carol, dave), g-legal (alice), g-users (olga, alice, bob, carol)
- space S1 (ENG): roles for group g-eng and user olga
  - page P1 "Runbook"                 deploy keys rotate monthly
    - page P2 "Incident 42"           restricted to carol and group g-legal
      - page P3 "Postmortem"          restricted to group g-eng (intersects with P2)
  - folder F1                         restricted to bob
    - page P4 "Bob's notes"
- space S2 (PUB): access classes anonymous-users and all-licensed-users
  - page P5 "Welcome"
- space S3 (HR): access class all-licensed-users only
  - page P6 "Holidays"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode

import httpx

EMAILS = {
    "a-olga": "olga@acme.test",
    "a-alice": "alice@acme.test",
    "a-bob": "bob@acme.test",
    "a-carol": "carol@acme.test",
}


@dataclass
class FakeNode:
    type: str
    space: str
    title: str
    parent: tuple[str, str] | None = None  # (type, id)
    text: str = ""
    version: int = 1
    restrict_users: list[str] = field(default_factory=list)
    restrict_groups: list[str] = field(default_factory=list)


@dataclass
class FakeConfluence:
    mode: str = "ROLES"
    groups: dict[str, list[str]] = field(
        default_factory=lambda: {
            "g-eng": ["a-bob", "a-carol", "a-dave"],
            "g-legal": ["a-alice"],
            "g-users": ["a-olga", "a-alice", "a-bob", "a-carol"],
        }
    )
    spaces: dict[str, str] = field(default_factory=lambda: {"S1": "ENG", "S2": "PUB", "S3": "HR"})
    # space -> [(principalType, principalId)]
    roles: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: {
            "S1": [("GROUP", "g-eng"), ("USER", "a-olga")],
            "S2": [("ACCESS_CLASS", "anonymous-users"), ("ACCESS_CLASS", "all-licensed-users")],
            "S3": [("ACCESS_CLASS", "all-licensed-users")],
        }
    )
    # PRE_ROLES space permissions: space -> [(type, id, key, targetType)]
    permissions: dict[str, list[tuple[str, str, str, str]]] = field(
        default_factory=lambda: {
            "S1": [
                ("group", "g-eng", "read", "space"),
                ("user", "a-olga", "read", "space"),
                ("user", "a-alice", "create", "page"),  # not a view grant
                ("role", "r-1", "read", "space"),  # members not listed: skipped
            ],
            "S2": [],
            "S3": [],
        }
    )
    nodes: dict[str, FakeNode] = field(
        default_factory=lambda: {
            "P1": FakeNode("page", "S1", "Runbook", None, "<p>Deploy keys rotate monthly.</p>"),
            "P2": FakeNode(
                "page",
                "S1",
                "Incident 42",
                ("page", "P1"),
                "<p>Deploy keys leaked.</p>",
                restrict_users=["a-carol"],
                restrict_groups=["g-legal"],
            ),
            "P3": FakeNode(
                "page",
                "S1",
                "Postmortem",
                ("page", "P2"),
                "<p>Root cause.</p>",
                restrict_groups=["g-eng"],
            ),
            "F1": FakeNode("folder", "S1", "Private", None, restrict_users=["a-bob"]),
            "P4": FakeNode("page", "S1", "Bob's notes", ("folder", "F1"), "<p>Notes.</p>"),
            "P5": FakeNode("page", "S2", "Welcome", None, "<p>Hello &amp; welcome.</p>"),
            "P6": FakeNode("page", "S3", "Holidays", None, "<ul><li>Dec 25</li></ul>"),
        }
    )
    modified: list[str] = field(default_factory=list)
    page_size: int = 2
    email_lookup_allowed: bool = True

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    # --- helpers ---------------------------------------------------------------------------

    def _v2_page(self, req: httpx.Request, items: list[Any]) -> httpx.Response:
        q = parse_qs(req.url.query.decode())
        start = int(q.pop("cursor", ["0"])[0])
        chunk = items[start : start + self.page_size]
        body: dict[str, Any] = {"results": chunk, "_links": {}}
        if start + self.page_size < len(items):
            q["cursor"] = [str(start + self.page_size)]
            body["_links"]["next"] = f"{req.url.path}?{urlencode(q, doseq=True)}"
        return httpx.Response(200, json=body)

    def _v1_page(self, req: httpx.Request, items: list[Any]) -> httpx.Response:
        q = parse_qs(req.url.query.decode())
        start, limit = int(q.get("start", ["0"])[0]), int(q.get("limit", ["25"])[0])
        chunk = items[start : start + limit]
        return httpx.Response(
            200, json={"results": chunk, "start": start, "limit": limit, "size": len(chunk)}
        )

    def _node_json(self, nid: str, body: bool) -> dict[str, Any]:
        n = self.nodes[nid]
        out: dict[str, Any] = {
            "id": nid,
            "title": n.title,
            "status": "current",
            "spaceId": n.space,
            "parentId": n.parent[1] if n.parent else None,
            "parentType": n.parent[0] if n.parent else None,
            "version": {"number": n.version, "createdAt": "2026-10-01T00:00:00.000Z"},
        }
        if body:
            out["body"] = {"storage": {"representation": "storage", "value": n.text}}
        return out

    # --- routes ----------------------------------------------------------------------------

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path)
        q = parse_qs(req.url.query.decode())
        if path == "/wiki/api/v2/space-role-mode":
            return httpx.Response(200, json={"mode": self.mode})
        if path == "/wiki/rest/api/group":
            return self._v1_page(req, [{"type": "group", "id": g, "name": g} for g in self.groups])
        m = re.fullmatch(r"/wiki/rest/api/group/([^/]+)/membersByGroupId", path)
        if m:
            rows = [{"type": "known", "accountId": a, "email": ""} for a in self.groups[m.group(1)]]
            return self._v1_page(req, rows)
        if path == "/wiki/rest/api/user/email/bulk":
            if not self.email_lookup_allowed:
                return httpx.Response(403)
            return httpx.Response(
                200,
                json=[
                    {"accountId": a, "email": EMAILS[a]}
                    for a in q.get("accountId", [])
                    if a in EMAILS
                ],
            )
        if path == "/wiki/api/v2/spaces":
            return self._v2_page(
                req,
                [
                    {"id": s, "key": k, "type": "global", "status": "current"}
                    for s, k in self.spaces.items()
                ],
            )
        m = re.fullmatch(r"/wiki/api/v2/spaces/([^/]+)", path)
        if m:
            return httpx.Response(200, json={"id": m.group(1), "key": self.spaces[m.group(1)]})
        m = re.fullmatch(r"/wiki/api/v2/spaces/([^/]+)/role-assignments", path)
        if m:
            rows = [
                {"principal": {"principalType": t, "principalId": i}, "roleId": "viewer"}
                for t, i in self.roles[m.group(1)]
            ]
            return self._v2_page(req, rows)
        m = re.fullmatch(r"/wiki/api/v2/spaces/([^/]+)/permissions", path)
        if m:
            rows = [
                {
                    "id": str(n),
                    "principal": {"type": t, "id": i},
                    "operation": {"key": k, "targetType": tt},
                }
                for n, (t, i, k, tt) in enumerate(self.permissions[m.group(1)])
            ]
            return self._v2_page(req, rows)
        m = re.fullmatch(r"/wiki/api/v2/spaces/([^/]+)/pages", path)
        if m:
            body = q.get("body-format") == ["storage"]
            rows = [
                self._node_json(nid, body)
                for nid, n in self.nodes.items()
                if n.space == m.group(1) and n.type == "page"
            ]
            return self._v2_page(req, rows)
        m = re.fullmatch(r"/wiki/api/v2/(pages|folders)/([^/]+)", path)
        if m:
            if m.group(2) not in self.nodes:
                return httpx.Response(404)
            return httpx.Response(
                200, json=self._node_json(m.group(2), q.get("body-format") == ["storage"])
            )
        m = re.fullmatch(r"/wiki/rest/api/content/([^/]+)/restriction/byOperation/read", path)
        if m:
            n = self.nodes[m.group(1)]
            return httpx.Response(
                200,
                json={
                    "operation": "read",
                    "restrictions": {
                        "user": {
                            "results": [{"type": "known", "accountId": a} for a in n.restrict_users]
                        },
                        "group": {
                            "results": [
                                {"type": "group", "name": g, "id": g} for g in n.restrict_groups
                            ]
                        },
                    },
                },
            )
        if path == "/wiki/rest/api/search":
            rows = [{"content": {"id": i, "type": "page"}} for i in self.modified]
            return httpx.Response(200, json={"results": rows, "_links": {}})
        return httpx.Response(404, json={"message": f"no route {path}"})
