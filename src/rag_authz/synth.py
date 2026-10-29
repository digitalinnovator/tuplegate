"""Synthetic organisation generator for the leak-rate harness and the filtering benchmark.

There is no public dataset of enterprise ACL distributions (see docs/benchmark/plan.md), so
every distribution here is an explicit, documented assumption with a knob, and the benchmark
sweeps the knobs that matter instead of claiming one "realistic" setting.

What it models:

- **Group-size skew.** Group sizes follow a rank-size (Zipf) law: a few department-sized
  groups, a long tail of small teams.
- **Membership skew.** Users join groups with Zipf-distributed propensity, so some users
  (admins, managers) sit in many groups and most sit in a few. This is what makes per-user
  selectivity vary by orders of magnitude, which drives the pre- vs post-filter trade-off.
- **Nested groups**, as a DAG (a group only nests under a larger one).
- **Hierarchy**: roots (spaces, shared drives, repos) with folder trees, inherited grants,
  inheritance breaks (Drive limited-access folders) and intersective restrictions
  (Confluence view restrictions).
- **ACL/topic correlation.** Docs under one root share topics, so what a user may see is
  clustered in embedding space. Filtered ANN behaves very differently when the filter is
  correlated with the query, so this is a knob.
- **Queries** are split into in-scope (a topic the user can see) and out-of-scope probes (a
  topic the user cannot see), which are the probes most likely to surface leaks.

Everything is deterministic in ``seed``.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from rag_authz.leakcheck import Probe
from rag_authz.model import (
    GroupMembers,
    NodeAcl,
    ObjectRef,
    SubjectRef,
    Tuple,
    container,
    doc,
    group,
    members,
    public,
    user,
)
from rag_authz.oracle import Oracle

FloatArray = npt.NDArray[np.float32]

ALL_EMPLOYEES = "all-employees"


@dataclass(frozen=True)
class OrgSpec:
    seed: int = 7
    users: int = 2_000
    groups: int = 200
    # Rank-size exponent for group sizes; size(rank r) = largest_group * r ** -alpha.
    group_size_alpha: float = 1.1
    largest_group_fraction: float = 0.4
    min_group_size: int = 3
    # Zipf exponent for how many groups a user joins. 0 = uniform; higher = more power users.
    membership_alpha: float = 0.8
    nested_group_fraction: float = 0.25
    roots: int = 80
    folders_per_root: float = 6.0
    max_folder_depth: int = 5
    docs: int = 10_000
    max_chunks_per_doc: int = 3
    dim: int = 64
    topics: int = 100
    topics_per_root: int = 2
    acl_topic_correlation: float = 0.85
    # Norm of the per-chunk noise relative to the unit topic centroid.
    topic_noise: float = 0.8
    # Roots readable by everyone (all-employees or public). Their *doc share* is set directly,
    # because a single heavily-populated company-wide space otherwise sets every user's floor.
    company_wide_roots: int = 3
    company_wide_doc_share: float = 0.02
    p_company_wide_public: float = 0.3
    # Rank-size exponent for how many docs each container holds (0 = uniform).
    doc_placement_alpha: float = 0.6
    p_extra_group_grant: float = 0.25
    p_doc_user_share: float = 0.05
    p_doc_public: float = 0.003
    p_inheritance_break: float = 0.03
    p_restriction: float = 0.03
    queries: int = 500
    p_out_of_scope_query: float = 0.5
    # "uniform": probe users drawn uniformly (most land in the common narrow-reach band).
    # "stratified": probes spread evenly over the selectivity buckets that have users, so
    # broad-reach users (where pre-filter lists get long) are measured, not just present.
    probe_sampling: str = "uniform"


@dataclass(frozen=True)
class Query:
    user: str
    vector: FloatArray
    intent: Literal["in_scope", "out_of_scope"]
    topic: int


@dataclass
class SyntheticOrg:
    spec: OrgSpec
    users: list[str]
    tuples: frozenset[Tuple]
    doc_ids: list[str]
    chunk_ids: list[str]
    chunk_doc: list[str]
    chunk_text: list[str]
    vectors: FloatArray
    queries: list[Query]
    # Ground truth from the oracle: visibility[doc_index, user_index] is can_view.
    visibility: npt.NDArray[np.bool_]
    stats: dict[str, object] = field(default_factory=dict)

    def oracle(self) -> Oracle:
        return Oracle(self.tuples)


def _uid(i: int) -> str:
    return f"u{i:05d}@example.test"


def _unit(v: FloatArray) -> FloatArray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return (v / np.maximum(n, 1e-12)).astype(np.float32)


def visibility_matrix(
    oracle: Oracle, users: list[str], doc_ids: list[str]
) -> npt.NDArray[np.bool_]:
    """Boolean ``[doc, user]`` matrix of ``can_view``, computed with the oracle's bitsets."""
    masks = oracle.bulk(users).doc_masks()
    nbytes = (len(users) + 7) // 8
    out = np.zeros((len(doc_ids), len(users)), dtype=np.bool_)
    for i, did in enumerate(doc_ids):
        m = masks.get(did, 0)
        if m:
            bits = np.unpackbits(
                np.frombuffer(m.to_bytes(nbytes, "little"), dtype=np.uint8), bitorder="little"
            )
            out[i] = bits[: len(users)].astype(np.bool_)
    return out


def generate(spec: OrgSpec) -> SyntheticOrg:
    rng = np.random.default_rng(spec.seed)
    users = [_uid(i) for i in range(spec.users)]
    tuples: set[Tuple] = set()

    # --- groups ----------------------------------------------------------------------------
    ranks = np.arange(1, spec.groups + 1, dtype=np.float64)
    largest = spec.largest_group_fraction * spec.users
    sizes = np.maximum(spec.min_group_size, np.round(largest * ranks**-spec.group_size_alpha))
    sizes = np.minimum(sizes, spec.users).astype(int)
    propensity = np.arange(1, spec.users + 1, dtype=np.float64) ** -spec.membership_alpha
    rng.shuffle(propensity)
    propensity /= propensity.sum()
    group_ids = [f"g{i:04d}" for i in range(spec.groups)]
    group_members: dict[str, set[str]] = {}
    for gid, size in zip(group_ids, sizes, strict=True):
        chosen = rng.choice(spec.users, size=size, replace=False, p=propensity)
        group_members[gid] = {users[i] for i in chosen}
    # Everyone belongs to at least one small team.
    in_any = set().union(*group_members.values())
    small = group_ids[spec.groups // 2 :]
    for u in users:
        if u not in in_any:
            group_members[str(rng.choice(small))].add(u)
    for gid, ms in group_members.items():
        tuples |= GroupMembers(group(gid), frozenset(user(u) for u in ms)).tuples()
    nested: list[tuple[str, str]] = []
    for idx in range(1, spec.groups):
        if rng.random() < spec.nested_group_fraction:
            parent_idx = int(rng.integers(0, idx))
            nested.append((group_ids[parent_idx], group_ids[idx]))
            tuples |= GroupMembers(
                group(group_ids[parent_idx]), frozenset({members(group_ids[idx])})
            ).tuples()
    tuples |= GroupMembers(group(ALL_EMPLOYEES), frozenset(user(u) for u in users)).tuples()

    def random_group() -> str:
        return group_ids[int(rng.integers(0, spec.groups))]

    def small_group() -> str:
        return small[int(rng.integers(0, len(small)))]

    # --- containers ------------------------------------------------------------------------
    centroids = _unit(rng.normal(size=(spec.topics, spec.dim)).astype(np.float32))
    containers: list[ObjectRef] = []
    container_root: dict[ObjectRef, int] = {}
    container_depth: dict[ObjectRef, int] = {}
    root_topics: list[npt.NDArray[np.int64]] = []
    breaks = restrictions = 0
    company_wide_root: set[int] = set()
    for r in range(spec.roots):
        root = container(f"root{r:03d}")
        viewers: set[SubjectRef] = {members(random_group())}
        if r < spec.company_wide_roots:
            company_wide_root.add(r)
            viewers.add(
                public() if rng.random() < spec.p_company_wide_public else members(ALL_EMPLOYEES)
            )
        if rng.random() < spec.p_extra_group_grant:
            viewers.add(members(random_group()))
        tuples |= NodeAcl(root, viewers=frozenset(viewers)).tuples()
        containers.append(root)
        container_root[root] = r
        container_depth[root] = 0
        root_topics.append(rng.choice(spec.topics, size=spec.topics_per_root, replace=False))
        n_folders = int(rng.poisson(spec.folders_per_root))
        level: list[ObjectRef] = [root]
        for f in range(n_folders):
            parent = level[int(rng.integers(0, len(level)))]
            if container_depth[parent] >= spec.max_folder_depth:
                parent = root
            folder = container(f"root{r:03d}/f{f:03d}")
            acl_parent: ObjectRef | None = parent
            fviewers: set[SubjectRef] = set()
            restriction: frozenset[SubjectRef] | None = None
            if rng.random() < spec.p_inheritance_break:
                acl_parent = None
                fviewers.add(members(small_group()))
                breaks += 1
            if rng.random() < spec.p_extra_group_grant:
                fviewers.add(members(random_group()))
            if rng.random() < spec.p_restriction:
                restriction = frozenset({members(small_group())})
                restrictions += 1
            tuples |= NodeAcl(folder, acl_parent, frozenset(fviewers), restriction).tuples()
            containers.append(folder)
            container_root[folder] = r
            container_depth[folder] = container_depth[parent] + 1
            level.append(folder)

    # --- docs and chunks -------------------------------------------------------------------
    weights = np.arange(1, len(containers) + 1, dtype=np.float64) ** -spec.doc_placement_alpha
    rng.shuffle(weights)
    wide = np.array([container_root[c] in company_wide_root for c in containers])
    if wide.any() and (~wide).any():
        weights[wide] *= spec.company_wide_doc_share / weights[wide].sum()
        weights[~wide] *= (1 - spec.company_wide_doc_share) / weights[~wide].sum()
    weights /= weights.sum()
    doc_ids: list[str] = []
    chunk_ids: list[str] = []
    chunk_doc: list[str] = []
    chunk_text: list[str] = []
    chunk_topic: list[int] = []
    doc_topic: dict[str, int] = {}
    homes = rng.choice(len(containers), size=spec.docs, p=weights)
    for d, home_idx in enumerate(homes):
        home = containers[int(home_idx)]
        did = f"{home.id}/d{d:06d}"
        doc_ids.append(did)
        doc_parent: ObjectRef | None = home
        dviewers: set[SubjectRef] = set()
        restriction = None
        if rng.random() < spec.p_inheritance_break:
            doc_parent = None
            dviewers.add(members(small_group()))
            breaks += 1
        if rng.random() < spec.p_doc_user_share:
            for i in rng.choice(spec.users, size=int(rng.integers(1, 4)), replace=False):
                dviewers.add(user(users[int(i)]))
        if rng.random() < spec.p_doc_public:
            dviewers.add(public())
        if rng.random() < spec.p_restriction:
            restriction = frozenset({members(small_group())})
            restrictions += 1
        tuples |= NodeAcl(doc(did), doc_parent, frozenset(dviewers), restriction).tuples()
        topics = root_topics[container_root[home]]
        if rng.random() < spec.acl_topic_correlation:
            t = int(topics[int(rng.integers(0, len(topics)))])
        else:
            t = int(rng.integers(0, spec.topics))
        doc_topic[did] = t
        for c in range(int(rng.integers(1, spec.max_chunks_per_doc + 1))):
            chunk_ids.append(f"{did}#c{c}")
            chunk_doc.append(did)
            chunk_text.append(f"synthetic chunk {c} of {did} about topic {t}")
            chunk_topic.append(t)
    noise_scale = spec.topic_noise / np.sqrt(spec.dim)
    noise = rng.normal(scale=noise_scale, size=(len(chunk_ids), spec.dim)).astype(np.float32)
    vectors = _unit(centroids[np.asarray(chunk_topic)] + noise)

    # --- queries ---------------------------------------------------------------------------
    frozen = frozenset(tuples)
    visibility = visibility_matrix(Oracle(frozen), users, doc_ids)  # docs x users, bool
    queries: list[Query] = []
    stratified = _stratified_users(spec, visibility, rng)
    for i in range(spec.queries):
        # Uniform draws stay interleaved with the per-query draws below, so every org generated
        # before stratified sampling existed is reproduced exactly.
        u_idx = int(rng.integers(0, spec.users)) if stratified is None else stratified[i]
        want_out = rng.random() < spec.p_out_of_scope_query
        pool = np.flatnonzero(visibility[:, u_idx] != want_out)
        if pool.size == 0:
            want_out = not want_out
            pool = np.flatnonzero(visibility[:, u_idx] != want_out)
        t = doc_topic[doc_ids[int(pool[int(rng.integers(0, pool.size))])]]
        qv = _unit(centroids[t] + rng.normal(scale=noise_scale, size=spec.dim).astype(np.float32))
        queries.append(Query(users[u_idx], qv, "out_of_scope" if want_out else "in_scope", t))

    selectivity = visibility.sum(axis=0) / max(1, spec.docs)
    stats: dict[str, object] = {
        "spec": asdict(spec),
        "tuples": len(frozen),
        "chunks": len(chunk_ids),
        "group_size_max": int(sizes.max()),
        "group_size_median": float(np.median(sizes)),
        "groups_per_user_p50": float(
            np.median([sum(u in ms for ms in group_members.values()) for u in users])
        ),
        "groups_per_user_max": int(
            max(sum(u in ms for ms in group_members.values()) for u in users)
        ),
        "nested_edges": len(nested),
        "containers": len(containers),
        "inheritance_breaks": breaks,
        "restrictions": restrictions,
        "selectivity_p10": float(np.percentile(selectivity, 10)),
        "selectivity_p50": float(np.percentile(selectivity, 50)),
        "selectivity_p90": float(np.percentile(selectivity, 90)),
        "selectivity_max": float(selectivity.max()),
    }
    return SyntheticOrg(
        spec=spec,
        users=users,
        tuples=frozen,
        doc_ids=doc_ids,
        chunk_ids=chunk_ids,
        chunk_doc=chunk_doc,
        chunk_text=chunk_text,
        vectors=vectors,
        queries=queries,
        visibility=visibility,
        stats=stats,
    )


# Matches leakcheck.SELECTIVITY_BUCKETS.
_BUCKET_EDGES = (0.0, 0.01, 0.05, 0.20, 0.50, 1.01)


def _stratified_users(
    spec: OrgSpec, visibility: npt.NDArray[np.bool_], rng: np.random.Generator
) -> list[int] | None:
    if spec.probe_sampling == "uniform":
        return None
    if spec.probe_sampling != "stratified":
        raise ValueError(f"unknown probe_sampling {spec.probe_sampling!r}")
    sel = visibility.sum(axis=0) / max(1, spec.docs)
    buckets = [
        np.flatnonzero((sel >= lo) & (sel < hi)) for lo, hi in itertools.pairwise(_BUCKET_EDGES)
    ]
    buckets = [b for b in buckets if b.size]
    return [
        int(buckets[i % len(buckets)][int(rng.integers(0, buckets[i % len(buckets)].size))])
        for i in range(spec.queries)
    ]


class SyntheticTruth:
    """Ground truth for ``leakcheck.run`` over a synthetic org: oracle ACLs + exact search."""

    def __init__(self, org: SyntheticOrg, k: int) -> None:
        self._org = org
        self._uidx = {u: i for i, u in enumerate(org.users)}
        self._didx = {d: i for i, d in enumerate(org.doc_ids)}
        chunk_doc_idx = np.fromiter((self._didx[d] for d in org.chunk_doc), dtype=np.int64)
        self._chunk_visibility = org.visibility[chunk_doc_idx]  # chunks x users
        self._auth_chunks = self._chunk_visibility.sum(axis=0)
        self._selectivity = org.visibility.mean(axis=0)
        self._expected: list[list[str]] = []
        for q in org.queries:
            mask = self._chunk_visibility[:, self._uidx[q.user]]
            scores = np.where(mask, org.vectors @ q.vector, -np.inf)
            top = np.argsort(-scores, kind="stable")[:k]
            self._expected.append([org.chunk_ids[i] for i in top if mask[i]])

    def probes(self) -> list[Probe]:
        return [Probe(q.user, q.vector.tolist(), q.intent) for q in self._org.queries]

    def allowed(self, user: str, doc_id: str) -> bool:
        d = self._didx.get(doc_id)
        u = self._uidx.get(user)
        return d is not None and u is not None and bool(self._org.visibility[d, u])

    def expected(self, probe_index: int) -> list[str]:
        return self._expected[probe_index]

    def authorized_chunks(self, user: str) -> int:
        return int(self._auth_chunks[self._uidx[user]])

    def selectivity(self, user: str) -> float:
        return float(self._selectivity[self._uidx[user]])
