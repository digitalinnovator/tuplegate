"""Qdrant adapter over its REST API (httpx; no ``qdrant-client`` dependency, ADR-0001).

The ACL filter is pushed into the query as ``must: [{key: acl_id, match: {any: [...]}}]`` on a
keyword payload index. Qdrant's planner picks a payload-index scan for restrictive filters and
filtered HNSW traversal otherwise. ACORN (``search_params.acorn``, Qdrant >= 1.16) trades speed
for recall on restrictive filters; it is off by default and a benchmark arm.

Since Qdrant 1.16 removed JWT payload filters, nothing in Qdrant itself can enforce a per-user
filter: the process holding the API key is the trust boundary (ADR-0001, sidecar mode).

``replace_doc`` sends one ordered batch (delete the doc's points, then upsert the new ones).
Qdrant applies batch operations in order but not atomically, so a concurrent reader can briefly
see the doc with no chunks: lost recall for one request, never stale content.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Sequence
from typing import Any

import httpx

from tuplegate import ids
from tuplegate.index.base import ChunkRecord, Hit

_NAME = re.compile(r"^[a-zA-Z0-9_\-]{1,255}$")
# Stable point ids: Qdrant accepts only unsigned ints or UUIDs.
_NS = uuid.UUID("6f1f3c1e-4b8a-4f0e-9a51-2a7d0c1b5e10")


class QdrantError(RuntimeError):
    pass


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_NS, chunk_id))


class QdrantIndex:
    name = "qdrant"

    def __init__(
        self,
        url: str,
        dim: int,
        *,
        collection: str = "rag_chunks",
        api_key: str | None = None,
        acorn: bool = False,
        hnsw_ef: int | None = None,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not _NAME.match(collection):
            raise ValueError(f"invalid collection name: {collection!r}")
        headers = {"api-key": api_key} if api_key else {}
        self._http = client or httpx.Client(base_url=url, headers=headers, timeout=timeout_s)
        self._dim = dim
        self._c = collection
        self._acorn = acorn
        self._ef = hnsw_ef

    # --- plumbing --------------------------------------------------------------------------

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = self._http.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise QdrantError(f"qdrant {method} {path}: {e}") from e
        if r.status_code >= 400:
            raise QdrantError(f"qdrant {method} {path}: HTTP {r.status_code}: {r.text[:500]}")
        return r.json().get("result")

    def _points(self, suffix: str) -> str:
        return f"/collections/{self._c}/points{suffix}"

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        r = self._http.get(f"/collections/{self._c}/exists")
        if r.status_code >= 400:
            raise QdrantError(f"qdrant collection exists: HTTP {r.status_code}")
        if not r.json()["result"]["exists"]:
            self._req(
                "PUT",
                f"/collections/{self._c}",
                {"vectors": {"size": self._dim, "distance": "Cosine"}},
            )
        for field in ("acl_id", "doc_id"):
            # Idempotent: re-creating an existing index with the same schema is a no-op.
            self._req(
                "PUT",
                f"/collections/{self._c}/index?wait=true",
                {"field_name": field, "field_schema": "keyword"},
            )

    # --- writes ----------------------------------------------------------------------------

    def _point(self, c: ChunkRecord) -> dict[str, Any]:
        return {
            "id": point_id(c.chunk_id),
            "vector": [float(x) for x in c.vector],
            "payload": {
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                # Computed here, never taken from the caller (index/base.py).
                "acl_id": ids.encode(c.doc_id),
                "text": c.text,
                "metadata": dict(c.metadata),
            },
        }

    def upsert(self, chunks: Sequence[ChunkRecord]) -> None:
        if chunks:
            self._req(
                "PUT", self._points("?wait=true"), {"points": [self._point(c) for c in chunks]}
            )

    def replace_doc(self, doc_id: str, chunks: Sequence[ChunkRecord]) -> None:
        if any(c.doc_id != doc_id for c in chunks):
            raise ValueError("replace_doc chunks must all belong to doc_id")
        ops: list[dict[str, Any]] = [
            {"delete": {"filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]}}}
        ]
        if chunks:
            ops.append({"upsert": {"points": [self._point(c) for c in chunks]}})
        self._req("POST", self._points("/batch?wait=true"), {"operations": ops})

    def delete_docs(self, doc_ids: Collection[str]) -> int:
        if not doc_ids:
            return 0
        flt = {"must": [{"key": "doc_id", "match": {"any": list(doc_ids)}}]}
        n = int(self._req("POST", self._points("/count"), {"filter": flt, "exact": True})["count"])
        self._req("POST", self._points("/delete?wait=true"), {"filter": flt})
        return n

    # --- reads -----------------------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        k: int,
        *,
        acl_filter: Collection[str] | None = None,
        offset: int = 0,
    ) -> list[Hit]:
        body: dict[str, Any] = {
            "query": [float(x) for x in vector],
            "limit": k,
            "offset": offset,
            "with_payload": True,
        }
        if acl_filter is not None:
            if not acl_filter:
                return []
            body["filter"] = {"must": [{"key": "acl_id", "match": {"any": list(acl_filter)}}]}
        params: dict[str, Any] = {}
        if self._acorn:
            params["acorn"] = {"enable": True}
        if self._ef is not None:
            params["hnsw_ef"] = self._ef
        if params:
            body["params"] = params
        result = self._req("POST", self._points("/query"), body)
        hits = []
        for p in result.get("points", []):
            pl = p.get("payload") or {}
            hits.append(
                Hit(
                    pl["chunk_id"],
                    pl["doc_id"],
                    pl["acl_id"],
                    pl.get("text", ""),
                    float(p["score"]),
                    pl.get("metadata") or {},
                )
            )
        return hits

    def count(self) -> int:
        return int(self._req("POST", self._points("/count"), {"exact": True})["count"])

    def drop(self) -> None:
        """Delete the collection. For tests and benchmark teardown."""
        r = self._http.delete(f"/collections/{self._c}")
        if r.status_code >= 400 and r.status_code != 404:
            raise QdrantError(f"qdrant drop: HTTP {r.status_code}")

    def close(self) -> None:
        self._http.close()
