"""Confluence Cloud connector: spaces, the content tree, view restrictions and groups.

Mapping (ADR-0002 §6), API shapes checked against Atlassian's OpenAPI specs on 2026-10-28
(docs/research/2026-10-28-confluence-api.md):

- ``container:confluence/<site>/space/<spaceId>``: viewers come from space access, read in
  whichever mode the site is in (``GET /api/v2/space-role-mode``):
  - ``PRE_ROLES``: space permissions with ``operation = read/space``. ``role`` principals are
    skipped (counted) because their members are not listed.
  - ``ROLES``: every role assignment. Atlassian: "View content is required for any role".
  - ``ROLES_TRANSITION``: both, since both apply during the transition.
- Content nodes (pages, folders, whiteboards, databases, embeds) are containers
  ``confluence/<site>/<type>/<id>`` holding the node's **view restriction** (``restricted``
  plus ``restriction_member``), with ``parent`` = the parent node or the space. Each page is
  also a ``doc`` with the same id whose parent is its own container. A restriction therefore
  applies to the page and every descendant, and users must pass every restricted ancestor
  (intersective), exactly as Confluence evaluates it.
- Groups are ``confluence/<site>/group/<groupId>`` (Confluence groups do not nest). Access
  classes: ``anonymous-users`` is ``user:*``. Every other class (``all-licensed-users``,
  ``authenticated-users``, ...) is a group ``confluence/<site>/access/<class>`` with **no
  members** unless ``access_class_groups`` names a Confluence group that stands in for it, so
  an unknown population fails closed.
- Principals are Atlassian account ids. ``identity`` maps them; with ``resolve_emails`` the
  connector also looks up e-mails (scope ``read:email-address:confluence``) and uses them as
  canonical ids. Anything unresolved is written unmapped (``~confluence/<accountId>``).

Change detection: ``changes(since)`` re-reads pages modified since a time (CQL
``lastmodified``). Confluence restriction changes do not update ``lastModified``, deletions are
not searchable, and group changes have no feed, so reconcile is what converges them. That is the
source's floor, and it is documented with the lag numbers.
"""

from __future__ import annotations

import base64
import html.parser
import logging
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from tuplegate.connectors._http import ConnectorError, SourceHttp
from tuplegate.connectors._tree import DepthTracker
from tuplegate.identity import IdentityResolver, PrincipalMapper
from tuplegate.model import (
    DocumentContent,
    GroupMembers,
    NodeAcl,
    ObjectRef,
    SubjectRef,
    SyncEvent,
    UpsertGroup,
    UpsertNode,
    container,
    doc,
    group,
    members,
    public,
)

log = logging.getLogger(__name__)

SOURCE = "confluence"
CONTENT_TYPES = frozenset({"page", "folder", "whiteboard", "database", "embed"})
_ROOT_PARENT_IDS = frozenset({"", "0"})
# Access classes other than anonymous-users, which maps to user:* instead.
ACCESS_CLASSES = frozenset(
    {"authenticated-users", "all-licensed-users", "all-product-admins", "jsm-project-admins"}
)


@dataclass
class ConfluenceStats:
    mode: str = ""
    spaces: int = 0
    pages: int = 0
    other_nodes: int = 0
    restricted_nodes: int = 0
    groups: int = 0
    role_principals_skipped: int = 0
    unmapped_access_classes: int = 0
    unmapped_principals: int = 0
    changed: int = 0
    max_depth: int = 0


class _TextExtractor(html.parser.HTMLParser):
    """Storage format (XHTML) to plain text. Chunking and embedding are the caller's choice."""

    _BLOCK = frozenset({"p", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        # In XHTML a raw newline is whitespace; only block elements break lines.
        self.parts.append(data.replace("\n", " "))


def storage_to_text(storage: str) -> str:
    p = _TextExtractor()
    p.feed(storage)
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


class _AccountResolver:
    def __init__(self, base: IdentityResolver | None, emails: Mapping[str, str]) -> None:
        self._base = base
        self._emails = emails

    def resolve(self, source: str, native_id: str) -> str | None:
        if self._base is not None:
            found = self._base.resolve(source, native_id)
            if found:
                return found
        email = self._emails.get(native_id)
        return email.lower() if email else None


class ConfluenceConnector:
    source = SOURCE

    def __init__(
        self,
        site_url: str,
        *,
        email: str | None = None,
        api_token: str | None = None,
        auth_header: Callable[[], Mapping[str, str]] | None = None,
        identity: IdentityResolver | None = None,
        resolve_emails: bool = True,
        space_keys: Collection[str] | None = None,
        access_class_groups: Mapping[str, str] | None = None,
        include_content: bool = True,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if auth_header is None and email and api_token:
            basic = "Basic " + base64.b64encode(f"{email}:{api_token}".encode()).decode()

            def auth_header() -> Mapping[str, str]:
                return {"Authorization": basic}

        kw: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
        self._base = site_url.rstrip("/")
        self._http = SourceHttp(client or httpx.Client(timeout=60.0), auth_header=auth_header, **kw)
        self.site = urlparse(self._base).hostname or self._base
        self._identity = identity
        self._resolve_emails = resolve_emails
        self._space_keys = frozenset(space_keys) if space_keys is not None else None
        self._access_class_groups = dict(access_class_groups or {})
        self._include_content = include_content
        self._emails: dict[str, str] = {}
        self.stats = ConfluenceStats()
        self._depth: DepthTracker | None = None

    # --- ids -------------------------------------------------------------------------------

    def space_id(self, space_id: str) -> str:
        return f"{SOURCE}/{self.site}/space/{space_id}"

    def node_id(self, type_: str, content_id: str) -> str:
        return f"{SOURCE}/{self.site}/{type_}/{content_id}"

    def group_id(self, gid: str) -> str:
        return f"{SOURCE}/{self.site}/group/{gid}"

    def access_group(self, cls: str) -> str:
        return f"{SOURCE}/{self.site}/access/{cls}"

    # --- paging ----------------------------------------------------------------------------

    def _v2(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        url: str | None = f"{self._base}{path}"
        p: dict[str, Any] | None = {"limit": 250, **(params or {})}
        seen: set[str] = set()
        while url:
            page = self._http.json(url, p)
            yield from page.get("results", [])
            nxt = (page.get("_links") or {}).get("next")
            if not nxt:
                return
            # The next link carries every query parameter, and is relative to the site.
            url = nxt if nxt.startswith("http") else f"{self._base}{nxt}"
            if url in seen:
                raise ConnectorError(f"pagination loop at {url}")
            seen.add(url)
            p = None

    def _v1(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        start, limit = 0, 200
        while True:
            page = self._http.json(
                f"{self._base}{path}", {**(params or {}), "start": start, "limit": limit}
            )
            results = page.get("results", [])
            yield from results
            if len(results) < limit:
                return
            start += len(results)

    # --- principals ------------------------------------------------------------------------

    def _lookup_emails(self, account_ids: Collection[str]) -> None:
        missing = [a for a in account_ids if a not in self._emails]
        for i in range(0, len(missing), 100):
            batch = missing[i : i + 100]
            r = self._http.get(
                f"{self._base}/wiki/rest/api/user/email/bulk", [("accountId", a) for a in batch]
            )
            if r.status_code in (401, 403, 404):
                log.warning("e-mail lookup not permitted; use an identity map")
                self._resolve_emails = False
                return
            if r.status_code >= 400:
                raise ConnectorError(f"email bulk: HTTP {r.status_code}", r.status_code)
            for row in r.json():
                if row.get("email"):
                    self._emails[row["accountId"]] = row["email"]

    def _user(self, mapper: PrincipalMapper, account_id: str) -> SubjectRef:
        if self._resolve_emails and account_id not in self._emails:
            self._lookup_emails([account_id])
        return mapper.user(account_id)

    def _access_class(self, cls: str) -> SubjectRef:
        if cls == "anonymous-users":
            return public()
        if cls not in self._access_class_groups:
            self.stats.unmapped_access_classes += 1
        return members(self.access_group(cls))

    # --- groups ----------------------------------------------------------------------------

    def _groups(self, mapper: PrincipalMapper) -> Iterator[SyncEvent]:
        for g in self._v1("/wiki/rest/api/group"):
            gid = g["id"]
            rows = list(self._v1(f"/wiki/rest/api/group/{gid}/membersByGroupId"))
            for row in rows:
                if row.get("email"):
                    self._emails.setdefault(row["accountId"], row["email"])
            if self._resolve_emails:
                self._lookup_emails([r["accountId"] for r in rows if r.get("accountId")])
            ms = frozenset(mapper.user(r["accountId"]) for r in rows if r.get("accountId"))
            yield UpsertGroup(GroupMembers(group(self.group_id(gid)), ms))
            self.stats.groups += 1
        for cls in sorted(ACCESS_CLASSES | set(self._access_class_groups)):
            stand_in = self._access_class_groups.get(cls)
            ms = frozenset({members(self.group_id(stand_in))}) if stand_in else frozenset()
            yield UpsertGroup(GroupMembers(group(self.access_group(cls)), ms))

    # --- spaces ----------------------------------------------------------------------------

    def _space_viewers(self, space_id: str, mapper: PrincipalMapper) -> frozenset[SubjectRef]:
        out: set[SubjectRef] = set()
        mode = self.stats.mode
        if mode in ("PRE_ROLES", "ROLES_TRANSITION"):
            for p in self._v2(f"/wiki/api/v2/spaces/{space_id}/permissions"):
                op = p.get("operation") or {}
                if (op.get("key"), op.get("targetType")) != ("read", "space"):
                    continue
                pr = p.get("principal") or {}
                match pr.get("type"):
                    case "user" if pr.get("id"):
                        out.add(self._user(mapper, pr["id"]))
                    case "group" if pr.get("id"):
                        out.add(members(self.group_id(pr["id"])))
                    case _:
                        self.stats.role_principals_skipped += 1
        if mode in ("ROLES", "ROLES_TRANSITION"):
            for a in self._v2(f"/wiki/api/v2/spaces/{space_id}/role-assignments"):
                pr = a.get("principal") or {}
                pid = pr.get("principalId")
                if not pid:
                    continue
                match pr.get("principalType"):
                    case "USER":
                        out.add(self._user(mapper, pid))
                    case "GROUP":
                        out.add(members(self.group_id(pid)))
                    case "ACCESS_CLASS":
                        out.add(self._access_class(pid))
        return frozenset(out)

    # --- content ---------------------------------------------------------------------------

    def _restriction(
        self, content_id: str, mapper: PrincipalMapper
    ) -> frozenset[SubjectRef] | None:
        """The node's own view restriction, or None if it has none."""
        out: set[SubjectRef] = set()
        start, limit = 0, 100
        while True:
            r = self._http.json(
                f"{self._base}/wiki/rest/api/content/{content_id}/restriction/byOperation/read",
                {"start": start, "limit": limit},
            )
            # An unexpected shape must not read as "no restriction" (that would fail open).
            try:
                users = r["restrictions"]["user"]["results"]
                groups = r["restrictions"]["group"]["results"]
                user_ids = [u["accountId"] for u in users]
                group_ids = [g["id"] for g in groups]
            except (KeyError, TypeError) as e:
                raise ConnectorError(f"unexpected restriction response for {content_id}") from e
            out.update(self._user(mapper, a) for a in user_ids)
            out.update(members(self.group_id(g)) for g in group_ids)
            if len(users) < limit and len(groups) < limit:
                break
            start += limit
        return frozenset(out) if out else None

    def _parent_ref(self, item: dict[str, Any], space_id: str) -> ObjectRef:
        pid = str(item.get("parentId") or "")
        ptype = item.get("parentType") or "page"
        if pid in _ROOT_PARENT_IDS or ptype not in CONTENT_TYPES:
            return container(self.space_id(space_id))
        return container(self.node_id(ptype, pid))

    def _content_node(
        self, type_: str, item: dict[str, Any], space_id: str, mapper: PrincipalMapper
    ) -> UpsertNode:
        restriction = self._restriction(item["id"], mapper)
        if restriction is not None:
            self.stats.restricted_nodes += 1
        acl = NodeAcl(
            container(self.node_id(type_, item["id"])),
            parent=self._parent_ref(item, space_id),
            restriction=restriction,
        )
        if self._depth is not None:
            self._depth.add(acl.ref.id, acl.parent.id if acl.parent else None)
        return UpsertNode(acl)

    def _page_events(
        self,
        item: dict[str, Any],
        space_id: str,
        mapper: PrincipalMapper,
        changed_at: datetime | None = None,
    ) -> Iterator[SyncEvent]:
        node = self._content_node("page", item, space_id, mapper)
        yield node
        content = None
        if self._include_content:
            storage = ((item.get("body") or {}).get("storage") or {}).get("value", "")
            content = DocumentContent(
                text=storage_to_text(storage),
                version=str((item.get("version") or {}).get("number", "")),
                title=item.get("title", ""),
                url=f"{self._base}/wiki/spaces/{space_id}/pages/{item['id']}",
                metadata={"source": SOURCE, "space_id": space_id},
            )
        yield UpsertNode(
            NodeAcl(doc(self.node_id("page", item["id"])), parent=node.acl.ref),
            content=content,
            changed_at=changed_at,
        )
        self.stats.pages += 1

    def _ancestor_nodes(
        self, items: list[dict[str, Any]], space_id: str, mapper: PrincipalMapper
    ) -> Iterator[SyncEvent]:
        """Non-page parents (folders, whiteboards, ...) carry restrictions too: sync each."""
        pages = {i["id"] for i in items}
        seen: set[tuple[str, str]] = set()
        todo = [
            (i.get("parentType"), str(i.get("parentId") or ""))
            for i in items
            if i.get("parentType") not in (None, "page")
        ]
        while todo:
            ptype, pid = todo.pop()
            if (
                ptype not in CONTENT_TYPES
                or pid in _ROOT_PARENT_IDS
                or (ptype, pid) in seen
                or (ptype == "page" and pid in pages)
            ):
                continue
            seen.add((ptype, pid))
            meta = self._http.json(f"{self._base}/wiki/api/v2/{ptype}s/{pid}")
            yield self._content_node(ptype, meta, space_id, mapper)
            self.stats.other_nodes += 1
            todo.append((meta.get("parentType"), str(meta.get("parentId") or "")))

    def _pages(self, space_id: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"depth": "all", "status": "current"}
        if self._include_content:
            params["body-format"] = "storage"
        return list(self._v2(f"/wiki/api/v2/spaces/{space_id}/pages", params))

    # --- snapshot --------------------------------------------------------------------------

    def _mapper(self) -> PrincipalMapper:
        return PrincipalMapper(SOURCE, _AccountResolver(self._identity, self._emails))

    def snapshot(self) -> Iterator[SyncEvent]:
        """Groups, spaces and the content tree as of now. Raises on any source error."""
        self.stats = ConfluenceStats()
        self._depth = DepthTracker()
        mapper = self._mapper()
        r = self._http.get(f"{self._base}/wiki/api/v2/space-role-mode")
        if r.status_code == 404:  # sites without role-based access
            self.stats.mode = "PRE_ROLES"
        elif r.status_code >= 400:
            raise ConnectorError(f"space-role-mode: HTTP {r.status_code}", r.status_code)
        else:
            self.stats.mode = r.json().get("mode", "PRE_ROLES")
        yield from self._groups(mapper)
        for space in self._v2("/wiki/api/v2/spaces", {"status": "current"}):
            if self._space_keys is not None and space.get("key") not in self._space_keys:
                continue
            sid = space["id"]
            yield UpsertNode(
                NodeAcl(container(self.space_id(sid)), viewers=self._space_viewers(sid, mapper))
            )
            self.stats.spaces += 1
            self._depth.add(self.space_id(sid), None)
            items = self._pages(sid)
            yield from self._ancestor_nodes(items, sid, mapper)
            for item in items:
                yield from self._page_events(item, sid, mapper)
        self.stats.unmapped_principals = len(mapper.unmapped)
        self.stats.max_depth = self._depth.report(SOURCE)
        self._depth = None

    # --- incremental -----------------------------------------------------------------------

    def changes(self, since: datetime, overlap: timedelta = timedelta(days=1)) -> list[SyncEvent]:
        """Pages modified since ``since`` (content and their own restriction).

        CQL dates are interpreted in the API user's time zone, hence the overlap; re-applying
        an unchanged page is a no-op for the engine and skips re-embedding.
        """
        mapper = self._mapper()
        cutoff = (since - overlap).strftime("%Y-%m-%d %H:%M")
        events: list[SyncEvent] = []
        url: str | None = f"{self._base}/wiki/rest/api/search"
        params: dict[str, Any] | None = {
            "cql": f'type = page and lastmodified >= "{cutoff}"',
            "limit": 100,
        }
        ids: list[str] = []
        while url:
            page = self._http.json(url, params)
            ids += [r["content"]["id"] for r in page.get("results", []) if r.get("content")]
            nxt = (page.get("_links") or {}).get("next")
            url = (nxt if nxt.startswith("http") else f"{self._base}{nxt}") if nxt else None
            params = None
        for pid in dict.fromkeys(ids):
            p: dict[str, Any] = {"body-format": "storage"} if self._include_content else {}
            r = self._http.get(f"{self._base}/wiki/api/v2/pages/{pid}", p)
            if r.status_code == 404:
                continue  # deleted since; reconcile's sweep removes it
            if r.status_code >= 400:
                raise ConnectorError(f"page {pid}: HTTP {r.status_code}", r.status_code)
            item = r.json()
            space_id = str(item["spaceId"])
            if self._space_keys is not None and not self._space_in_scope(space_id):
                continue
            when = _parse_time((item.get("version") or {}).get("createdAt"))
            # A page created under, or moved into, a folder we have not synced needs that
            # folder's node too, or the page would hang from a container with no grants.
            events += self._ancestor_nodes([item], space_id, mapper)
            events += self._page_events(item, space_id, mapper, changed_at=when)
            self.stats.changed += 1
        return events

    def _space_in_scope(self, space_id: str) -> bool:
        space = self._http.json(f"{self._base}/wiki/api/v2/spaces/{space_id}")
        return self._space_keys is None or space.get("key") in self._space_keys

    def close(self) -> None:
        self._http.close()


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
