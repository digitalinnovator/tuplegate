"""OpenFGA backend over its HTTP API (ADR-0001: httpx, not the aiohttp-based SDK).

Verified against OpenFGA v1.21.0 (docs/research/2026-09-27-engines-and-vector-stores.md):

- ``write`` is transactional, max 100 tuples per request by default.
- ``batch-check`` takes at most 50 checks per request by default.
- ``streamed-list-objects`` has no result cap but stops silently at the server deadline
  (default 3s), so a list that ran close to the deadline is reported as incomplete.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Collection, Iterator, Sequence
from typing import Any

import httpx

from rag_authz import ids
from rag_authz.authz.base import (
    AuthzError,
    Consistency,
    ListResult,
    safe_batches,
    validate_subject,
)
from rag_authz.model import CAN_VIEW, ObjectRef, ObjectType, Relation, SubjectRef, Tuple
from rag_authz.schema import openfga_model

log = logging.getLogger(__name__)

_CONSISTENCY = {
    Consistency.FULL: "HIGHER_CONSISTENCY",
    Consistency.MINIMIZE_LATENCY: "MINIMIZE_LATENCY",
    # OpenFGA has no write tokens; the only way to see our own writes is to skip caches.
    Consistency.AT_LEAST_AS_FRESH: "HIGHER_CONSISTENCY",
}


def _obj(ref: ObjectRef) -> str:
    return f"{ref.type}:{ids.encode(ref.id)}"


def _subject(s: SubjectRef) -> str:
    base = f"{s.type}:{ids.encode(s.id)}"
    return f"{base}#{s.relation}" if s.relation else base


def to_key(t: Tuple) -> dict[str, str]:
    if t.relation is Relation.PARENT:
        assert t.parent_ref is not None
        user = _obj(t.parent_ref)
    else:
        assert t.subject is not None
        user = _subject(t.subject)
    return {"user": user, "relation": str(t.relation), "object": _obj(t.object)}


def _parse_ref(s: str) -> ObjectRef:
    type_, _, id_ = s.partition(":")
    return ObjectRef(ObjectType(type_), ids.decode(id_))


def from_key(key: dict[str, str]) -> Tuple:
    obj = _parse_ref(key["object"])
    rel = Relation(key["relation"])
    if rel is Relation.PARENT:
        return Tuple(obj, rel, parent_ref=_parse_ref(key["user"]))
    base, _, sub_rel = key["user"].partition("#")
    type_, _, id_ = base.partition(":")
    subject = SubjectRef(ObjectType(type_), ids.decode(id_), sub_rel or None)
    return Tuple(obj, rel, subject)


class OpenFGABackend:
    name = "openfga"

    def __init__(
        self,
        api_url: str,
        store_name: str = "rag-authz",
        *,
        api_token: str | None = None,
        max_tuples_per_write: int = 100,
        max_checks_per_batch: int = 50,
        list_deadline_s: float = 3.0,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
        self._http = client or httpx.Client(base_url=api_url, headers=headers, timeout=timeout_s)
        self._store_name = store_name
        self._max_write = max_tuples_per_write
        self._max_checks = max_checks_per_batch
        self._list_deadline_s = list_deadline_s
        self.store_id: str | None = None
        self.model_id: str | None = None

    # --- plumbing --------------------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self._http.post(path, json=body)
        except httpx.HTTPError as e:
            raise AuthzError(f"openfga {path}: {e}") from e
        if r.status_code >= 400:
            raise AuthzError(f"openfga {path}: HTTP {r.status_code}: {r.text[:500]}")
        out: dict[str, Any] = r.json()
        return out

    def _store(self, suffix: str) -> str:
        if self.store_id is None:
            raise AuthzError("call ensure_schema() first")
        return f"/stores/{self.store_id}/{suffix}"

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Find or create the store, and write the model if the latest one differs."""
        r = self._http.get("/stores", params={"name": self._store_name})
        if r.status_code >= 400:
            raise AuthzError(f"openfga list stores: HTTP {r.status_code}: {r.text[:500]}")
        stores = [s for s in r.json().get("stores", []) if s.get("name") == self._store_name]
        if stores:
            self.store_id = stores[0]["id"]
        else:
            self.store_id = self._post("/stores", {"name": self._store_name})["id"]
        wanted = openfga_model()
        r = self._http.get(self._store("authorization-models"), params={"page_size": 1})
        models = r.json().get("authorization_models", []) if r.status_code < 400 else []
        if models and _same_model(models[0], wanted):
            self.model_id = models[0]["id"]
            return
        self.model_id = self._post(self._store("authorization-models"), wanted)[
            "authorization_model_id"
        ]
        log.info("wrote openfga model", extra={"model_id": self.model_id})

    # --- tuples ----------------------------------------------------------------------------

    def read(self, obj: ObjectRef) -> frozenset[Tuple]:
        out: set[Tuple] = set()
        token = ""
        while True:
            body: dict[str, Any] = {"tuple_key": {"object": _obj(obj)}, "page_size": 100}
            if token:
                body["continuation_token"] = token
            page = self._post(self._store("read"), body)
            out.update(from_key(t["key"]) for t in page.get("tuples", []))
            token = page.get("continuation_token") or ""
            if not token:
                return frozenset(out)

    def write(self, writes: Collection[Tuple], deletes: Collection[Tuple]) -> None:
        for w, d in safe_batches(writes, deletes, self._max_write):
            body: dict[str, Any] = {"authorization_model_id": self.model_id}
            if w:
                body["writes"] = {"tuple_keys": [to_key(t) for t in w]}
            if d:
                body["deletes"] = {"tuple_keys": [to_key(t) for t in d]}
            self._post(self._store("write"), body)

    # --- queries ---------------------------------------------------------------------------

    def check_docs(
        self, user_id: str, acl_ids: Sequence[str], consistency: Consistency
    ) -> dict[str, bool]:
        validate_subject(user_id)
        user = f"user:{ids.encode(user_id)}"
        unique = list(dict.fromkeys(acl_ids))
        out: dict[str, bool] = {}
        for i in range(0, len(unique), self._max_checks):
            batch = unique[i : i + self._max_checks]
            corr = {str(j): a for j, a in enumerate(batch)}
            body = {
                "authorization_model_id": self.model_id,
                "consistency": _CONSISTENCY[consistency],
                "checks": [
                    {
                        "tuple_key": {"user": user, "relation": CAN_VIEW, "object": f"doc:{a}"},
                        "correlation_id": c,
                    }
                    for c, a in corr.items()
                ],
            }
            result = self._post(self._store("batch-check"), body).get("result", {})
            for c, a in corr.items():
                r = result.get(c, {})
                # A per-check error is a denial: fail closed.
                out[a] = bool(r.get("allowed")) and not r.get("error")
        return out

    def list_docs(self, user_id: str, consistency: Consistency) -> ListResult:
        validate_subject(user_id)
        body = {
            "authorization_model_id": self.model_id,
            "type": "doc",
            "relation": CAN_VIEW,
            "user": f"user:{ids.encode(user_id)}",
            "consistency": _CONSISTENCY[consistency],
        }
        found: set[str] = set()
        started = time.monotonic()
        try:
            with self._http.stream("POST", self._store("streamed-list-objects"), json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    raise AuthzError(f"openfga streamed-list-objects: HTTP {r.status_code}")
                for obj in _ndjson_objects(r.iter_lines()):
                    found.add(obj.removeprefix("doc:"))
        except httpx.HTTPError as e:
            raise AuthzError(f"openfga streamed-list-objects: {e}") from e
        elapsed = time.monotonic() - started
        # The server ends the stream silently at its deadline; treat a near-deadline stream as
        # possibly truncated so callers can see the recall risk.
        complete = elapsed < 0.9 * self._list_deadline_s
        return ListResult(frozenset(found), complete)

    def delete_store(self) -> None:
        """Delete this backend's store. For tests and benchmark teardown."""
        if self.store_id is not None:
            r = self._http.delete(f"/stores/{self.store_id}")
            if r.status_code >= 400 and r.status_code != 404:
                raise AuthzError(f"openfga delete store: HTTP {r.status_code}")
            self.store_id = None

    def close(self) -> None:
        self._http.close()


def _ndjson_objects(lines: Iterator[str]) -> Iterator[str]:
    for line in lines:
        if not line.strip():
            continue
        msg = json.loads(line)
        if "error" in msg:
            # An error mid-stream means the list is unusable; never return a partial list that
            # might have skipped exclusion checks (GHSA-5278-rrxc-mgf7 was exactly that).
            raise AuthzError(f"openfga streamed-list-objects error: {msg['error']}")
        obj = msg.get("result", {}).get("object")
        if obj:
            yield obj


def _same_model(stored: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Compare a stored model with ours, ignoring the defaults OpenFGA fills in on read.

    The server returns ``module: ""``, ``source_info: null``, ``condition: ""``,
    ``object: ""`` and ``metadata: null``/``relations: {}`` on types without relations. Left
    in, they make every start write a new model version.
    """

    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            out = {k: strip(x) for k, x in v.items() if x is not None and x != ""}
            for k in ("relations", "metadata"):
                if out.get(k) == {}:
                    del out[k]
            return out
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    def norm(m: dict[str, Any]) -> str:
        return json.dumps(
            strip(
                {
                    "schema_version": m.get("schema_version"),
                    "type_definitions": m.get("type_definitions"),
                }
            ),
            sort_keys=True,
        )

    return norm(stored) == norm(wanted)
