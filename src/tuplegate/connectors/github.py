"""GitHub connector: org, teams, repositories and repository files (ADR-0002 §6).

Mapping, verified against GitHub docs on 2026-09-27
(docs/research/2026-09-27-source-acl-semantics.md):

- ``container:github/<org>``: viewers are the org owners, always, plus all org members when
  the base permission is at least ``read``. A base-permission change is one tuple.
- ``container:github/<org>/<repo>``: parent is the org container. Public adds ``user:*``;
  internal adds org members (read minimum even when the base permission is ``none``). Direct
  and outside collaborators and teams with access are viewers. Every GitHub role includes read.
- ``group:github/<org>/team/<slug>``: immediate members (the REST endpoint includes child-team
  members, flagged ``inherited``; those are dropped) plus ``child#member`` for each child team,
  because child teams inherit their parent's access.
- ``doc:github/<org>/<repo>/<path>``: parent is the repo. GitHub has no per-file ACLs.

Limitations (v0.1): enterprise-wide visibility of internal repos is not modelled (it
under-grants); change detection is full reconciliation (webhooks and ETag polling are on the
roadmap); a truncated git tree is reported, not paged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import httpx

from tuplegate.connectors._http import ConnectorError
from tuplegate.identity import IdentityResolver, PrincipalMapper
from tuplegate.model import (
    DeleteGroup,
    DeleteNode,
    DocumentContent,
    GroupMembers,
    NodeAcl,
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

__all__ = ["ConnectorError", "GitHubConnector"]

SOURCE = "github"
READ_BASE_PERMISSIONS = frozenset({"read", "triage", "write", "maintain", "admin"})
DEFAULT_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".mdx",
        ".rst",
        ".txt",
        ".py",
        ".ts",
        ".js",
        ".go",
        ".java",
        ".rs",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
    }
)


@dataclass
class GitHubStats:
    repos: int = 0
    teams: int = 0
    files: int = 0
    skipped_files: int = 0
    truncated_trees: list[str] = field(default_factory=list)
    unmapped_principals: int = 0
    base_permission: str | None = None


def org_container(org: str) -> str:
    return f"{SOURCE}/{org}"


def repo_container(org: str, repo: str) -> str:
    return f"{SOURCE}/{org}/{repo}"


def members_group(org: str) -> str:
    return f"{SOURCE}/{org}/members"


def owners_group(org: str) -> str:
    return f"{SOURCE}/{org}/owners"


def team_group(org: str, slug: str) -> str:
    return f"{SOURCE}/{org}/team/{slug}"


def file_doc(org: str, repo: str, path: str) -> str:
    return f"{SOURCE}/{org}/{repo}/{path}"


class GitHubConnector:
    source = SOURCE

    def __init__(
        self,
        org: str,
        identity: IdentityResolver,
        *,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
        include_content: bool = True,
        repos: Collection[str] | None = None,
        max_file_bytes: int = 200_000,
        text_suffixes: Collection[str] = DEFAULT_TEXT_SUFFIXES,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = client or httpx.Client(base_url=api_url, headers=headers, timeout=30.0)
        self.org = org
        self._identity = identity
        self._include_content = include_content
        self._repos = frozenset(repos) if repos is not None else None
        self._max_bytes = max_file_bytes
        self._suffixes = frozenset(text_suffixes)
        self.stats = GitHubStats()

    # --- HTTP ------------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            r = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise ConnectorError(f"GET {path}: {e}") from e
        return r

    def _json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._get(path, params)
        if r.status_code >= 400:
            raise ConnectorError(f"GET {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        # The "next" link already carries every query parameter. Pass params=None when
        # following it: httpx replaces a URL's query string with any params mapping, even {}.
        p: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        url: str | None = path
        seen: set[str] = set()
        while url:
            if url in seen:
                raise ConnectorError(f"pagination loop at {url}")
            seen.add(url)
            r = self._get(url, p)
            if r.status_code >= 400:
                raise ConnectorError(f"GET {url}: HTTP {r.status_code}: {r.text[:300]}")
            yield from r.json()
            url = r.links.get("next", {}).get("url")
            p = None

    # --- snapshot --------------------------------------------------------------------------

    def snapshot(self) -> Iterator[SyncEvent]:
        """Every group, container and doc as of now. Raises on any source error, so the sync
        engine never sweeps on a partial snapshot."""
        mapper = PrincipalMapper(SOURCE, self._identity)
        self.stats = GitHubStats()
        yield from self._org_groups(mapper)
        yield from self._teams(mapper)
        yield self._org_node()
        for repo in self._paginate(f"/orgs/{self.org}/repos", {"type": "all"}):
            if self._repos is not None and repo["name"] not in self._repos:
                continue
            yield from self._repo(repo, mapper)
            self.stats.repos += 1
        self.stats.unmapped_principals = len(mapper.unmapped)

    def _org_groups(self, mapper: PrincipalMapper) -> Iterator[SyncEvent]:
        org = self.org
        owners = {
            mapper.user(m["login"])
            for m in self._paginate(f"/orgs/{org}/members", {"role": "admin"})
        }
        org_members = {mapper.user(m["login"]) for m in self._paginate(f"/orgs/{org}/members")}
        yield UpsertGroup(GroupMembers(group(owners_group(org)), frozenset(owners)))
        yield UpsertGroup(GroupMembers(group(members_group(org)), frozenset(org_members)))

    def _teams(self, mapper: PrincipalMapper) -> Iterator[SyncEvent]:
        org = self.org
        teams = list(self._paginate(f"/orgs/{org}/teams"))
        children: dict[str, list[str]] = {}
        for t in teams:
            parent = t.get("parent")
            if parent:
                children.setdefault(parent["slug"], []).append(t["slug"])
        for t in teams:
            slug = t["slug"]
            ms: set[SubjectRef] = set()
            for m in self._paginate(f"/orgs/{org}/teams/{slug}/members"):
                if m.get("inherited") is True:
                    continue  # comes from a child team, which is modelled as a nested group
                ms.add(mapper.user(m["login"]))
            ms.update(members(team_group(org, c)) for c in children.get(slug, []))
            yield UpsertGroup(GroupMembers(group(team_group(org, slug)), frozenset(ms)))
            self.stats.teams += 1

    def _org_node(self) -> UpsertNode:
        org = self.org
        info = self._json(f"/orgs/{org}")
        # Only org owners (or admin:org tokens) see default_repository_permission. Missing
        # means unknown, which fails closed: members get no base access.
        base = info.get("default_repository_permission")
        self.stats.base_permission = base
        if base is None:
            log.warning(
                "base permission not visible to this token; assuming none", extra={"org": org}
            )
        org_viewers: set[SubjectRef] = {members(owners_group(org))}
        if base in READ_BASE_PERMISSIONS:
            org_viewers.add(members(members_group(org)))
        return UpsertNode(NodeAcl(container(org_container(org)), viewers=frozenset(org_viewers)))

    # --- webhooks --------------------------------------------------------------------------

    def webhook_events(self, event: str, payload: dict[str, Any]) -> list[SyncEvent]:
        """Translate one webhook delivery into the sync events that bring us up to date.

        Each delivery re-reads the affected objects from the API rather than trusting the
        payload, so out-of-order or replayed deliveries converge on current state. Base
        permission changes have no webhook: reconcile covers them.
        """
        mapper = PrincipalMapper(SOURCE, self._identity)
        repo = payload.get("repository") or {}
        name: str | None = repo.get("name")
        if name is not None and self._repos is not None and name not in self._repos:
            return []
        now = datetime.now(UTC)
        out: list[SyncEvent] = []
        match event, payload.get("action"):
            case ("member" | "team_add" | "public", _) if name:
                out += self._repo_events(name, mapper, content=False)
            case ("team", "added_to_repository" | "removed_from_repository") if name:
                out += self._repo_events(name, mapper, content=False)
            case ("team", "deleted"):
                slug = (payload.get("team") or {}).get("slug")
                if slug:
                    out.append(DeleteGroup(group(team_group(self.org, slug)), changed_at=now))
                out += self._teams(mapper)  # a deleted parent changes its children's nesting
            case ("team" | "membership", _):
                out += self._teams(mapper)
            case ("organization", "member_added" | "member_removed"):
                out += self._org_groups(mapper)
                out += self._teams(mapper)  # removal from the org removes team memberships
            case ("repository", "deleted"):
                # Tuples go first; the repo's files lose their only grant path at once, and
                # their chunks are purged by the next reconcile's sweep.
                out.append(DeleteNode(container(repo_container(self.org, name or "")), now))
            case ("repository", "renamed"):
                old = ((payload.get("changes") or {}).get("repository") or {}).get("name", {})
                if old.get("from"):
                    out.append(DeleteNode(container(repo_container(self.org, old["from"])), now))
                if name:
                    out += self._repo_events(name, mapper, content=self._include_content)
            case ("repository", "transferred") if name:
                owner = ((repo.get("owner") or {}).get("login") or "").lower()
                if owner == self.org.lower():  # transferred in
                    out += self._repo_events(name, mapper, content=self._include_content)
                else:  # transferred out: our copy of its ACL is no longer the source's
                    out.append(DeleteNode(container(repo_container(self.org, name)), now))
            case ("repository", _) if name:
                out += self._repo_events(name, mapper, content=self._include_content)
            case ("push", _) if (
                name and payload.get("ref") == f"refs/heads/{repo.get('default_branch')}"
            ):
                removed = {
                    path for c in payload.get("commits", []) for path in c.get("removed", [])
                }
                out += [DeleteNode(doc(file_doc(self.org, name, p)), now) for p in sorted(removed)]
                out += self._repo_events(name, mapper, content=self._include_content)
        return [_stamp(ev, now) for ev in out]

    def _repo_events(self, name: str, mapper: PrincipalMapper, *, content: bool) -> list[SyncEvent]:
        r = self._get(f"/repos/{self.org}/{name}")
        if r.status_code == 404:
            return [DeleteNode(container(repo_container(self.org, name)))]
        if r.status_code >= 400:
            raise ConnectorError(f"GET repo {name}: HTTP {r.status_code}", r.status_code)
        saved = self._include_content
        self._include_content = content
        try:
            return list(self._repo(r.json(), mapper))
        finally:
            self._include_content = saved

    def _repo(self, repo: dict[str, Any], mapper: PrincipalMapper) -> Iterator[SyncEvent]:
        org, name = self.org, repo["name"]
        viewers: set[SubjectRef] = set()
        visibility = repo.get("visibility") or ("private" if repo.get("private") else "public")
        if visibility == "public":
            viewers.add(public())
        elif visibility == "internal":
            viewers.add(members(members_group(org)))
        for c in self._paginate(f"/repos/{org}/{name}/collaborators", {"affiliation": "direct"}):
            viewers.add(mapper.user(c["login"]))
        for t in self._paginate(f"/repos/{org}/{name}/teams"):
            viewers.add(members(team_group(org, t["slug"])))
        repo_ref = container(repo_container(org, name))
        yield UpsertNode(
            NodeAcl(repo_ref, parent=container(org_container(org)), viewers=frozenset(viewers))
        )
        if not self._include_content:
            return
        branch = repo.get("default_branch") or "main"
        r = self._get(f"/repos/{org}/{name}/git/trees/{branch}", {"recursive": "1"})
        if r.status_code in (404, 409):  # empty repository
            return
        if r.status_code >= 400:
            raise ConnectorError(f"tree {org}/{name}: HTTP {r.status_code}")
        tree = r.json()
        if tree.get("truncated"):
            self.stats.truncated_trees.append(name)
            log.warning("git tree truncated; some files are not synced", extra={"repo": name})
        for entry in tree.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = entry["path"]
            if (
                PurePosixPath(path).suffix.lower() not in self._suffixes
                or entry.get("size", 0) > self._max_bytes
            ):
                self.stats.skipped_files += 1
                continue
            blob = self._json(f"/repos/{org}/{name}/git/blobs/{entry['sha']}")
            try:
                text = base64.b64decode(blob.get("content", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self.stats.skipped_files += 1
                continue
            yield UpsertNode(
                NodeAcl(doc(file_doc(org, name, path)), parent=repo_ref),
                content=DocumentContent(
                    text=text,
                    version=entry["sha"],
                    title=f"{name}/{path}",
                    url=f"{repo.get('html_url', '')}/blob/{branch}/{path}",
                    metadata={"source": SOURCE, "repo": name, "path": path},
                ),
            )
            self.stats.files += 1

    def close(self) -> None:
        self._http.close()


def _stamp(ev: SyncEvent, when: datetime) -> SyncEvent:
    """Give webhook-driven events a source time so the sync engine records their lag."""
    if ev.changed_at is not None:
        return ev
    return replace(ev, changed_at=when)


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Check ``X-Hub-Signature-256``. ``webhook_events`` re-reads state from the API, so a
    forged delivery cannot change an ACL, but it can spend the connector's rate limit."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode()
    # Compare bytes: compare_digest raises on non-ASCII str, and a forged header must get False.
    return hmac.compare_digest(expected, header.removeprefix("sha256=").encode())
