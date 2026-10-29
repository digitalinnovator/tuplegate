"""Fixtures for tests against the Compose services (`make up`).

Each test gets its own OpenFGA store and pgvector table, deleted afterwards, so tests can run
against a shared dev stack without interfering.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest

from tuplegate.authz.openfga import OpenFGABackend
from tuplegate.authz.spicedb import SpiceDBBackend
from tuplegate.index.pgvector import PgVectorIndex
from tuplegate.index.qdrant import QdrantIndex

OPENFGA_URL = os.environ.get("TUPLEGATE_OPENFGA_URL", "http://127.0.0.1:58080")
SPICEDB_ENDPOINT = os.environ.get("TUPLEGATE_SPICEDB_ENDPOINT", "127.0.0.1:55051")
SPICEDB_TOKEN = os.environ.get("TUPLEGATE_SPICEDB_TOKEN", "rag-dev-only")
QDRANT_URL = os.environ.get("TUPLEGATE_QDRANT_URL", "http://127.0.0.1:56333")
PG_DSN = os.environ.get("TUPLEGATE_PG_DSN", "postgresql://rag:rag-dev-only@127.0.0.1:55432/rag")


@pytest.fixture
def openfga() -> Iterator[OpenFGABackend]:
    b = OpenFGABackend(OPENFGA_URL, store_name=f"test-{uuid.uuid4().hex[:12]}")
    b.ensure_schema()
    try:
        yield b
    finally:
        b.delete_store()
        b.close()


@pytest.fixture
def spicedb() -> Iterator[SpiceDBBackend]:
    # SpiceDB has no stores: tests share one instance, so each test starts and ends empty.
    # Run SpiceDB tests serially (the default).
    b = SpiceDBBackend(SPICEDB_ENDPOINT, SPICEDB_TOKEN, insecure=True)
    b.ensure_schema()
    b.delete_all()
    try:
        yield b
    finally:
        b.delete_all()
        b.close()


@pytest.fixture
def pgindex() -> Iterator[Callable[..., PgVectorIndex]]:
    made: list[PgVectorIndex] = []

    def make(dim: int, **kw: object) -> PgVectorIndex:
        ix = PgVectorIndex(PG_DSN, dim, table=f"t_{uuid.uuid4().hex[:12]}", **kw)  # type: ignore[arg-type]
        ix.ensure_schema()
        made.append(ix)
        return ix

    try:
        yield make
    finally:
        for ix in made:
            ix.drop()
            ix.close()


@pytest.fixture
def qdindex() -> Iterator[Callable[..., QdrantIndex]]:
    made: list[QdrantIndex] = []

    def make(dim: int, **kw: object) -> QdrantIndex:
        ix = QdrantIndex(QDRANT_URL, dim, collection=f"t_{uuid.uuid4().hex[:12]}", **kw)  # type: ignore[arg-type]
        ix.ensure_schema()
        made.append(ix)
        return ix

    try:
        yield make
    finally:
        for ix in made:
            ix.drop()
            ix.close()
