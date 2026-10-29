"""Google Drive connector: shared drives, their folders and files, and Directory groups.

Mapping (ADR-0002 §6), verified against Drive API v3 and Admin SDK docs on 2026-09-27
(docs/research/2026-09-27-source-acl-semantics.md):

- ``container:gdrive/<driveId>``: the shared drive. Viewers are its members.
- ``container:gdrive/<folderId>`` / ``doc:gdrive/<fileId>``: parent is the item's parent folder
  (or the drive). Viewers are the item's *direct* grants only; inherited grants come through
  the ``parent`` tuple, so a folder-level revoke is one tuple write.
- **Limited access** (``inheritedPermissionsDisabled``): no ``parent`` tuple. Per Drive, "only
  the owner role, the organizer role, and users with direct folder permissions can access
  it", so inherited ``owner``/``organizer`` grants are kept as direct viewers.
- Grant types: ``user`` → the user; ``group`` → ``gworkspace/group/<email>#member``;
  ``domain`` → ``gworkspace/domain/<domain>#member``; ``anyone`` → ``user:*``.
- **Dropped, deliberately** (fail closed): ``domain``/``anyone`` grants with
  ``allowFileDiscovery: false`` (link-only is not discovery), ``view: "metadata"`` grants
  (metadata is not content), ``view: "published"`` grants, and deleted principals.
- ``expirationTime`` becomes an expiring grant, removed by the sync engine's sweep.
- Directory groups: ``USER``/``EXTERNAL`` members are users, ``GROUP`` members are nested
  groups, ``CUSTOMER`` means everyone in the account (``gworkspace/customer``, whose members
  are the per-domain groups). Domain groups hold every Directory user of that domain.

Incremental sync uses the Changes API per shared drive. Take the start token *before* a
snapshot so changes made during it are replayed. Permission changes on a parent do not emit
entries for descendants; with structure preserved, none are needed. Group-membership changes
have no feed here (the Reports API lags by tens of minutes to hours): reconcile covers them.

Authentication is a callable returning a fresh OAuth access token, so the library does not
depend on ``google-auth``. With a service account and domain-wide delegation::

    creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES,
                                                                  subject=admin_email)
    def token() -> str:
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())
        return creds.token
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from rag_authz.connectors._http import ConnectorError, SourceHttp
from rag_authz.connectors._tree import DepthTracker
from rag_authz.identity import IdentityPassthrough, IdentityResolver, PrincipalMapper
from rag_authz.model import (
    DeleteNode,
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

SOURCE = "gdrive"
SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
)
FOLDER = "application/vnd.google-apps.folder"
# Google-native formats are exported; everything else textual is downloaded as is.
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIME_TYPES = frozenset({"application/json", "application/xml", "application/x-yaml"})
LIMITED_ACCESS_KEEPS = frozenset({"owner", "organizer"})

_FILE_FIELDS = (
    "id,name,mimeType,parents,driveId,trashed,inheritedPermissionsDisabled,"
    "modifiedTime,md5Checksum,size,webViewLink"
)
_PERM_FIELDS = (
    "nextPageToken,permissions(id,type,role,emailAddress,domain,allowFileDiscovery,"
    "expirationTime,view,deleted,permissionDetails(inherited,role))"
)


@dataclass
class DriveStats:
    drives: int = 0
    folders: int = 0
    files: int = 0
    groups: int = 0
    skipped_content: int = 0
    link_only_grants: int = 0
    metadata_only_grants: int = 0
    published_grants: int = 0
    expiring_grants: int = 0
    limited_access_items: int = 0
    unmapped_principals: int = 0
    changes: int = 0
    max_depth: int = 0


@dataclass
class ChangeBatch:
    events: list[SyncEvent]
    next_token: str


def drive_container(drive_id: str) -> str:
    return f"{SOURCE}/{drive_id}"


def item_id(file_id: str) -> str:
    return f"{SOURCE}/{file_id}"


def group_id(email: str) -> str:
    return f"gworkspace/group/{email.lower()}"


def domain_group(domain: str) -> str:
    return f"gworkspace/domain/{domain.lower()}"


CUSTOMER_GROUP = "gworkspace/customer"


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class GoogleDriveConnector:
    source = SOURCE

    def __init__(
        self,
        drive_ids: Collection[str],
        token: Callable[[], str] | None = None,
        *,
        identity: IdentityResolver | None = None,
        sync_directory: bool = True,
        include_content: bool = True,
        max_file_bytes: int = 2_000_000,
        drive_url: str = "https://www.googleapis.com/drive/v3",
        directory_url: str = "https://admin.googleapis.com/admin/directory/v1",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        def auth() -> dict[str, str]:
            return {"Authorization": f"Bearer {token()}"} if token else {}

        kw: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
        self._http = SourceHttp(client or httpx.Client(timeout=60.0), auth_header=auth, **kw)
        self.drive_ids = list(drive_ids)
        self._identity = identity or IdentityPassthrough()
        self._sync_directory = sync_directory
        self._include_content = include_content
        self._max_bytes = max_file_bytes
        self._drive = drive_url.rstrip("/")
        self._dir = directory_url.rstrip("/")
        self.stats = DriveStats()
        self._depth: DepthTracker | None = None

    # --- paging ----------------------------------------------------------------------------

    def _pages(self, url: str, params: dict[str, Any], key: str) -> Iterator[dict[str, Any]]:
        token: str | None = None
        seen: set[str] = set()
        while True:
            p = dict(params)
            if token:
                p["pageToken"] = token
            page = self._http.json(url, p)
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            if token in seen:
                raise ConnectorError(f"pagination loop at {url}")
            seen.add(token)

    # --- directory -------------------------------------------------------------------------

    def _directory(self, mapper: PrincipalMapper) -> Iterator[SyncEvent]:
        domains: dict[str, set[SubjectRef]] = {}
        for u in self._pages(
            f"{self._dir}/users",
            {
                "customer": "my_customer",
                "maxResults": 500,
                "fields": "nextPageToken,users(primaryEmail,suspended)",
            },
            "users",
        ):
            email = u.get("primaryEmail", "")
            if not email or u.get("suspended"):
                continue
            domains.setdefault(email.rsplit("@", 1)[-1].lower(), set()).add(mapper.user(email))
        for d, us in domains.items():
            yield UpsertGroup(GroupMembers(group(domain_group(d)), frozenset(us)))
        yield UpsertGroup(
            GroupMembers(
                group(CUSTOMER_GROUP), frozenset(members(domain_group(d)) for d in domains)
            )
        )
        for g in self._pages(
            f"{self._dir}/groups",
            {"customer": "my_customer", "maxResults": 200, "fields": "nextPageToken,groups(email)"},
            "groups",
        ):
            email = g["email"]
            ms: set[SubjectRef] = set()
            for m in self._pages(
                f"{self._dir}/groups/{email}/members",
                {"maxResults": 200, "fields": "nextPageToken,members(email,type,status)"},
                "members",
            ):
                if m.get("status", "ACTIVE") != "ACTIVE":
                    continue  # suspended or archived: Drive denies them, so do we
                match m.get("type"):
                    case "USER" | "EXTERNAL" if m.get("email"):
                        ms.add(mapper.user(m["email"]))
                    case "GROUP" if m.get("email"):
                        if m["email"].lower() != email.lower():
                            ms.add(members(group_id(m["email"])))
                    case "CUSTOMER":
                        ms.add(members(CUSTOMER_GROUP))
            yield UpsertGroup(GroupMembers(group(group_id(email)), frozenset(ms)))
            self.stats.groups += 1

    # --- permissions -----------------------------------------------------------------------

    def _grants(
        self, file_id: str, mapper: PrincipalMapper, limited: bool
    ) -> tuple[frozenset[SubjectRef], frozenset[tuple[SubjectRef, datetime]]]:
        viewers: set[SubjectRef] = set()
        expiring: set[tuple[SubjectRef, datetime]] = set()
        for p in self._pages(
            f"{self._drive}/files/{file_id}/permissions",
            {"supportsAllDrives": "true", "pageSize": 100, "fields": _PERM_FIELDS},
            "permissions",
        ):
            if p.get("deleted"):
                continue
            details = p.get("permissionDetails")
            # No details (My Drive items without inheritance info): treat as direct.
            direct = not details or any(not d.get("inherited", False) for d in details)
            roles = {p.get("role", "")} | {d.get("role", "") for d in details or []}
            if not direct and not (limited and roles & LIMITED_ACCESS_KEEPS):
                continue  # arrives through the parent tuple
            view = p.get("view")
            if view == "metadata":
                self.stats.metadata_only_grants += 1
                continue
            if view == "published":
                self.stats.published_grants += 1
                continue
            subject = self._subject(p, mapper)
            if subject is None:
                continue
            exp = _parse_time(p.get("expirationTime"))
            if exp is not None:
                expiring.add((subject, exp))
                self.stats.expiring_grants += 1
            else:
                viewers.add(subject)
        return frozenset(viewers), frozenset(expiring)

    def _subject(self, p: dict[str, Any], mapper: PrincipalMapper) -> SubjectRef | None:
        match p.get("type"):
            case "user" if p.get("emailAddress"):
                return mapper.user(p["emailAddress"])
            case "group" if p.get("emailAddress"):
                return members(group_id(p["emailAddress"]))
            case "domain" | "anyone" as t:
                if not p.get("allowFileDiscovery", False):
                    self.stats.link_only_grants += 1
                    return None
                if t == "anyone":
                    return public()
                return members(domain_group(p["domain"])) if p.get("domain") else None
        return None

    # --- items -----------------------------------------------------------------------------

    def _item_event(
        self, f: dict[str, Any], mapper: PrincipalMapper, changed_at: datetime | None = None
    ) -> UpsertNode:
        fid = f["id"]
        limited = bool(f.get("inheritedPermissionsDisabled"))
        if limited:
            self.stats.limited_access_items += 1
        parents = f.get("parents") or []
        parent: ObjectRef | None = None
        if parents and not limited:
            pid = parents[0]
            parent = container(drive_container(pid) if pid == f.get("driveId") else item_id(pid))
        viewers, expiring = self._grants(fid, mapper, limited)
        is_folder = f.get("mimeType") == FOLDER
        if is_folder and self._depth is not None:
            self._depth.add(item_id(fid), parent.id if parent is not None else None)
        ref = container(item_id(fid)) if is_folder else doc(item_id(fid))
        acl = NodeAcl(ref, parent=parent, viewers=viewers, expiring=expiring)
        content = None if is_folder else self._content(f)
        if is_folder:
            self.stats.folders += 1
        else:
            self.stats.files += 1
        return UpsertNode(acl, content=content, changed_at=changed_at)

    def _content(self, f: dict[str, Any]) -> DocumentContent | None:
        if not self._include_content:
            return None
        mime = f.get("mimeType", "")
        fid = f["id"]
        if mime in EXPORTS:
            r = self._http.get(f"{self._drive}/files/{fid}/export", {"mimeType": EXPORTS[mime]})
            version = f.get("modifiedTime") or ""
        elif mime.startswith(TEXT_MIME_PREFIXES) or mime in TEXT_MIME_TYPES:
            if int(f.get("size") or 0) > self._max_bytes:
                self.stats.skipped_content += 1
                return None
            r = self._http.get(
                f"{self._drive}/files/{fid}", {"alt": "media", "supportsAllDrives": "true"}
            )
            version = f.get("md5Checksum") or f.get("modifiedTime") or ""
        else:
            self.stats.skipped_content += 1
            return None
        if r.status_code >= 400:
            # Export limits (10 MB) and permission quirks: keep the ACL, skip the text.
            log.warning("content unavailable", extra={"file": fid, "status": r.status_code})
            self.stats.skipped_content += 1
            return None
        return DocumentContent(
            text=r.text[: self._max_bytes],
            version=version,
            title=f.get("name", ""),
            url=f.get("webViewLink", ""),
            metadata={"source": SOURCE, "mime_type": mime},
        )

    # --- snapshot --------------------------------------------------------------------------

    def snapshot(self) -> Iterator[SyncEvent]:
        """Every group, drive, folder and file as of now. Raises on any source error."""
        mapper = PrincipalMapper(SOURCE, self._identity)
        self.stats = DriveStats()
        self._depth = DepthTracker()
        if self._sync_directory:
            yield from self._directory(mapper)
        for drive_id in self.drive_ids:
            viewers, expiring = self._grants(drive_id, mapper, limited=False)
            yield UpsertNode(
                NodeAcl(container(drive_container(drive_id)), viewers=viewers, expiring=expiring)
            )
            self.stats.drives += 1
            self._depth.add(drive_container(drive_id), None)
            for f in self._pages(
                f"{self._drive}/files",
                {
                    "corpora": "drive",
                    "driveId": drive_id,
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                    "q": "trashed = false",
                    "pageSize": 1000,
                    "fields": f"nextPageToken,files({_FILE_FIELDS})",
                },
                "files",
            ):
                yield self._item_event(f, mapper)
        self.stats.unmapped_principals = len(mapper.unmapped)
        self.stats.max_depth = self._depth.report(SOURCE)
        self._depth = None

    # --- incremental -----------------------------------------------------------------------

    def start_tokens(self) -> dict[str, str]:
        """Change-feed positions per drive. Take them before ``snapshot``."""
        return {
            d: self._http.json(
                f"{self._drive}/changes/startPageToken",
                {"driveId": d, "supportsAllDrives": "true"},
            )["startPageToken"]
            for d in self.drive_ids
        }

    def changes(self, drive_id: str, token: str) -> ChangeBatch:
        """Events since ``token`` for one shared drive, and the token to resume from.

        A change whose item can no longer be read (404) is reported as a deletion: if the
        connector's identity lost access, so must the index. Any other error raises, so the
        caller keeps ``token`` and retries the whole batch.
        """
        mapper = PrincipalMapper(SOURCE, self._identity)
        events: list[SyncEvent] = []
        page_token = token
        while True:
            page = self._http.json(
                f"{self._drive}/changes",
                {
                    "pageToken": page_token,
                    "driveId": drive_id,
                    "includeItemsFromAllDrives": "true",
                    "supportsAllDrives": "true",
                    "pageSize": 1000,
                    "fields": "nextPageToken,newStartPageToken,"
                    f"changes(changeType,fileId,removed,time,file({_FILE_FIELDS}))",
                },
            )
            for ch in page.get("changes", []):
                self.stats.changes += 1
                when = _parse_time(ch.get("time"))
                if ch.get("changeType", "file") == "drive":
                    # Membership changes on the shared drive itself: the most common revoke.
                    # Re-read its members; every item below inherits through parent tuples.
                    viewers, expiring = self._grants(drive_id, mapper, limited=False)
                    events.append(
                        UpsertNode(
                            NodeAcl(
                                container(drive_container(drive_id)),
                                viewers=viewers,
                                expiring=expiring,
                            ),
                            changed_at=when,
                        )
                    )
                    continue
                fid = ch["fileId"]
                f = ch.get("file")
                if not (ch.get("removed") or f is None or f.get("trashed")):
                    try:
                        events.append(self._item_event(f, mapper, changed_at=when))
                        continue
                    except ConnectorError as e:
                        if e.status != 404:
                            raise
                # The type of a removed item is unknown; deleting both is harmless.
                events.append(DeleteNode(doc(item_id(fid)), changed_at=when))
                events.append(DeleteNode(container(item_id(fid)), changed_at=when))
            if page.get("newStartPageToken"):
                self.stats.unmapped_principals = len(mapper.unmapped)
                return ChangeBatch(events, page["newStartPageToken"])
            page_token = page["nextPageToken"]

    def close(self) -> None:
        self._http.close()
