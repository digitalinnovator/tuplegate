"""OpenFGA adapter against a fake HTTP transport: wire format, batching, fail-closed parsing."""

import json

import httpx
import pytest

from tuplegate.authz.base import AuthzError, Consistency
from tuplegate.authz.openfga import OpenFGABackend, from_key, to_key
from tuplegate.model import NodeAcl, container, doc, members, public, user


def test_tuple_keys_roundtrip() -> None:
    acl = NodeAcl(
        doc("github/acme/api/README.md"),
        parent=container("github/acme/api"),
        viewers=frozenset({user("alice@acme.com"), members("github/acme/team/core"), public()}),
        restriction=frozenset({user("bob@acme.com")}),
    )
    for t in acl.tuples():
        key = to_key(t)
        assert "@" not in key["object"]
        assert "." not in key["object"]
        assert from_key(key) == t
    assert to_key(next(t for t in acl.tuples() if t.subject == public()))["user"] == "user:*"


def _backend(handler) -> OpenFGABackend:  # type: ignore[no-untyped-def]
    b = OpenFGABackend(
        "http://fga",
        client=httpx.Client(base_url="http://fga", transport=httpx.MockTransport(handler)),
    )
    b.store_id, b.model_id = "s1", "m1"
    return b


def test_batch_check_chunks_at_limit_and_denies_errors() -> None:
    calls: list[dict] = []  # type: ignore[type-arg]

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert req.url.path == "/stores/s1/batch-check"
        assert body["consistency"] == "HIGHER_CONSISTENCY"
        result = {}
        for c in body["checks"]:
            obj = c["tuple_key"]["object"]
            if obj.endswith("err"):
                result[c["correlation_id"]] = {"allowed": True, "error": {"message": "boom"}}
            else:
                result[c["correlation_id"]] = {"allowed": obj.endswith("0")}
        return httpx.Response(200, json={"result": result})

    b = _backend(handler)
    acl_ids = [f"d{i}" for i in range(120)] + ["xerr"]
    got = b.check_docs("alice@acme.com", acl_ids, Consistency.FULL)
    assert len(calls) == 3  # 50 + 50 + 21
    assert all(len(c["checks"]) <= 50 for c in calls)
    assert got["d10"] is True
    assert got["d11"] is False
    assert got["xerr"] is False  # per-item error denies
    assert calls[0]["checks"][0]["tuple_key"]["user"] == "user:alice=40acme=2Ecom"


def test_streamed_list_objects_parses_ndjson() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        lines = [{"result": {"object": "doc:a"}}, {"result": {"object": "doc:b"}}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n")

    got = _backend(handler).list_docs("alice", Consistency.FULL)
    assert got.acl_ids == {"a", "b"}
    assert got.complete


def test_streamed_list_objects_error_line_fails_closed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        lines = [{"result": {"object": "doc:a"}}, {"error": {"message": "deadline"}}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines))

    with pytest.raises(AuthzError):
        _backend(handler).list_docs("alice", Consistency.FULL)


def test_http_errors_raise_authz_error() -> None:
    b = _backend(lambda req: httpx.Response(500, text="nope"))
    with pytest.raises(AuthzError):
        b.check_docs("alice", ["d"], Consistency.FULL)


def test_same_model_ignores_server_defaults() -> None:
    from tuplegate.authz.openfga import _same_model
    from tuplegate.schema import openfga_model

    wanted = openfga_model()

    def fill(v: object) -> object:
        # What OpenFGA v1.21.0 returns from GET authorization-models.
        if isinstance(v, dict):
            out = {k: fill(x) for k, x in v.items()}
            if "relation" in out and "type" not in out and "this" not in out:
                out.setdefault("object", "")
            if "type" in out and "relations" not in out and "metadata" not in out:
                out.update(metadata=None, relations={})
            if "directly_related_user_types" in out:
                out.update(module="", source_info=None)
            return out
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v

    stored = fill(json.loads(json.dumps(wanted)))
    assert stored != wanted
    assert _same_model(stored, wanted)  # type: ignore[arg-type]
    changed = json.loads(json.dumps(wanted))
    changed["type_definitions"][1]["relations"]["member"] = {"computedUserset": {"relation": "x"}}
    assert not _same_model(changed, wanted)
