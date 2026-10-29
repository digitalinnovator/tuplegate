"""Settings from ``RAG_AUTHZ_*`` environment variables, shared by the sidecar and the CLI."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from rag_authz.authz.base import AuthzBackend
from rag_authz.index.base import VectorIndex


class Settings(BaseModel):
    """Every setting is an environment variable named ``RAG_AUTHZ_<FIELD>`` (upper case)."""

    callers: str = Field(default="", description="name:sha256hex,... (sidecar only)")
    engine: Literal["openfga", "spicedb"] = "openfga"
    openfga_url: str = "http://127.0.0.1:58080"
    openfga_store: str = "rag-authz"
    openfga_token: str | None = None
    # Must equal the server's OPENFGA_LIST_OBJECTS_DEADLINE: OpenFGA ends the stream silently
    # at its deadline, and this value is how a truncated list is recognised.
    openfga_list_deadline_s: float = 3.0
    spicedb_endpoint: str = "127.0.0.1:55051"
    spicedb_token: str | None = None
    spicedb_insecure: bool = False
    spicedb_list_deadline_s: float = 3.0
    index: Literal["pgvector", "qdrant"] = "pgvector"
    dim: int = Field(default=0, ge=0, description="embedding dimension; required for the index")
    pg_dsn: str = "postgresql://rag:rag-dev-only@127.0.0.1:55432/rag"
    pg_table: str = "rag_chunks"
    qdrant_url: str = "http://127.0.0.1:56333"
    qdrant_collection: str = "rag_chunks"
    qdrant_api_key: str | None = None
    strategy: Literal["pre_filter", "post_filter", "iterative", "cached_pre_filter"] = "pre_filter"
    consistency: Literal["full", "minimize_latency", "at_least_as_fresh"] = "full"
    audit_log: str = "-"
    # Sync (rag-authz sync)
    sync_state: str = "rag-authz-sync.db"
    identity_map: str | None = Field(default=None, description="JSON file, see identity.py")
    github_org: str | None = None
    github_token: str | None = None
    gdrive_drive_ids: str = Field(default="", description="comma-separated shared drive ids")
    gdrive_token: str | None = Field(default=None, description="OAuth access token")
    confluence_url: str | None = None
    confluence_email: str | None = None
    confluence_api_token: str | None = None
    confluence_access_class_groups: str = Field(
        default="", description="class:groupId,... e.g. all-licensed-users:<id>"
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        prefix = "RAG_AUTHZ_"
        values = {
            k[len(prefix) :].lower(): v
            for k, v in env.items()
            if k.startswith(prefix) and k[len(prefix) :].lower() in cls.model_fields
        }
        return cls.model_validate(values)


def build_engine(s: Settings) -> AuthzBackend:
    if s.engine == "openfga":
        from rag_authz.authz.openfga import OpenFGABackend

        b: AuthzBackend = OpenFGABackend(
            s.openfga_url,
            s.openfga_store,
            api_token=s.openfga_token,
            list_deadline_s=s.openfga_list_deadline_s,
        )
    else:
        from rag_authz.authz.spicedb import SpiceDBBackend

        if not s.spicedb_token:
            raise ValueError("RAG_AUTHZ_SPICEDB_TOKEN is required")
        b = SpiceDBBackend(
            s.spicedb_endpoint,
            s.spicedb_token,
            insecure=s.spicedb_insecure,
            list_deadline_s=s.spicedb_list_deadline_s,
        )
    b.ensure_schema()
    return b


def build_index(s: Settings) -> VectorIndex:
    if s.dim <= 0:
        raise ValueError("RAG_AUTHZ_DIM is required")
    if s.index == "pgvector":
        from rag_authz.index.pgvector import PgVectorIndex

        ix: VectorIndex = PgVectorIndex(s.pg_dsn, s.dim, table=s.pg_table)
    else:
        from rag_authz.index.qdrant import QdrantIndex

        ix = QdrantIndex(
            s.qdrant_url, s.dim, collection=s.qdrant_collection, api_key=s.qdrant_api_key
        )
    ix.ensure_schema()
    return ix
