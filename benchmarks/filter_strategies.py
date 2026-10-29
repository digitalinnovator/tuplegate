"""Filtering-strategy benchmark: leak rate, recall@k and latency under group-membership skew.

Protocol: docs/benchmark/plan.md. Every number published from this project comes from this
script (or its successors) with the JSON it writes committed next to the claim.

    uv run python benchmarks/filter_strategies.py --preset smoke --backend memory --index exact
    uv run python benchmarks/filter_strategies.py --preset default \
        --backend openfga --index pgvector
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from tuplegate import leakcheck
from tuplegate.authz.base import AuthzBackend, Consistency
from tuplegate.authz.memory import MemoryBackend
from tuplegate.index.base import ChunkRecord, VectorIndex
from tuplegate.retrieval import AuthorizedRetriever, RetrievalConfig, Strategy
from tuplegate.synth import OrgSpec, SyntheticOrg, SyntheticTruth, generate

PRESETS: dict[str, OrgSpec] = {
    "smoke": OrgSpec(users=500, groups=60, roots=30, docs=3_000, queries=200),
    "default": OrgSpec(users=2_000, groups=200, roots=80, docs=10_000, queries=500),
    "medium": OrgSpec(users=5_000, groups=500, roots=150, docs=30_000, queries=500),
    # Broad-reach users exist and are probed: more company-wide content, heavier power-user
    # skew, bigger top groups, and probes stratified across selectivity buckets.
    "skewed": OrgSpec(
        users=2_000,
        groups=200,
        roots=80,
        docs=10_000,
        queries=500,
        company_wide_doc_share=0.10,
        membership_alpha=1.5,
        largest_group_fraction=0.6,
        group_size_alpha=0.8,
        probe_sampling="stratified",
    ),
}
STRATEGIES = [
    Strategy.UNFILTERED,
    Strategy.POST_FILTER,
    Strategy.ITERATIVE,
    Strategy.PRE_FILTER,
    Strategy.CACHED_PRE_FILTER,
]
ROOT = Path(__file__).resolve().parents[1]


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
        )
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def build_backend(name: str, org: SyntheticOrg) -> tuple[AuthzBackend, dict[str, Any]]:
    t0 = time.perf_counter()
    if name == "memory":
        b: AuthzBackend = MemoryBackend(org.tuples)
    elif name == "openfga":
        from tuplegate.authz.openfga import OpenFGABackend

        url = os.environ.get("TUPLEGATE_OPENFGA_URL", "http://127.0.0.1:58080")
        fga = OpenFGABackend(url, store_name=f"bench-{int(time.time())}")
        fga.ensure_schema()
        fga.write(org.tuples, [])
        b = fga
    elif name == "spicedb":
        from tuplegate.authz.spicedb import SpiceDBBackend

        sp = SpiceDBBackend(
            os.environ.get("TUPLEGATE_SPICEDB_ENDPOINT", "127.0.0.1:55051"),
            os.environ.get("TUPLEGATE_SPICEDB_TOKEN", "rag-dev-only"),
            insecure=True,
        )
        sp.ensure_schema()
        sp.delete_all()  # SpiceDB has no stores: the benchmark owns the dev instance
        sp.write(org.tuples, [])
        b = sp
    else:
        raise SystemExit(f"unknown backend {name}")
    return b, {"backend": name, "load_s": round(time.perf_counter() - t0, 2)}


def teardown(backend: AuthzBackend, index: VectorIndex) -> None:
    for obj, method in ((backend, "delete_store"), (backend, "delete_all"), (index, "drop")):
        fn = getattr(obj, method, None)
        if callable(fn):
            fn()
    backend.close()
    index.close()


def build_index(
    name: str, org: SyntheticOrg, iterative_scan: str, acorn: bool = False
) -> tuple[VectorIndex, dict[str, Any]]:
    t0 = time.perf_counter()
    rows = [
        ChunkRecord(c, d, t, v.tolist())
        for c, d, t, v in zip(
            org.chunk_ids, org.chunk_doc, org.chunk_text, org.vectors, strict=True
        )
    ]
    if name == "exact":
        from tuplegate.index.exact import ExactIndex

        ix: VectorIndex = ExactIndex(org.spec.dim)
    elif name == "pgvector":
        from tuplegate.index.pgvector import PgVectorIndex

        dsn = os.environ.get(
            "TUPLEGATE_PG_DSN", "postgresql://rag:rag-dev-only@127.0.0.1:55432/rag"
        )
        ix = PgVectorIndex(
            dsn, org.spec.dim, table=f"bench_{int(time.time())}", iterative_scan=iterative_scan
        )  # type: ignore[arg-type]
    elif name == "qdrant":
        from tuplegate.index.qdrant import QdrantIndex

        ix = QdrantIndex(
            os.environ.get("TUPLEGATE_QDRANT_URL", "http://127.0.0.1:56333"),
            org.spec.dim,
            collection=f"bench_{int(time.time())}",
            acorn=acorn,
        )
    else:
        raise SystemExit(f"unknown index {name}")
    ix.ensure_schema()
    for i in range(0, len(rows), 1000):
        ix.upsert(rows[i : i + 1000])
    return ix, {
        "index": name,
        "iterative_scan": iterative_scan if name == "pgvector" else None,
        "acorn": acorn if name == "qdrant" else None,
        "load_s": round(time.perf_counter() - t0, 2),
    }


def markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "| strategy | query leak rate | result leak rate | recall@k | short results "
        "| p50 ms | p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        o = r["overall"]
        rec = "n/a" if o["recall_mean"] is None else f"{o['recall_mean']:.3f}"
        lines.append(
            f"| {r['label']} | {o['query_leak_rate']:.3f} | {o['result_leak_rate']:.3f} | {rec} | "
            f"{o['short_rate']:.2f} | {o['latency_p50_ms']:.1f} | {o['latency_p95_ms']:.1f} |"
        )
    lines += ["", "Recall@k by user selectivity (share of docs the user can see):", ""]
    buckets = [b[0] for b in leakcheck.SELECTIVITY_BUCKETS]
    lines.append("| strategy | " + " | ".join(buckets) + " |")
    lines.append("|---|" + "---:|" * len(buckets))
    for r in results:
        cells = []
        for b in buckets:
            s = r["by_bucket"].get(b)
            cells.append(
                "-"
                if s is None or s["recall_mean"] is None
                else f"{s['recall_mean']:.3f} (n={s['probes']})"
            )
        lines.append(f"| {r['label']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--preset", choices=sorted(PRESETS), default="smoke")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--backend", choices=["memory", "openfga", "spicedb"], default="memory")
    ap.add_argument("--index", choices=["exact", "pgvector", "qdrant"], default="exact")
    ap.add_argument("--acorn", action="store_true", help="Qdrant: enable ACORN search")
    ap.add_argument(
        "--strategies",
        default=",".join(s.value for s in STRATEGIES),
        help="comma-separated subset, in run order",
    )
    # Longer than any run, so the cached arms measure a warm cache (the cold path is pre_filter).
    ap.add_argument("--list-cache-ttl", type=float, default=3600.0)
    ap.add_argument("--keep", action="store_true", help="keep the engine store and index")
    ap.add_argument("--tag", default="", help="suffix for the result file names")
    ap.add_argument(
        "--iterative-scan",
        choices=["off", "relaxed_order", "strict_order"],
        default="relaxed_order",
    )
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--overfetch", type=int, default=4)
    ap.add_argument("--max-candidates", type=int, default=1000)
    ap.add_argument("--consistency", choices=[c.value for c in Consistency], default="full")
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "benchmarks" / "results")
    args = ap.parse_args(argv)

    spec = PRESETS[args.preset]
    if args.seed is not None:
        spec = replace(spec, seed=args.seed)
    t0 = time.perf_counter()
    org = generate(spec)
    print(
        f"generated org in {time.perf_counter() - t0:.1f}s:",
        json.dumps({k: v for k, v in org.stats.items() if k != "spec"}),
    )
    backend, bmeta = build_backend(args.backend, org)
    index, imeta = build_index(args.index, org, args.iterative_scan, args.acorn)
    truth = SyntheticTruth(org, args.k)
    probes = truth.probes()
    config = RetrievalConfig(
        consistency=Consistency(args.consistency),
        overfetch=args.overfetch,
        max_candidates=args.max_candidates,
        list_cache_ttl_s=args.list_cache_ttl,
        allow_unfiltered=True,
    )
    engine = AuthorizedRetriever(backend, index, config)
    results = []
    for strategy in [Strategy(x) for x in args.strategies.split(",")]:
        for p in probes[: args.warmup]:
            engine.retrieve(p.user, p.vector, args.k, strategy=strategy)
        if strategy is Strategy.CACHED_PRE_FILTER:
            # Steady state: every probe user's list is cached (the TTL outlasts the run). The
            # cold path is the pre_filter row.
            for p in {p.user: p for p in probes}.values():
                engine.retrieve(p.user, p.vector, args.k, strategy=strategy)
        report = leakcheck.run(
            strategy.value,
            lambda p, k, s=strategy: engine.retrieve(p.user, p.vector, k, strategy=s).hits,
            probes,
            truth,
            args.k,
        )
        results.append(report.to_dict())
        o = report.overall
        print(
            f"{strategy.value:12s} leak_q={o.query_leak_rate:.3f} recall={o.recall_mean or 0:.3f} "
            f"short={o.short_rate:.2f} p95={o.latency_p95_ms:.1f}ms errors={report.errors}"
        )

    if not args.keep:
        teardown(backend, index)
    latency_meaningful = args.backend != "memory"
    out = {
        "benchmark": "filter_strategies",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "package_version": version("tuplegate"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "preset": args.preset,
        "args": {k: str(v) for k, v in vars(args).items()},
        "org_stats": org.stats,
        "backend": bmeta,
        "index": imeta,
        "retrieval_config": {k: str(v) for k, v in asdict(config).items()},
        "latency_meaningful": latency_meaningful,
        "results": results,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    variant = args.index
    if args.index == "pgvector" and args.iterative_scan != "relaxed_order":
        variant += f"-{args.iterative_scan}"
    if args.acorn:
        variant += "-acorn"
    stem = f"{datetime.now(UTC):%Y-%m-%d}-{args.preset}-{args.backend}-{variant}"
    if args.consistency != "full":
        stem += f"-{args.consistency}"
    if args.tag:
        stem += f"-{args.tag}"
    (args.out_dir / f"{stem}.json").write_text(json.dumps(out, indent=2) + "\n")
    md = markdown(results)
    if not latency_meaningful:
        md = (
            "> In-memory reference run: leak rate and recall are meaningful; latency is not.\n\n"
            + md
        )
    (args.out_dir / f"{stem}.md").write_text(md)
    print(md)
    print(f"wrote {args.out_dir / stem}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
