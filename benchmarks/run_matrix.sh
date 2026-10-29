#!/usr/bin/env bash
# The engine-backed benchmark matrix behind the published tables (docs/benchmark/results.md).
# Needs `make up-full`. Runs sequentially: concurrent runs would distort each other's latency.
set -euo pipefail
cd "$(dirname "$0")/.."
PRESET="${PRESET:-default}"
export PYTHONUNBUFFERED=1
run() { echo "== $*"; uv run --frozen python benchmarks/filter_strategies.py --preset "$PRESET" "$@"; }

# Every engine x index on seed 7.
for backend in openfga spicedb; do
  for index in pgvector qdrant; do
    run --backend "$backend" --index "$index" --seed 7 --tag s7
  done
done
# Seed variance on the two diagonal combinations.
for seed in 8 9; do
  run --backend openfga --index pgvector --seed "$seed" --tag "s$seed"
  run --backend spicedb --index qdrant --seed "$seed" --tag "s$seed"
done

# Single-seed arms (seed 7).
run --backend openfga --index pgvector --iterative-scan off --strategies pre_filter --tag s7
run --backend openfga --index qdrant --acorn --strategies pre_filter,post_filter --tag s7
for backend in openfga spicedb; do
  run --backend "$backend" --index pgvector --consistency minimize_latency \
    --strategies post_filter,pre_filter --tag s7
done
for of in 2 10; do
  run --backend openfga --index pgvector --overfetch "$of" --strategies post_filter --tag "s7-overfetch$of"
done
