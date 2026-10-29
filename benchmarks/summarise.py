"""Build docs/benchmark/results.md from the committed result JSONs. Numbers come only from here.

uv run python -m benchmarks.summarise [--date 2026-10-28]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks" / "results"
BUCKETS = ["<1%", "1-5%", "5-20%", "20-50%", ">=50%"]
# "auto" was measured and rejected (ADR-0004); its rows stay in the JSON, not in the tables.
STRATEGY_ORDER = [
    "unfiltered",
    "post_filter",
    "iterative",
    "pre_filter",
    "cached_pre_filter",
    "auto",
]
HIDDEN = {"auto"}


def load(date: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(RESULTS.glob(f"{date}-*.json")):
        d = json.loads(p.read_text())
        d["_file"] = p.name
        out.append(d)
    return out


def merge(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One run per configuration. A later run that re-measured some strategies (say, with a
    longer cache TTL) replaces just those rows; the files keep every measurement."""

    def key(d: dict[str, Any]) -> tuple[str, ...]:
        a, ix = d["args"], d["index"]
        return (
            d["preset"],
            d["backend"]["backend"],
            ix["index"],
            str(ix.get("iterative_scan")),
            str(ix.get("acorn")),
            a.get("consistency", "full"),
            str(d["org_stats"]["spec"]["seed"]),
            a.get("overfetch", "4"),
            a.get("max_candidates", "1000"),
        )

    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for d in sorted(runs, key=lambda d: d["created_at"]):
        k = key(d)
        if k not in merged:
            merged[k] = {**d, "results": list(d["results"]), "_files": [d["_file"]]}
            continue
        m = merged[k]
        rows = {r["label"]: r for r in m["results"]}
        for r in d["results"]:
            rows[r["label"]] = r
        m["results"] = sorted(rows.values(), key=lambda r: STRATEGY_ORDER.index(r["label"]))
        m["_files"].append(d["_file"])
        m["retrieval_config"] = d["retrieval_config"]
    for m in merged.values():
        m["results"] = [r for r in m["results"] if r["label"] not in HIDDEN]
    return list(merged.values())


def combo(d: dict[str, Any]) -> str:
    ix = d["index"]["index"]
    if ix == "pgvector" and d["index"].get("iterative_scan") not in (None, "relaxed_order"):
        ix += f" (iterative_scan={d['index']['iterative_scan']})"
    if d["index"].get("acorn"):
        ix += " (ACORN)"
    label = f"{d['backend']['backend']} + {ix}"
    consistency = d["args"].get("consistency", "full")
    return label if consistency == "full" else f"{label}, {consistency}"


def strategy_label(d: dict[str, Any], s: str) -> str:
    rc = d["retrieval_config"]
    if s == "post_filter":
        return f"post_filter (overfetch {rc['overfetch']})"
    if s == "iterative":
        return f"iterative (budget {rc['max_candidates']})"
    return s


def fmt(x: float | None, nd: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def headline(runs: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| engine + index | strategy | query leak rate | recall@10 | short results | p50 ms | "
        "p95 ms |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for d in runs:
        for r in d["results"]:
            o = r["overall"]
            lines.append(
                f"| {combo(d)} | {strategy_label(d, r['label'])} | {o['query_leak_rate']:.3f} | "
                f"{fmt(o['recall_mean'])} | {o['short_rate']:.2f} | {o['latency_p50_ms']:.1f} | "
                f"{o['latency_p95_ms']:.1f} |"
            )
    return lines


def buckets(d: dict[str, Any]) -> list[str]:
    lines = [
        "| strategy | " + " | ".join(BUCKETS) + " |",
        "|---|" + "---:|" * len(BUCKETS),
    ]
    for r in d["results"]:
        cells = []
        for b in BUCKETS:
            s = r["by_bucket"].get(b)
            cells.append(
                "-"
                if s is None or s["recall_mean"] is None
                else f"{s['recall_mean']:.2f} / {s['latency_p95_ms']:.0f} ms (n={s['probes']})"
            )
        lines.append(f"| {strategy_label(d, r['label'])} | " + " | ".join(cells) + " |")
    return lines


def variance(runs: list[dict[str, Any]]) -> list[str]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seeds: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in runs:
        for r in d["results"]:
            groups[(combo(d), r["label"])].append(r["overall"])
            seeds[(combo(d), r["label"])].append(str(d["org_stats"]["spec"]["seed"]))
    lines = [
        "| engine + index | strategy | seeds | recall@10 median (min to max) | p95 ms median "
        "(min to max) | leaking queries (all seeds) |",
        "|---|---|---|---:|---:|---:|",
    ]
    for (c, s), os_ in sorted(groups.items()):
        if len(os_) < 2:
            continue
        rec = [o["recall_mean"] for o in os_ if o["recall_mean"] is not None]
        p95 = [o["latency_p95_ms"] for o in os_]
        leaks = sum(round(o["query_leak_rate"] * o["probes"]) for o in os_)
        rec_s = f"{statistics.median(rec):.3f} ({min(rec):.3f} to {max(rec):.3f})" if rec else "n/a"
        lines.append(
            f"| {c} | {s} | {','.join(seeds[(c, s)])} | {rec_s} | {statistics.median(p95):.0f} "
            f"({min(p95):.0f} to {max(p95):.0f}) | {leaks if s != 'unfiltered' else '(baseline)'} |"
        )
    return lines


def skewed_section(runs: list[dict[str, Any]]) -> list[str]:
    if not runs:
        return []
    st = runs[0]["org_stats"]
    out = [
        "## Broad-reach users (`--preset skewed`, seed 7)",
        "",
        "Ten times the company-wide content, heavier power-user skew and bigger top groups, with "
        "probes stratified across selectivity buckets. Per-user selectivity: median "
        f"{st['selectivity_p50']:.1%}, p90 {st['selectivity_p90']:.1%}, max "
        f"{st['selectivity_max']:.1%}.",
        "",
        *headline(runs),
        "",
    ]
    for d in runs:
        out += [f"Recall and p95 latency by selectivity, {combo(d)}:", "", *buckets(d), ""]
    return out


def lag_section(d: dict[str, Any] | None) -> list[str]:
    if d is None:
        return []
    from benchmarks.revocation_lag import markdown as lag_markdown

    a = d["args"]
    return [
        "## Revocation",
        "",
        f"`benchmarks/revocation_lag.py`: {a['trials']} trials per row, polling every "
        f"{a['poll_ms']} ms. Sidecar lag is change observed to engine write committed; "
        "enforcement lag is write committed to first compliant response. Source-feed lag is "
        "not included (see ADR-0005).",
        "",
        lag_markdown(d["enforcement"], d["revoke_cost"]),
        "",
    ]


def sidecar_section(runs: list[dict[str, Any]]) -> list[str]:
    if not runs:
        return []
    out = [
        "## Sidecar overhead (library call vs HTTP, same retriever)",
        "",
        "| engine + index | probes | library p50 / p95 ms | sidecar p50 / p95 ms | overhead p50 "
        "/ p95 ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for d in runs:
        a = d["args"]
        lib, side, ov = d["library_ms"], d["sidecar_ms"], d["overhead_ms"]
        out.append(
            f"| {a['backend']} + {a['index']} | {d['probes']} | {lib['p50']:.1f} / "
            f"{lib['p95']:.1f} | {side['p50']:.1f} / {side['p95']:.1f} | {ov['p50']:.1f} / "
            f"{ov['p95']:.1f} |"
        )
    return [*out, ""]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-28")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "benchmark" / "results.md")
    args = ap.parse_args()
    everything = load(args.date)
    all_runs = [d for d in everything if d.get("benchmark") == "filter_strategies"]
    runs = merge([d for d in all_runs if d["preset"] == "default"])
    skewed = merge([d for d in all_runs if d["preset"] == "skewed"])
    lag = next((d for d in everything if d.get("benchmark") == "revocation_lag"), None)
    sidecar = [d for d in everything if d.get("benchmark") == "sidecar_overhead"]
    if not runs:
        raise SystemExit("no results")
    seed7 = [
        d
        for d in runs
        if d["org_stats"]["spec"]["seed"] == 7
        and d["args"].get("consistency") == "full"
        and d["args"].get("overfetch") == "4"
        and d["args"].get("iterative_scan") == "relaxed_order"
        and d["args"].get("acorn") == "False"
    ]
    arms = [d for d in runs if d not in seed7 and d["org_stats"]["spec"]["seed"] == 7]
    main_run = next(
        (d for d in seed7 if combo(d) == "openfga + pgvector"), seed7[0] if seed7 else runs[0]
    )
    spec = main_run["org_stats"]["spec"]
    st = main_run["org_stats"]
    out = [
        "# Benchmark results",
        "",
        "Generated by `python -m benchmarks.summarise` from "
        f"`benchmarks/results/{args.date}-*.json`. "
        "Do not edit by hand. Protocol and metric definitions: [plan.md](plan.md).",
        "",
        "## Setup",
        "",
        f"- **Organisation** (`--preset default`): {spec['users']:,} users, {spec['groups']} "
        f"groups, {spec['docs']:,} docs, {st['chunks']:,} chunks, {st['tuples']:,} tuples, "
        f"{spec['queries']} probes (half aimed at topics the user cannot see), k = 10.",
        f"- **Per-user selectivity** (share of docs a user can see): p10 "
        f"{st['selectivity_p10']:.1%}, median {st['selectivity_p50']:.1%}, p90 "
        f"{st['selectivity_p90']:.1%}, max {st['selectivity_max']:.1%}.",
        f"- **Machine:** {main_run['platform']}, {main_run['cpu_count']} CPUs; every service in "
        "Docker on localhost (`deploy/compose.yaml`, images pinned by digest), one run at a time.",
        f"- **Software:** rag-authz {main_run['package_version']}, Python {main_run['python']}.",
        "- **Consistency:** `full` unless stated: OpenFGA `HIGHER_CONSISTENCY`, SpiceDB "
        "`fully_consistent`.",
        "- **`cached_pre_filter`** is measured with a warm cache (every probe user's list cached, "
        "TTL longer than the run); a cache miss costs one `pre_filter`. Its first measurement "
        "used a 60 s TTL that expired mid-run on slow lists; the re-run with a 3,600 s TTL "
        "replaces those rows (both are in the files).",
        "",
        "## Every engine and index (seed 7)",
        "",
        *headline(seed7),
        "",
        "## Recall and p95 latency by user selectivity (openfga + pgvector, seed 7)",
        "",
        *buckets(main_run),
        "",
        "## Seed variance",
        "",
        *variance([d for d in runs if d in seed7 or d["org_stats"]["spec"]["seed"] in (8, 9)]),
        "",
        "## Single-seed arms",
        "",
        *headline(arms),
        "",
        *skewed_section(skewed),
        *lag_section(lag),
        *sidecar_section(sidecar),
        "## Files",
        "",
        *[f"- `benchmarks/results/{d['_file']}`" for d in everything],
        "",
    ]
    args.out.write_text("\n".join(out))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
