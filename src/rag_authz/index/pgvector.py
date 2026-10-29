"""pgvector adapter (pgvector >= 0.8 for iterative index scans).

The ACL filter is pushed into SQL as ``acl_id = ANY(:ids)``. With ``iterative_scan`` on, the
HNSW scan keeps expanding until ``LIMIT`` rows pass the filter (bounded by
``hnsw.max_scan_tuples``). With it off, HNSW returns at most ``ef_search`` candidates before
the filter, which is where filtered recall collapses. Both are benchmark arms.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from typing import Any, Literal

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import sql

from rag_authz import ids
from rag_authz.index.base import ChunkRecord, Hit

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
IterativeScan = Literal["off", "relaxed_order", "strict_order"]


class PgVectorIndex:
    name = "pgvector"

    def __init__(
        self,
        dsn: str,
        dim: int,
        *,
        table: str = "rag_chunks",
        iterative_scan: IterativeScan = "relaxed_order",
        ef_search: int = 40,
        max_scan_tuples: int = 20_000,
    ) -> None:
        if not _IDENT.match(table):
            raise ValueError(f"invalid table name: {table!r}")
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._dim = dim
        self._t = sql.Identifier(table)
        self._table = table
        self._iterative = iterative_scan
        self._ef = ef_search
        self._max_scan = max_scan_tuples

    def ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                sql.SQL(
                    """CREATE TABLE IF NOT EXISTS {t} (
                        chunk_id  text PRIMARY KEY,
                        doc_id    text NOT NULL,
                        acl_id    text NOT NULL,
                        content   text NOT NULL,
                        metadata  jsonb NOT NULL DEFAULT '{{}}',
                        embedding vector({dim}) NOT NULL
                    )"""
                ).format(t=self._t, dim=sql.Literal(self._dim))
            )
            for suffix, cols in (("acl", "acl_id"), ("doc", "doc_id")):
                cur.execute(
                    sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} ({c})").format(
                        i=sql.Identifier(f"{self._table}_{suffix}_idx"),
                        t=self._t,
                        c=sql.Identifier(cols),
                    )
                )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {i} ON {t} USING hnsw (embedding vector_cosine_ops)"
                ).format(i=sql.Identifier(f"{self._table}_hnsw_idx"), t=self._t)
            )
        register_vector(self._conn)
        self._apply_settings()

    def _apply_settings(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL("SET hnsw.iterative_scan = {}").format(sql.Literal(self._iterative))
            )
            cur.execute(sql.SQL("SET hnsw.ef_search = {}").format(sql.Literal(self._ef)))
            cur.execute(
                sql.SQL("SET hnsw.max_scan_tuples = {}").format(sql.Literal(self._max_scan))
            )

    def _rows(self, chunks: Sequence[ChunkRecord]) -> list[tuple[Any, ...]]:
        return [
            (
                c.chunk_id,
                c.doc_id,
                ids.encode(c.doc_id),
                c.text,
                json.dumps(dict(c.metadata)),
                Vector([float(x) for x in c.vector]),
            )
            for c in chunks
        ]

    _UPSERT = """INSERT INTO {t} (chunk_id, doc_id, acl_id, content, metadata, embedding)
                 VALUES (%s, %s, %s, %s, %s, %s)
                 ON CONFLICT (chunk_id) DO UPDATE SET doc_id = EXCLUDED.doc_id,
                   acl_id = EXCLUDED.acl_id, content = EXCLUDED.content,
                   metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding"""

    def upsert(self, chunks: Sequence[ChunkRecord]) -> None:
        if not chunks:
            return
        with self._conn.cursor() as cur:
            cur.executemany(sql.SQL(self._UPSERT).format(t=self._t), self._rows(chunks))

    def replace_doc(self, doc_id: str, chunks: Sequence[ChunkRecord]) -> None:
        if any(c.doc_id != doc_id for c in chunks):
            raise ValueError("replace_doc chunks must all belong to doc_id")
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {t} WHERE doc_id = %s").format(t=self._t), (doc_id,))
            if chunks:
                cur.executemany(sql.SQL(self._UPSERT).format(t=self._t), self._rows(chunks))

    def delete_docs(self, doc_ids: Collection[str]) -> int:
        if not doc_ids:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {t} WHERE doc_id = ANY(%s)").format(t=self._t),
                (list(doc_ids),),
            )
            return cur.rowcount

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        acl_filter: Collection[str] | None = None,
        offset: int = 0,
    ) -> list[Hit]:
        q = Vector([float(x) for x in vector])
        where = sql.SQL("")
        params: list[Any] = [q]
        if acl_filter is not None:
            if not acl_filter:
                return []
            where = sql.SQL("WHERE acl_id = ANY(%s)")
            params.append(list(acl_filter))
        params += [q, k, offset]
        query = sql.SQL(
            """SELECT chunk_id, doc_id, acl_id, content, metadata, 1 - (embedding <=> %s) AS score
               FROM {t} {where}
               ORDER BY embedding <=> %s
               LIMIT %s OFFSET %s"""
        ).format(t=self._t, where=where)
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        hits = [Hit(r[0], r[1], r[2], r[3], float(r[5]), r[4] or {}) for r in rows]
        # relaxed_order may return rows slightly out of order; restore it.
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=self._t))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def drop(self) -> None:
        """Drop the table. For tests and benchmark teardown."""
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {t}").format(t=self._t))

    def truncate(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql.SQL("TRUNCATE {t}").format(t=self._t))

    def close(self) -> None:
        self._conn.close()
