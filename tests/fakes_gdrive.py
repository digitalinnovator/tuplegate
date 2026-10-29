"""A fake Google Drive v3 + Admin SDK Directory API with mutable state (httpx.MockTransport).

The fixture account, domain acme.test:

- users: olga, alice, bob, carol (acme.test). eve@elsewhere.test is outside the account.
- groups: eng@ (bob, and the group backend@), backend@ (carol), everyone@ (CUSTOMER).
- shared drive D1 "Engineering": members eng@ (reader), olga (organizer).
  - folder F_api                  (no direct grants: inherits D1)
    - doc api-design (Google Doc) "Deploy keys rotate every 30 days."
  - folder F_secret               limited access; direct alice (writer); inherited eng, olga
    - file secret.txt             "Board minutes. Acquisition of Initech."
  - file link.txt                 anyone with the link (allowFileDiscovery false)
  - file public.md                anyone, discoverable
  - file domain.md                domain acme.test, discoverable; also "metadata" view for eve
    - file guest.md               eve, expiring in one day
  - file all-hands.md             group everyone@ (CUSTOMER member)

Expected visibility is EXPECTED in tests/unit/test_gdrive_connector.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"


def _perm(
    pid: str,
    type_: str,
    role: str = "reader",
    *,
    email: str | None = None,
    domain: str | None = None,
    discover: bool | None = None,
    inherited: bool = False,
    view: str | None = None,
    expires: datetime | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": pid,
        "type": type_,
        "role": role,
        "permissionDetails": [{"inherited": inherited, "role": role}],
    }
    if email:
        p["emailAddress"] = email
    if domain:
        p["domain"] = domain
    if discover is not None:
        p["allowFileDiscovery"] = discover
    if view:
        p["view"] = view
    if expires:
        p["expirationTime"] = expires.isoformat().replace("+00:00", "Z")
    return p


@dataclass
class FakeFile:
    name: str
    mime: str
    parent: str
    perms: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    limited: bool = False
    trashed: bool = False
    modified: str = "2026-09-01T00:00:00Z"


@dataclass
class FakeDrive:
    domain: str = "acme.test"
    users: list[str] = field(
        default_factory=lambda: [
            "olga@acme.test",
            "alice@acme.test",
            "bob@acme.test",
            "carol@acme.test",
        ]
    )
    # email -> [(type, email or None)]
    groups: dict[str, list[tuple[str, str | None]]] = field(
        default_factory=lambda: {
            "eng@acme.test": [("USER", "bob@acme.test"), ("GROUP", "backend@acme.test")],
            "backend@acme.test": [("USER", "carol@acme.test")],
            "everyone@acme.test": [("CUSTOMER", None)],
        }
    )
    drives: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            "D1": [
                _perm("p-eng", "group", "reader", email="eng@acme.test"),
                _perm("p-olga", "user", "organizer", email="olga@acme.test"),
            ]
        }
    )
    files: dict[str, FakeFile] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    page_size: int = 2
    fail_once: set[str] = field(default_factory=set)
    suspended: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.files:
            return
        inh_eng = _perm("p-eng", "group", "reader", email="eng@acme.test", inherited=True)
        inh_olga = _perm("p-olga", "user", "organizer", email="olga@acme.test", inherited=True)
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        self.files = {
            "F_api": FakeFile("api", FOLDER, "D1", [inh_eng, inh_olga]),
            "api-design": FakeFile(
                "API design",
                GDOC,
                "F_api",
                [inh_eng, inh_olga],
                "Deploy keys rotate every 30 days.",
            ),
            "F_secret": FakeFile(
                "secret",
                FOLDER,
                "D1",
                [inh_eng, inh_olga, _perm("p-alice", "user", "writer", email="alice@acme.test")],
                limited=True,
            ),
            "secret.txt": FakeFile(
                "secret.txt",
                "text/plain",
                "F_secret",
                [
                    inh_eng,
                    inh_olga,
                    _perm("p-alice", "user", "writer", email="alice@acme.test", inherited=True),
                ],
                "Board minutes. Acquisition of Initech.",
            ),
            "link.txt": FakeFile(
                "link.txt",
                "text/plain",
                "D1",
                [inh_eng, inh_olga, _perm("p-any", "anyone", discover=False)],
                "Shared by link only.",
            ),
            "public.md": FakeFile(
                "public.md",
                "text/markdown",
                "D1",
                [inh_eng, inh_olga, _perm("p-any", "anyone", discover=True)],
                "Acme builds deploy tooling.",
            ),
            "domain.md": FakeFile(
                "domain.md",
                "text/markdown",
                "D1",
                [
                    inh_eng,
                    inh_olga,
                    _perm("p-dom", "domain", domain="acme.test", discover=True),
                    _perm("p-eve", "user", email="eve@elsewhere.test", view="metadata"),
                ],
                "Holiday calendar.",
            ),
            "guest.md": FakeFile(
                "guest.md",
                "text/markdown",
                "D1",
                [
                    inh_eng,
                    inh_olga,
                    _perm("p-eve", "user", email="eve@elsewhere.test", expires=tomorrow),
                ],
                "Contractor brief.",
            ),
            "all-hands.md": FakeFile(
                "all-hands.md",
                "text/markdown",
                "D1",
                [inh_eng, inh_olga, _perm("p-all", "group", email="everyone@acme.test")],
                "All-hands notes.",
            ),
        }
        self.files["guest.md"].parent = "F_secret"

    # --- mutation helpers ------------------------------------------------------------------

    def record_drive_change(self) -> None:
        self.changes.append(
            {
                "changeType": "drive",
                "driveId": "D1",
                "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )

    def record_change(self, file_id: str, removed: bool = False) -> None:
        self.changes.append(
            {
                "changeType": "file",
                "fileId": file_id,
                "removed": removed,
                "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )

    # --- transport -------------------------------------------------------------------------

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _page(self, items: list[Any], key: str, params: dict[str, str]) -> httpx.Response:
        start = int(params.get("pageToken", "0") or 0)
        chunk = items[start : start + self.page_size]
        body: dict[str, Any] = {key: chunk}
        if start + self.page_size < len(items):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)

    def _file_json(self, fid: str) -> dict[str, Any]:
        f = self.files[fid]
        return {
            "id": fid,
            "name": f.name,
            "mimeType": f.mime,
            "parents": [f.parent],
            "driveId": "D1",
            "trashed": f.trashed,
            "inheritedPermissionsDisabled": f.limited,
            "modifiedTime": f.modified,
            "md5Checksum": str(hash(f.text) & 0xFFFFFFFF),
            "size": str(len(f.text)),
            "webViewLink": f"https://drive.example/{fid}",
        }

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path)
        params = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        self.requests.append(path)
        if path in self.fail_once:
            self.fail_once.discard(path)
            return httpx.Response(503, headers={"Retry-After": "0"}, text="try later")
        if path.endswith("/directory/v1/users"):
            return self._page([{"primaryEmail": u} for u in self.users], "users", params)
        if path.endswith("/directory/v1/groups"):
            return self._page([{"email": g} for g in self.groups], "groups", params)
        m = re.search(r"/directory/v1/groups/([^/]+)/members$", path)
        if m:
            ms = [
                {
                    "type": t,
                    "status": "SUSPENDED" if e in self.suspended else "ACTIVE",
                    **({"email": e} if e else {}),
                }
                for t, e in self.groups[m.group(1)]
            ]
            return self._page(ms, "members", params)
        m = re.search(r"/drive/v3/files/([^/]+)/permissions$", path)
        if m:
            fid = m.group(1)
            if fid in self.drives:
                return self._page(self.drives[fid], "permissions", params)
            if fid not in self.files:
                return httpx.Response(404, json={"error": "not found"})
            return self._page(self.files[fid].perms, "permissions", params)
        m = re.search(r"/drive/v3/files/([^/]+)/export$", path)
        if m:
            return httpx.Response(200, text=self.files[m.group(1)].text)
        if path.endswith("/drive/v3/changes/startPageToken"):
            return httpx.Response(200, json={"startPageToken": str(len(self.changes))})
        if path.endswith("/drive/v3/changes"):
            start = int(params["pageToken"])
            out = []
            for ch in self.changes[start:]:
                c = dict(ch)
                if ch["changeType"] == "file" and not ch["removed"] and ch["fileId"] in self.files:
                    c["file"] = self._file_json(ch["fileId"])
                out.append(c)
            return httpx.Response(
                200, json={"changes": out, "newStartPageToken": str(len(self.changes))}
            )
        if path.endswith("/drive/v3/files"):
            items = [self._file_json(fid) for fid, f in self.files.items() if not f.trashed]
            return self._page(items, "files", params)
        m = re.search(r"/drive/v3/files/([^/]+)$", path)
        if m and params.get("alt") == "media":
            return httpx.Response(200, text=self.files[m.group(1)].text)
        return httpx.Response(404, text=json.dumps({"error": f"no route {path}"}))
