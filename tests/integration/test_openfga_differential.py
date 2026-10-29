"""OpenFGA must agree with the oracle on every sampled (user, doc) pair (ADR-0002 §9).

OpenFGA shipped several exclusion/wildcard authorization bugs in 2026 (GHSA-5278-rrxc-mgf7,
GHSA-g3pg-frfm-pr2m); our model uses both. This test is the guard that runs on every image bump.
"""

from __future__ import annotations

import random

import pytest

from rag_authz import ids
from rag_authz.authz.base import Consistency
from rag_authz.authz.openfga import OpenFGABackend
from rag_authz.model import NodeAcl, container, doc, members, user
from rag_authz.synth import OrgSpec, generate

pytestmark = pytest.mark.integration


def test_model_roundtrip_and_read(openfga: OpenFGABackend) -> None:
    acl = NodeAcl(
        doc("github/acme/api/README.md"),
        parent=container("github/acme/api"),
        viewers=frozenset({user("alice@acme.test"), members("github/acme/team/core")}),
        restriction=frozenset({user("alice@acme.test")}),
    )
    openfga.write(acl.tuples(), [])
    assert openfga.read(acl.ref) == acl.tuples()
    openfga.write([], acl.tuples())
    assert openfga.read(acl.ref) == frozenset()


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_list_and_check_agree_with_oracle(openfga: OpenFGABackend, seed: int) -> None:
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
    openfga.write(org.tuples, [])
    rng = random.Random(seed)
    acl_ids = [ids.encode(d) for d in org.doc_ids]
    for u_idx in rng.sample(range(len(org.users)), 20):
        u = org.users[u_idx]
        expected = {acl_ids[i] for i in range(len(org.doc_ids)) if org.visibility[i, u_idx]}
        listed = openfga.list_docs(u, Consistency.FULL)
        assert listed.complete
        assert listed.acl_ids == expected, u
        sample = rng.sample(range(len(org.doc_ids)), 60)
        got = openfga.check_docs(u, [acl_ids[i] for i in sample], Consistency.FULL)
        for i in sample:
            assert got[acl_ids[i]] == bool(org.visibility[i, u_idx]), (u, org.doc_ids[i])


def test_ensure_schema_reuses_the_stored_model(openfga: OpenFGABackend) -> None:
    again = OpenFGABackend(openfga._http.base_url.__str__(), store_name=openfga._store_name)
    try:
        again.ensure_schema()
        assert again.store_id == openfga.store_id
        assert again.model_id == openfga.model_id
    finally:
        again.close()
