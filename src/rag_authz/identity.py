"""Map source principals (GitHub logins, Atlassian account ids) to canonical users (ADR-0002 §4).

Resolution happens at sync time. A principal that cannot be mapped is written under
``~<source>/<id>``, which the retrieval API refuses as a subject: the grant stays visible to
audit but nobody can claim it. Connectors count unmapped principals so recall lost to identity
gaps is a number, not a surprise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from rag_authz.model import UNMAPPED_PREFIX, SubjectRef, user


class IdentityResolver(Protocol):
    def resolve(self, source: str, native_id: str) -> str | None: ...


class MappingResolver:
    """A static mapping, e.g. exported from SCIM or GitHub SAML external identities.

    File format (JSON): ``{"github": {"octocat": "alice@example.com"}, ...}``. Lookups are
    case-insensitive on the native id because GitHub logins and e-mails are.
    """

    def __init__(self, mapping: Mapping[str, Mapping[str, str]]) -> None:
        self._m = {src: {k.lower(): v for k, v in ids.items()} for src, ids in mapping.items()}

    @classmethod
    def from_file(cls, path: Path) -> MappingResolver:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("identity map must be a JSON object keyed by source")
        return cls(data)

    def resolve(self, source: str, native_id: str) -> str | None:
        return self._m.get(source, {}).get(native_id.lower())


class IdentityPassthrough:
    """For sources whose principals already are canonical ids (Google Workspace e-mails)."""

    def resolve(self, source: str, native_id: str) -> str | None:
        return native_id.lower()


class PrincipalMapper:
    """Resolves and counts. One per connector run."""

    def __init__(self, source: str, resolver: IdentityResolver) -> None:
        self.source = source
        self._resolver = resolver
        self.unmapped: set[str] = set()

    def user(self, native_id: str) -> SubjectRef:
        canonical = self._resolver.resolve(self.source, native_id)
        if canonical:
            return user(canonical)
        self.unmapped.add(native_id)
        return user(f"{UNMAPPED_PREFIX}{self.source}/{native_id}")
