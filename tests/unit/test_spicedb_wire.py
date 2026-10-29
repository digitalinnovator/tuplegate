from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import grpc
import pytest
from authzed.api.v1 import (
    CheckBulkPermissionsPair,
    CheckBulkPermissionsResponse,
    CheckBulkPermissionsResponseItem,
    CheckPermissionResponse,
    LookupResourcesResponse,
)
from authzed.api.v1.permission_service_pb2 import (
    LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION,
    LOOKUP_PERMISSIONSHIP_HAS_PERMISSION,
)
from google.rpc.status_pb2 import Status

from rag_authz.authz.base import AuthzError, Consistency
from rag_authz.authz.spicedb import SpiceDBBackend, from_relationship, to_relationship
from rag_authz.model import NodeAcl, container, doc, members, public, user

HAS = CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION
NO = CheckPermissionResponse.PERMISSIONSHIP_NO_PERMISSION
CONDITIONAL = CheckPermissionResponse.PERMISSIONSHIP_CONDITIONAL_PERMISSION


class _RpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return "boom"


class FakeClient:
    def __init__(self) -> None:
        self.check_answers: dict[str, Any] = {}
        self.lookup: list[tuple[str, int]] = []
        self.lookup_error: grpc.StatusCode | None = None
        self.check_calls = 0

    def CheckBulkPermissions(self, req: Any, timeout: float) -> CheckBulkPermissionsResponse:  # noqa: N802
        self.check_calls += 1
        pairs = []
        for item in req.items:
            a = item.resource.object_id
            ans = self.check_answers.get(a, NO)
            if ans == "error":
                pairs.append(CheckBulkPermissionsPair(request=item, error=Status(code=13)))
            else:
                pairs.append(
                    CheckBulkPermissionsPair(
                        request=item, item=CheckBulkPermissionsResponseItem(permissionship=ans)
                    )
                )
        return CheckBulkPermissionsResponse(pairs=pairs)

    def LookupResources(self, req: Any, timeout: float) -> Iterator[LookupResourcesResponse]:  # noqa: N802
        for rid, p in self.lookup:
            yield LookupResourcesResponse(resource_object_id=rid, permissionship=p)
        if self.lookup_error is not None:
            raise _RpcError(self.lookup_error)


def _backend(client: FakeClient, **kw: Any) -> SpiceDBBackend:
    return SpiceDBBackend("unused", "unused", client=client, **kw)


def test_relationship_roundtrip() -> None:
    acl = NodeAcl(
        doc("drive/Q3 plan ✓.docx"),
        parent=container("drive/folder"),
        viewers=frozenset({user("alice@acme.test"), members("eng"), public()}),
        restriction=frozenset({members("legal")}),
    )
    for t in acl.tuples():
        assert from_relationship(to_relationship(t)) == t


def test_bulk_check_fails_closed_on_errors_and_conditionals() -> None:
    c = FakeClient()
    c.check_answers = {"a": HAS, "b": "error", "c": CONDITIONAL, "d": NO}
    got = _backend(c, max_checks_per_batch=2).check_docs(
        "u@x", ["a", "b", "c", "d", "a"], Consistency.FULL
    )
    assert got == {"a": True, "b": False, "c": False, "d": False}
    assert c.check_calls == 2


def test_lookup_deadline_keeps_streamed_ids_and_reports_incomplete() -> None:
    c = FakeClient()
    c.lookup = [
        ("a", LOOKUP_PERMISSIONSHIP_HAS_PERMISSION),
        ("b", LOOKUP_PERMISSIONSHIP_HAS_PERMISSION),
    ]
    c.lookup_error = grpc.StatusCode.DEADLINE_EXCEEDED
    got = _backend(c).list_docs("u@x", Consistency.FULL)
    assert got.acl_ids == {"a", "b"}
    assert not got.complete


def test_lookup_drops_conditional_results() -> None:
    c = FakeClient()
    c.lookup = [
        ("a", LOOKUP_PERMISSIONSHIP_HAS_PERMISSION),
        ("b", LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION),
    ]
    got = _backend(c).list_docs("u@x", Consistency.FULL)
    assert got.acl_ids == {"a"}
    assert got.complete


def test_lookup_other_errors_fail_closed() -> None:
    c = FakeClient()
    c.lookup = [("a", LOOKUP_PERMISSIONSHIP_HAS_PERMISSION)]
    c.lookup_error = grpc.StatusCode.UNAVAILABLE
    with pytest.raises(AuthzError):
        _backend(c).list_docs("u@x", Consistency.FULL)


def test_unmapped_subjects_are_refused() -> None:
    with pytest.raises(ValueError, match="invalid query subject"):
        _backend(FakeClient()).list_docs("~github/octocat", Consistency.FULL)
