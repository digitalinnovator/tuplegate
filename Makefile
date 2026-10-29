.PHONY: dev check test integration up up-full down schema bench-smoke bench bench-matrix bench-followups bench-summary bench-lag bench-sidecar demo image sidecar

COMPOSE := docker compose -f deploy/compose.yaml

# One-command setup: locked environment with every optional extra.
dev:
	uv sync --frozen --all-extras

check:
	uv run ruff check src tests benchmarks examples scripts
	uv run ruff format --check src tests benchmarks examples scripts
	uv run mypy
	uv run pytest

test:
	uv run pytest

# Needs `make up-full`. Talks to OpenFGA, SpiceDB, pgvector and Qdrant on localhost.
integration:
	uv run pytest -m integration -v

up:
	$(COMPOSE) up -d --wait postgres openfga

up-full:
	$(COMPOSE) --profile full up -d --wait

down:
	$(COMPOSE) --profile full down -v

# Re-render schema/ from src/rag_authz/schema.py (a unit test fails if they drift).
schema:
	uv run rag-authz schema --write-dir schema

# In-memory reference run: correctness and recall only; latencies are not meaningful.
bench-smoke:
	uv run python benchmarks/filter_strategies.py --preset smoke --backend memory --index exact

# The rest need `make up-full`.
bench:
	uv run python benchmarks/filter_strategies.py --preset default --backend openfga --index pgvector

# Every engine x index, three seeds, plus the single-seed arms (about 2.5 hours).
bench-matrix:
	./benchmarks/run_matrix.sh

# The skewed workload, revocation lag and sidecar overhead (about 45 minutes).
bench-followups:
	./benchmarks/run_followups.sh

# Rebuild docs/benchmark/results.md from the committed result files.
bench-summary:
	uv run python -m benchmarks.summarise

bench-lag:
	uv run python benchmarks/revocation_lag.py

bench-sidecar:
	uv run python -m benchmarks.sidecar_overhead --backend openfga --index pgvector

# Two users, one question, a revoke: the README demo.
demo:
	uv run python examples/demo.py

# The sidecar image (no push), and the sidecar on the dev stack at 127.0.0.1:58089.
image:
	docker build -f deploy/Dockerfile -t rag-authz:dev .

sidecar:
	$(COMPOSE) --profile sidecar up -d --build --wait sidecar
