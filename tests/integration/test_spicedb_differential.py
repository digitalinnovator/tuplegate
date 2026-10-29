"""SpiceDB must agree with the oracle on every sampled (user, doc) pair (ADR-0002 §9).

Needs `make up-full`.
"""

from __future__ import annotations

import random

import pytest

from rag_authz import ids
from rag_authz.authz.base import Consistency
from rag_authz.authz.spicedb import SpiceDBBackend
from rag_authz.model import NodeAcl, container, doc, members, public, user
from rag_authz.synth import OrgSpec, generate

pytestmark = pytest.mark.integration


def test_model_roundtrip_and_read(spicedb: SpiceDBBackend) -> None:
    acl = NodeAcl(
        doc("github/acme/api/README.md"),
        parent=container("github/acme/api"),
        viewers=frozenset({user("alice@acme.test"), members("github/acme/team/core"), public()}),
        restriction=frozenset({user("alice@acme.test")}),
    )
    spicedb.write(acl.tuples(), [])
    assert spicedb.read(acl.ref) == acl.tuples()
    spicedb.write([], acl.tuples())
    assert spicedb.read(acl.ref) == frozenset()


def test_read_pages_past_the_server_limit(spicedb: SpiceDBBackend) -> None:
    viewers = frozenset(user(f"u{i}@acme.test") for i in range(2_500))
    acl = NodeAcl(doc("big"), viewers=viewers)
    spicedb.write(acl.tuples(), [])
    assert spicedb.read(acl.ref) == acl.tuples()


def test_ensure_schema_is_idempotent(spicedb: SpiceDBBackend) -> None:
    spicedb.ensure_schema()
    spicedb.ensure_schema()


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_list_and_check_agree_with_oracle(spicedb: SpiceDBBackend, seed: int) -> None:
    org = generate(
        OrgSpec(
            seed=seed,
            users=150,
            groups=30,
            roots=15,
            docs=600,
            queries=5,
            p_restriction=0.10,
            p_inheritance_break=0.10,
            p_doc_public=0.02,
            p_doc_user_share=0.10,
        )
    )
    spicedb.write(org.tuples, [])
    rng = random.Random(seed)
    acl_ids = [ids.encode(d) for d in org.doc_ids]
    for u_idx in rng.sample(range(len(org.users)), 20):
        u = org.users[u_idx]
        expected = {acl_ids[i] for i in range(len(org.doc_ids)) if org.visibility[i, u_idx]}
        listed = spicedb.list_docs(u, Consistency.FULL)
        assert listed.complete
        assert listed.acl_ids == expected, u
        sample = rng.sample(range(len(org.doc_ids)), 60)
        got = spicedb.check_docs(u, [acl_ids[i] for i in sample], Consistency.FULL)
        for i in sample:
            assert got[acl_ids[i]] == bool(org.visibility[i, u_idx]), (u, org.doc_ids[i])


def test_revoke_is_visible_at_least_as_fresh(spicedb: SpiceDBBackend) -> None:
    acl = NodeAcl(doc("d"), viewers=frozenset({user("bob@acme.test")}))
    spicedb.write(acl.tuples(), [])
    a = ids.encode("d")
    assert spicedb.check_docs("bob@acme.test", [a], Consistency.AT_LEAST_AS_FRESH) == {a: True}
    spicedb.write([], acl.tuples())
    assert spicedb.check_docs("bob@acme.test", [a], Consistency.AT_LEAST_AS_FRESH) == {a: False}
    assert spicedb.list_docs("bob@acme.test", Consistency.AT_LEAST_AS_FRESH).acl_ids == frozenset()
