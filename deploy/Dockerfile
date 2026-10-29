# The tuplegate sidecar: the enforcement boundary (ADR-0001). Built from uv.lock; no dev or
# notebook dependencies. Base images are pinned by digest; bump them in a deliberate commit.
#
#   docker build -f deploy/Dockerfile -t tuplegate:dev .
#   docker run --rm -p 8089:8089 --env-file sidecar.env tuplegate:dev

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_BUILD=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --group image --no-install-project \
      --extra server --extra pgvector --extra spicedb
COPY src ./src
# The project itself is the one package built here (hatchling, from the lock's pinned version).
RUN UV_NO_BUILD=0 uv sync --frozen --no-dev --group image --no-editable \
      --no-build-package grpcio --no-build-package psycopg-binary \
      --extra server --extra pgvector --extra spicedb

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
RUN useradd --system --uid 10001 --no-create-home rag
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8089
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8089/healthz', timeout=2)"]
ENTRYPOINT ["tuplegate", "serve", "--host", "0.0.0.0", "--port", "8089"]
