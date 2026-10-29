"""SpiceDB backend over the official gRPC client (``pip install rag-authz[spicedb]``).

Verified against SpiceDB v1.56.2 (docs/research/2026-09-27-engines-and-vector-stores.md):

- ``WriteRelationships`` is atomic, max 1,000 updates per call by default.
- ``CheckBulkPermissions`` answers each item independently (server max 10,000 items per call,
  measured); a per-item error is a denial.
- ``LookupResources`` streams without a result cap. A stream cut off by our deadline keeps
  what arrived, and is reported as incomplete: every streamed id is still authorised.
- The default ``minimize_latency`` serves reads at a revision quantized to 5s, so a revoke can
  keep working for up to that long. ``Consistency.FULL`` maps to ``fully_consistent``;
  ``Consistency.AT_LEAST_AS_FRESH`` uses the ZedToken of this backend's latest write.

SpiceDB has no stores. One schema serves the whole instance, and ``delete_all`` exists only for
tests and benchmark teardown.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Sequence
from typing import Any

import grpc
from authzed.api.v1 import (
    CheckBulkPermissionsRequest,
    CheckBulkPermissionsRequestItem,
    CheckPermissionResponse,
    Client,
    DeleteRelationshipsRequest,
    InsecureClient,
    LookupResourcesRequest,
    ObjectReference,
    ReadRelationshipsRequest,
    ReadSchemaRequest,
    Relationship,
    RelationshipFilter,
    RelationshipUpdate,
    SubjectReference,
    WriteRelationshipsRequest,
    WriteSchemaRequest,
    ZedToken,
)
from authzed.api.v1 import (
    Consistency as PbConsistency,
)
from authzed.api.v1.permission_service_pb2 import LOOKUP_PERMISSIONSHIP_HAS_PERMISSION
from grpcutil import bearer_token_credentials

from rag_authz import ids
from rag_authz.authz.base import (
    AuthzError,
    Consistency,
    ListResult,
    safe_batches,
    validate_subject,
)
from rag_authz.model import CAN_VIEW, ObjectRef, ObjectType, Relation, SubjectRef, Tuple
from rag_authz.schema import spicedb_schema

log = logging.getLogger(__name__)

_HAS = CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION


def _obj(ref: ObjectRef) -> ObjectReference:
    return ObjectReference(object_type=str(ref.type), object_id=ids.encode(ref.id))


def to_relationship(t: Tuple) -> Relationship:
    if t.relation is Relation.PARENT:
        assert t.parent_ref is not None
        subject = SubjectReference(object=_obj(t.parent_ref))
    else:
        assert t.subject is not None
        s: SubjectRef = t.subject
        subject = SubjectReference(
            object=ObjectReference(object_type=str(s.type), object_id=ids.encode(s.id)),
            optional_relation=str(s.relation) if s.relation else "",
        )
    return Relationship(resource=_obj(t.object), relation=str(t.relation), subject=subject)


def from_relationship(r: Relationship) -> Tuple:
    obj = ObjectRef(ObjectType(r.resource.object_type), ids.decode(r.resource.object_id))
    rel = Relation(r.relation)
    s = r.subject
    if rel is Relation.PARENT:
        return Tuple(
            obj, rel, parent_ref=ObjectRef(ObjectType.CONTAINER, ids.decode(s.object.object_id))
        )
    subject = SubjectRef(
        ObjectType(s.object.object_type),
        ids.decode(s.object.object_id),
        s.optional_relation or None,
    )
    return Tuple(obj, rel, subject)


def _normalise_schema(text: str) -> str:
    # ReadSchema returns the schema as SpiceDB formats it; compare token streams.
    return " ".join(re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S).split())


class SpiceDBBackend:
    name = "spicedb"

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        insecure: bool = False,
        max_updates_per_write: int = 1_000,
        max_checks_per_batch: int = 1_000,
        list_deadline_s: float = 3.0,
        timeout_s: float = 30.0,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        elif insecure:
            # Plaintext gRPC. For a local or in-cluster sidecar only.
            self._client = InsecureClient(endpoint, token)
        else:
            self._client = Client(endpoint, bearer_token_credentials(token))
        self._max_write = max_updates_per_write
        self._max_checks = max_checks_per_batch
        self._list_deadline_s = list_deadline_s
        self._timeout_s = timeout_s
        # ZedToken of the latest write through this backend: the floor for AT_LEAST_AS_FRESH.
        self.last_written: ZedToken | None = None

    # --- plumbing --------------------------------------------------------------------------

    def _consistency(self, c: Consistency) -> PbConsistency:
        match c:
            case Consistency.FULL:
                return PbConsistency(fully_consistent=True)
            case Consistency.MINIMIZE_LATENCY:
                return PbConsistency(minimize_latency=True)
            case Consistency.AT_LEAST_AS_FRESH:
                if self.last_written is None:
                    return PbConsistency(fully_consistent=True)
                return PbConsistency(at_least_as_fresh=self.last_written)

    def _call(self, what: str, fn: Any, req: Any) -> Any:
        try:
            return fn(req, timeout=self._timeout_s)
        except grpc.RpcError as e:
            raise AuthzError(f"spicedb {what}: {_rpc_message(e)}") from e

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        wanted = spicedb_schema()
        try:
            current = self._client.ReadSchema(ReadSchemaRequest(), timeout=self._timeout_s)
            if _normalise_schema(current.schema_text) == _normalise_schema(wanted):
                return
        except grpc.RpcError as e:
            if e.code() is not grpc.StatusCode.NOT_FOUND:  # no schema written yet
                raise AuthzError(f"spicedb ReadSchema: {_rpc_message(e)}") from e
        self._call("WriteSchema", self._client.WriteSchema, WriteSchemaRequest(schema=wanted))
        log.info("wrote spicedb schema")

    # --- tuples ----------------------------------------------------------------------------

    def read(self, obj: ObjectRef) -> frozenset[Tuple]:
        out: set[Tuple] = set()
        cursor = None
        page = 1_000
        while True:
            req = ReadRelationshipsRequest(
                consistency=PbConsistency(fully_consistent=True),
                relationship_filter=RelationshipFilter(
                    resource_type=str(obj.type), optional_resource_id=ids.encode(obj.id)
                ),
                optional_limit=page,
                optional_cursor=cursor,
            )
            n = 0
            try:
                for resp in self._client.ReadRelationships(req, timeout=self._timeout_s):
                    out.add(from_relationship(resp.relationship))
                    cursor = resp.after_result_cursor
                    n += 1
            except grpc.RpcError as e:
                raise AuthzError(f"spicedb ReadRelationships: {_rpc_message(e)}") from e
            if n < page:
                return frozenset(out)

    def write(self, writes: Collection[Tuple], deletes: Collection[Tuple]) -> None:
        for w, d in safe_batches(writes, deletes, self._max_write):
            updates = [
                RelationshipUpdate(
                    operation=RelationshipUpdate.OPERATION_TOUCH, relationship=to_relationship(t)
                )
                for t in w
            ] + [
                RelationshipUpdate(
                    operation=RelationshipUpdate.OPERATION_DELETE, relationship=to_relationship(t)
                )
                for t in d
            ]
            resp = self._call(
                "WriteRelationships",
                self._client.WriteRelationships,
                WriteRelationshipsRequest(updates=updates),
            )
            self.last_written = resp.written_at

    # --- queries ---------------------------------------------------------------------------

    def check_docs(
        self, user_id: str, acl_ids: Sequence[str], consistency: Consistency
    ) -> dict[str, bool]:
        validate_subject(user_id)
        subject = SubjectReference(
            object=ObjectReference(object_type="user", object_id=ids.encode(user_id))
        )
        unique = list(dict.fromkeys(acl_ids))
        out: dict[str, bool] = {}
        for i in range(0, len(unique), self._max_checks):
            batch = unique[i : i + self._max_checks]
            req = CheckBulkPermissionsRequest(
                consistency=self._consistency(consistency),
                items=[
                    CheckBulkPermissionsRequestItem(
                        resource=ObjectReference(object_type="doc", object_id=a),
                        permission=CAN_VIEW,
                        subject=subject,
                    )
                    for a in batch
                ],
            )
            resp = self._call("CheckBulkPermissions", self._client.CheckBulkPermissions, req)
            if len(resp.pairs) != len(batch):
                raise AuthzError("spicedb CheckBulkPermissions: answer count mismatch")
            for pair in resp.pairs:
                a = pair.request.resource.object_id
                # A per-item error, or a conditional answer (we define no caveats), is a denial.
                ok = pair.WhichOneof("response") == "item" and pair.item.permissionship == _HAS
                out[a] = ok and out.get(a, True)
        # Anything the engine did not answer for is denied.
        return {a: out.get(a, False) for a in unique}

    def list_docs(self, user_id: str, consistency: Consistency) -> ListResult:
        validate_subject(user_id)
        req = LookupResourcesRequest(
            consistency=self._consistency(consistency),
            resource_object_type="doc",
            permission=CAN_VIEW,
            subject=SubjectReference(
                object=ObjectReference(object_type="user", object_id=ids.encode(user_id))
            ),
        )
        found: set[str] = set()
        complete = True
        try:
            for resp in self._client.LookupResources(req, timeout=self._list_deadline_s):
                if resp.permissionship == LOOKUP_PERMISSIONSHIP_HAS_PERMISSION:
                    found.add(resp.resource_object_id)
        except grpc.RpcError as e:
            if e.code() is not grpc.StatusCode.DEADLINE_EXCEEDED:
                raise AuthzError(f"spicedb LookupResources: {_rpc_message(e)}") from e
            # Every id streamed so far was a definite HAS_PERMISSION; the rest is lost recall.
            complete = False
        return ListResult(frozenset(found), complete)

    def delete_all(self) -> None:
        """Delete every relationship of every type. For tests and benchmark teardown."""
        for t in ObjectType:
            if t is ObjectType.USER:
                continue
            while True:
                resp = self._call(
                    "DeleteRelationships",
                    self._client.DeleteRelationships,
                    DeleteRelationshipsRequest(
                        relationship_filter=RelationshipFilter(resource_type=str(t)),
                        optional_limit=1_000,
                        optional_allow_partial_deletions=True,
                    ),
                )
                self.last_written = resp.deleted_at
                if resp.deletion_progress != resp.DELETION_PROGRESS_PARTIAL:
                    break

    def close(self) -> None:
        close: Any = getattr(self._client, "close", None)
        if callable(close):
            close()


def _rpc_message(e: grpc.RpcError) -> str:
    code: Any = getattr(e, "code", None)
    details: Any = getattr(e, "details", None)
    c = code() if callable(code) else "?"
    d = details() if callable(details) else str(e)
    return f"{c}: {d}"
