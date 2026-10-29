"""Revocation lag (benchmark plan Q3) and what a folder-level revoke costs.

Part A, enforcement lag. A user reaches a doc through a group grant on its folder. The group
membership is revoked through the sync engine, then the retriever is polled every
``--poll-ms`` until the doc stops coming back. Reported per engine, consistency and strategy:

- sidecar lag (a): engine write committed minus the change being observed;
- enforcement lag (b): first compliant response minus write committed;
- leaked responses: responses after the commit that still returned the revoked doc.

Source-feed lag (how long Google, GitHub or Atlassian take to tell us) is not in these numbers;
docs/research/2026-09-27-source-acl-semantics.md documents it per source.

Part B, revoke cost against subtree size. ADR-0002 keeps grants where the source stores them,
so revoking a folder grant is one tuple however many docs sit below it. The flattened
alternative (each doc carries its effective principals, as Elastic DLS and Azure AI Search do)
has to rewrite every descendant. Both are written to the same engine and timed.

    uv run python benchmarks/revocation_lag.py --engines openfga,spicedb
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from rag_authz import ids
from rag_authz.authz.base import AuthzBackend, Consistency
from rag_authz.index.base import ChunkRecord
from rag_authz.index.pgvector import PgVectorIndex
from rag_authz.model import (
    GroupMembers,
    NodeAcl,
    Relation,
    Tuple,
    UpsertGroup,
    container,
    doc,
    group,
    members,
    user,
)
from rag_authz.retrieval import AuthorizedRetriever, RetrievalConfig, Strategy
from rag_authz.sync import SyncEngine

ROOT = Path(__file__).resolve().parents[1]
U, OTHER = "revoked@bench.test", "stays@bench.test"
G = group("bench/team")
FOLDER = container("bench/folder")
TARGET = doc("bench/folder/target")


def make_engine(name: str) -> AuthzBackend:
    if name == "openfga":
        from rag_authz.authz.openfga import OpenFGABackend

        b: AuthzBackend = OpenFGABackend(
            os.environ.get("RAG_AUTHZ_OPENFGA_URL", "http://127.0.0.1:58080"),
            store_name=f"lag-{time.time_ns()}",
        )
    else:
        from rag_authz.authz.spicedb import SpiceDBBackend

        sp = SpiceDBBackend(
            os.environ.get("RAG_AUTHZ_SPICEDB_ENDPOINT", "127.0.0.1:55051"),
            os.environ.get("RAG_AUTHZ_SPICEDB_TOKEN", "rag-dev-only"),
            insecure=True,
        )
        sp.ensure_schema()
        sp.delete_all()
        b = sp
    b.ensure_schema()
    return b


def drop_engine(b: AuthzBackend) -> None:
    for method in ("delete_store", "delete_all"):
        fn = getattr(b, method, None)
        if callable(fn):
            fn()
    b.close()


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))] if s else float("nan")


def summary(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {}
    return {
        "n": len(xs),
        "p50": round(statistics.median(xs), 2),
        "p95": round(pct(xs, 0.95), 2),
        "max": round(max(xs), 2),
    }


# --- Part A ---------------------------------------------------------------------------------


def lag_trials(
    engine: AuthzBackend,
    index: PgVectorIndex,
    consistency: Consistency,
    strategy: Strategy,
    trials: int,
    poll_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    sync = SyncEngine(engine, index)
    retriever = AuthorizedRetriever(
        engine,
        index,
        RetrievalConfig(strategy=strategy, consistency=consistency, list_cache_ttl_s=3600),
    )
    q = [1.0] + [0.0] * 7
    granted = GroupMembers(G, frozenset({user(U), user(OTHER)}))
    revoked = GroupMembers(G, frozenset({user(OTHER)}))

    def sees() -> bool:
        return any(h.doc_id == TARGET.id for h in retriever.retrieve(U, q, 3).hits)

    sidecar, enforcement, leaked, timeouts = [], [], [], 0
    for _ in range(trials):
        sync.apply("bench", [UpsertGroup(granted)])
        deadline = time.monotonic() + timeout_s
        while True:
            # A stale (quantized) read must not get cached while we wait for the grant.
            retriever.invalidate()
            if sees():
                break
            if time.monotonic() > deadline:
                raise RuntimeError("grant never became visible")
            time.sleep(poll_s)
        sees()  # warm (and, for the cached arm, cache the list that names the target)
        rep = sync.apply("bench", [UpsertGroup(revoked, changed_at=datetime.now(UTC))])
        committed = time.perf_counter()
        sidecar.append(rep.lag_ms[0])
        n_leaked = 0
        while sees():
            n_leaked += 1
            if time.perf_counter() - committed > timeout_s:
                timeouts += 1
                break
            time.sleep(poll_s)
        enforcement.append((time.perf_counter() - committed) * 1000.0 if n_leaked else 0.0)
        leaked.append(float(n_leaked))
    return {
        "consistency": str(consistency),
        "strategy": str(strategy),
        "sidecar_lag_ms": summary(sidecar),
        "enforcement_lag_ms": summary(enforcement),
        "total_lag_ms": summary([a + b for a, b in zip(sidecar, enforcement, strict=True)]),
        "trials_with_leaks": sum(1 for x in leaked if x),
        "leaked_responses": int(sum(leaked)),
        "timeouts": timeouts,
    }


def part_a(args: argparse.Namespace) -> list[dict[str, Any]]:
    out = []
    dsn = os.environ.get("RAG_AUTHZ_PG_DSN", "postgresql://rag:rag-dev-only@127.0.0.1:55432/rag")
    for name in args.engines:
        engine = make_engine(name)
        index = PgVectorIndex(dsn, 8, table=f"lag_{time.time_ns() % 10**12}")
        index.ensure_schema()
        try:
            engine.write(
                NodeAcl(FOLDER, viewers=frozenset({members(G.id)})).tuples()
                | NodeAcl(TARGET, parent=FOLDER).tuples(),
                [],
            )
            index.upsert(
                [ChunkRecord("target#0", TARGET.id, "the target", [1.0] + [0.0] * 7)]
                + [ChunkRecord(f"noise{i}#0", f"bench/noise{i}", "", [0.1] * 8) for i in range(20)]
            )
            for consistency in args.consistency:
                for strategy in args.strategies:
                    row = {"engine": name} | lag_trials(
                        engine,
                        index,
                        Consistency(consistency),
                        Strategy(strategy),
                        args.trials,
                        args.poll_ms / 1000.0,
                        args.timeout_s,
                    )
                    print(json.dumps(row))
                    out.append(row)
        finally:
            index.drop()
            index.close()
            drop_engine(engine)
    return out


# --- Part B ---------------------------------------------------------------------------------


def part_b(args: argparse.Namespace) -> list[dict[str, Any]]:
    out = []
    for name in args.engines:
        for n in args.subtree_sizes:
            for model in ("structured", "flattened"):
                engine = make_engine(name)
                try:
                    folder = container(f"bench/{model}/{n}")
                    grant = members(G.id)
                    tuples: set[Tuple] = {Tuple(G, Relation.MEMBER, user(U))}
                    docs = [doc(f"bench/{model}/{n}/d{i}") for i in range(n)]
                    if model == "structured":
                        revoke = {Tuple(folder, Relation.VIEWER, grant)}
                        tuples |= revoke
                        for d in docs:
                            tuples |= NodeAcl(d, parent=folder).tuples()
                    else:
                        revoke = {Tuple(d, Relation.VIEWER, grant) for d in docs}
                        tuples |= revoke
                    engine.write(tuples, [])
                    before = engine.list_docs(U, Consistency.FULL).acl_ids
                    assert before == {ids.encode(d.id) for d in docs}, "setup mismatch"
                    t0 = time.perf_counter()
                    engine.write([], revoke)
                    write_ms = (time.perf_counter() - t0) * 1000.0
                    assert engine.list_docs(U, Consistency.FULL).acl_ids == frozenset()
                    row = {
                        "engine": name,
                        "docs_below": n,
                        "model": model,
                        "tuples_written": len(revoke),
                        "write_ms": round(write_ms, 1),
                    }
                    print(json.dumps(row))
                    out.append(row)
                finally:
                    drop_engine(engine)
    return out


def markdown(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> str:
    lines = [
        "### Enforcement after a revoke",
        "",
        "| engine | consistency | strategy | sidecar lag p50 / p95 ms | enforcement lag p50 / "
        "p95 / max ms | trials that leaked |",
        "|---|---|---|---:|---:|---:|",
    ]
    for r in a:
        s, e = r["sidecar_lag_ms"], r["enforcement_lag_ms"]
        lines.append(
            f"| {r['engine']} | {r['consistency']} | {r['strategy']} | {s['p50']} / {s['p95']} "
            f"| {e['p50']} / {e['p95']} / {e['max']} | {r['trials_with_leaks']}/{s['n']} |"
        )
    lines += [
        "",
        "### Revoking one folder grant",
        "",
        "| engine | docs below the folder | model | tuples written | write ms |",
        "|---|---:|---|---:|---:|",
    ]
    for r in b:
        lines.append(
            f"| {r['engine']} | {r['docs_below']:,} | {r['model']} | {r['tuples_written']:,} "
            f"| {r['write_ms']:,} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--engines", type=lambda s: s.split(","), default=["openfga", "spicedb"])
    ap.add_argument(
        "--consistency",
        type=lambda s: s.split(","),
        default=["full", "at_least_as_fresh", "minimize_latency"],
    )
    ap.add_argument(
        "--strategies",
        type=lambda s: s.split(","),
        default=["pre_filter", "post_filter", "cached_pre_filter"],
    )
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--poll-ms", type=float, default=50.0)
    ap.add_argument("--timeout-s", type=float, default=15.0)
    ap.add_argument(
        "--subtree-sizes", type=lambda s: [int(x) for x in s.split(",")], default=[10, 1000, 10000]
    )
    ap.add_argument("--out-dir", type=Path, default=ROOT / "benchmarks" / "results")
    args = ap.parse_args(argv)
    a = part_a(args)
    b = part_b(args)
    out = {
        "benchmark": "revocation_lag",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "package_version": version("rag-authz"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "args": {k: str(v) for k, v in vars(args).items()},
        "enforcement": a,
        "revoke_cost": b,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC):%Y-%m-%d}-revocation-lag"
    (args.out_dir / f"{stem}.json").write_text(json.dumps(out, indent=2) + "\n")
    md = markdown(a, b)
    (args.out_dir / f"{stem}.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
