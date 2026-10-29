"""Sidecar overhead (benchmark plan Q5): the same retrieve call in library mode and over HTTP.

Both modes share one engine, one index and one retriever; the sidecar runs in-process under
uvicorn on localhost, so the difference is the HTTP hop (serialisation, auth, the ASGI stack),
not network distance. Probes alternate between the modes to cancel drift.

    uv run python benchmarks/sidecar_overhead.py --backend openfga --index pgvector
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import sys
import threading
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx
import uvicorn

from benchmarks.filter_strategies import PRESETS, build_backend, build_index, teardown
from tuplegate.retrieval import AuthorizedRetriever, RetrievalConfig, Strategy
from tuplegate.server import Callers, create_app, hash_token
from tuplegate.synth import SyntheticTruth, generate

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preset", choices=sorted(PRESETS), default="smoke")
    ap.add_argument("--backend", choices=["openfga", "spicedb"], default="openfga")
    ap.add_argument("--index", choices=["pgvector", "qdrant"], default="pgvector")
    ap.add_argument("--strategy", default="pre_filter")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=30)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "benchmarks" / "results")
    args = ap.parse_args(argv)

    org = generate(PRESETS[args.preset])
    backend, _ = build_backend(args.backend, org)
    index, _ = build_index(args.index, org, "relaxed_order")
    strategy = Strategy(args.strategy)
    retriever = AuthorizedRetriever(backend, index, RetrievalConfig(strategy=strategy))
    token = "bench-token"
    app = create_app(retriever, Callers({hash_token(token): "bench"}))
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}"}
    )
    probes = SyntheticTruth(org, args.k).probes()

    def lib(p: object) -> float:
        t0 = time.perf_counter()
        retriever.retrieve(p.user, p.vector, args.k)  # type: ignore[attr-defined]
        return (time.perf_counter() - t0) * 1000.0

    def http(p: object) -> float:
        body = {"subject": p.user, "vector": [float(x) for x in p.vector], "k": args.k}  # type: ignore[attr-defined]
        t0 = time.perf_counter()
        r = client.post("/v1/retrieve", json=body)
        ms = (time.perf_counter() - t0) * 1000.0
        r.raise_for_status()
        return ms

    for p in probes[: args.warmup]:
        lib(p)
        http(p)
    lib_ms, http_ms = [], []
    for i, p in enumerate(probes):
        if i % 2:
            lib_ms.append(lib(p))
            http_ms.append(http(p))
        else:
            http_ms.append(http(p))
            lib_ms.append(lib(p))
    server.should_exit = True
    thread.join(timeout=10)
    client.close()
    teardown(backend, index)

    deltas = [h - lib for h, lib in zip(http_ms, lib_ms, strict=True)]
    result = {
        "benchmark": "sidecar_overhead",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "package_version": version("tuplegate"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "args": {k: str(v) for k, v in vars(args).items()},
        "probes": len(probes),
        "library_ms": {"p50": statistics.median(lib_ms), "p95": pct(lib_ms, 0.95)},
        "sidecar_ms": {"p50": statistics.median(http_ms), "p95": pct(http_ms, 0.95)},
        "overhead_ms": {"p50": statistics.median(deltas), "p95": pct(deltas, 0.95)},
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC):%Y-%m-%d}-sidecar-overhead-{args.backend}-{args.index}"
    (args.out_dir / f"{stem}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
