from __future__ import annotations

import hashlib
import hmac

import pytest

from rag_authz import ids
from rag_authz.authz.base import Consistency
from rag_authz.authz.memory import MemoryBackend
from rag_authz.connectors.github import GitHubConnector, verify_signature
from rag_authz.identity import MappingResolver
from rag_authz.index.memory import MemoryIndex
from rag_authz.sync import SyncEngine
from rag_authz.testing import HashingEmbedder
from tests.fakes import IDENTITIES, FakeGitHub, FakeRepo

World = tuple[FakeGitHub, GitHubConnector, SyncEngine, MemoryBackend, MemoryIndex]


@pytest.fixture
def world() -> World:
    gh = FakeGitHub()
    conn = GitHubConnector("acme", MappingResolver(IDENTITIES), client=gh.client())
    backend, index = MemoryBackend(), MemoryIndex()
    sync = SyncEngine(backend, index, embedder=HashingEmbedder(32))
    assert sync.reconcile("github", conn.snapshot()).errors == []
    return gh, conn, sync, backend, index


def _sees(b: MemoryBackend, u: str, path: str) -> bool:
    a = ids.encode(f"github/acme/{path}")
    return b.check_docs(u, [a], Consistency.FULL)[a]


def test_collaborator_removed(world: World) -> None:
    gh, conn, sync, b, _ = world
    assert _sees(b, "alice@acme.test", "payroll/salaries.md")
    gh.repos["payroll"].collaborators.remove("alice")
    evs = conn.webhook_events("member", {"action": "removed", "repository": {"name": "payroll"}})
    rep = sync.apply("github", evs)
    assert rep.errors == []
    assert not _sees(b, "alice@acme.test", "payroll/salaries.md")
    assert len(rep.lag_ms) == len(evs)


def test_team_membership_and_org_removal(world: World) -> None:
    gh, conn, sync, b, _ = world
    assert _sees(b, "carol@acme.test", "api/README.md")
    gh.teams["backend"] = ("engineering", [])
    sync.apply("github", conn.webhook_events("membership", {"action": "removed"}))
    assert not _sees(b, "carol@acme.test", "api/README.md")
    assert _sees(b, "carol@acme.test", "handbook/onboarding.md")
    gh.members.remove("carol")
    sync.apply("github", conn.webhook_events("organization", {"action": "member_removed"}))
    assert not _sees(b, "carol@acme.test", "handbook/onboarding.md")


def test_visibility_change_and_team_repo_access(world: World) -> None:
    gh, conn, sync, b, _ = world
    assert _sees(b, "eve@elsewhere.test", "website/index.md")
    gh.repos["website"].visibility = "private"
    sync.apply(
        "github",
        conn.webhook_events(
            "repository", {"action": "privatized", "repository": {"name": "website"}}
        ),
    )
    assert not _sees(b, "eve@elsewhere.test", "website/index.md")
    gh.repos["payroll"].teams.append("engineering")
    sync.apply(
        "github",
        conn.webhook_events(
            "team", {"action": "added_to_repository", "repository": {"name": "payroll"}}
        ),
    )
    assert _sees(b, "bob@acme.test", "payroll/salaries.md")
    assert _sees(b, "carol@acme.test", "payroll/salaries.md")  # child team inherits


def test_repository_deleted_revokes_every_file_at_once(world: World) -> None:
    gh, conn, sync, b, index = world
    del gh.repos["api"]
    before = index.count()
    sync.apply(
        "github",
        conn.webhook_events("repository", {"action": "deleted", "repository": {"name": "api"}}),
    )
    assert not _sees(b, "carol@acme.test", "api/README.md")
    assert not _sees(b, "olga@acme.test", "api/src/app.py")
    assert index.count() == before  # chunks wait for the sweep, but nobody can retrieve them
    rep = sync.reconcile("github", conn.snapshot())
    assert rep.chunks_deleted == 2


def test_push_updates_and_removes_files(world: World) -> None:
    gh, conn, sync, b, _ = world
    gh.repos["handbook"] = FakeRepo("internal", files={"laptops.md": "Laptop policy."})
    rep = sync.apply(
        "github",
        conn.webhook_events(
            "push",
            {
                "ref": "refs/heads/main",
                "repository": {"name": "handbook", "default_branch": "main"},
                "commits": [{"removed": ["onboarding.md"], "added": ["laptops.md"]}],
            },
        ),
    )
    assert rep.errors == []
    assert rep.chunks_deleted == 1
    assert not _sees(b, "bob@acme.test", "handbook/onboarding.md")
    assert _sees(b, "bob@acme.test", "handbook/laptops.md")


def test_pushes_to_other_branches_are_ignored(world: World) -> None:
    _, conn, _, _, _ = world
    evs = conn.webhook_events(
        "push",
        {"ref": "refs/heads/feature", "repository": {"name": "api", "default_branch": "main"}},
    )
    assert evs == []


def test_signature() -> None:
    body = b'{"action":"removed"}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature(b"s3cret", body, sig)
    assert not verify_signature(b"other", body, sig)
    assert not verify_signature(b"s3cret", body, None)
    assert not verify_signature(b"s3cret", body, "sha1=abc")


def test_transfers_in_and_out(world: World) -> None:
    gh, conn, sync, b, _ = world
    gh.repos["tools"] = FakeRepo("public", files={"README.md": "CLI tools."})
    evs = conn.webhook_events(
        "repository",
        {
            "action": "transferred",
            "repository": {"name": "tools", "owner": {"login": "acme"}},
            "changes": {"owner": {"from": {"user": {"login": "someone"}}}},
        },
    )
    sync.apply("github", evs)
    assert _sees(b, "eve@elsewhere.test", "tools/README.md")
    evs = conn.webhook_events(
        "repository",
        {
            "action": "transferred",
            "repository": {"name": "website", "owner": {"login": "other-org"}},
        },
    )
    sync.apply("github", evs)
    assert not _sees(b, "eve@elsewhere.test", "website/index.md")


def test_signature_with_non_ascii_header_is_false() -> None:
    assert not verify_signature(b"s3cret", b"{}", "sha256=é")
