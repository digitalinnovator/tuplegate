#!/usr/bin/env bash
# Runs after run_matrix.sh: the skewed workload, revocation lag, sidecar overhead.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
for backend in openfga spicedb; do
  echo "== skewed $backend"
  uv run --frozen python benchmarks/filter_strategies.py --preset skewed --backend "$backend" \
    --index pgvector --seed 7 --strategies post_filter,iterative,pre_filter,cached_pre_filter --tag s7
done
echo "== revocation lag"
uv run --frozen python benchmarks/revocation_lag.py
echo "== sidecar overhead"
uv run --frozen python -m benchmarks.sidecar_overhead --backend openfga --index pgvector
