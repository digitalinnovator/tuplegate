#!/usr/bin/env bash
# Re-measures the cached arms with a TTL longer than the run (the first matrix used 60 s, which
# expired mid-run where lists are slow), adds the auto strategy, and re-runs the skewed preset.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
for backend in openfga spicedb; do
  for index in pgvector qdrant; do
    echo "== default $backend $index"
    uv run --frozen python benchmarks/filter_strategies.py --preset default --backend "$backend" \
      --index "$index" --seed 7 --strategies cached_pre_filter,auto --tag s7-cache
  done
done
for backend in openfga spicedb; do
  echo "== skewed $backend"
  uv run --frozen python benchmarks/filter_strategies.py --preset skewed --backend "$backend" \
    --index pgvector --seed 7 --strategies post_filter,iterative,pre_filter,cached_pre_filter,auto \
    --tag s7
done
