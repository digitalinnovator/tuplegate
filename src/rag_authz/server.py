"""The sidecar: one authorised retrieve call over HTTP (``pip install rag-authz[server]``).

This process is the enforcement boundary (ADR-0001, sidecar mode): it holds the engine and
vector-store credentials, and applications hold only a sidecar token. v0.1 trusts an
authenticated caller to assert the end user's identity; verifying the end user's OIDC token is
on the roadmap.

    POST /v1/retrieve   {"subject": "alice@acme.test", "vector": [...], "k": 5}
    GET  /healthz       liveness
    GET  /readyz        engine and index reachable

Failure modes, all closed:

- An engine or index error returns 503 with no hits (and an audit record with the error).
- An invalid subject (empty, ``*``, or an unmapped ``~source/...`` principal) returns 400.
- ``strategy: "unfiltered"`` is refused with 403, whatever the retriever's configuration.

Run it with ``rag-authz serve`` (configuration from environment variables, see ``Settings``).
"""

# No ``from __future__ import annotations`` here: FastAPI resolves endpoint annotations at
# runtime, and postponed ones cannot see the closures that create_app defines.
import hashlib
import hmac
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from rag_authz import audit
from rag_authz.audit import JsonLinesAuditSink
from rag_authz.authz.base import AuthzError, Consistency
from rag_authz.config import Settings, build_engine, build_index
from rag_authz.retrieval import AuthorizedRetriever, RetrievalConfig, Strategy

log = logging.getLogger(__name__)

Embedder = Callable[[str], Sequence[float]]

# --- API models ------------------------------------------------------------------------------


class RetrieveRequest(BaseModel):
    subject: str = Field(
        min_length=1, max_length=512, description="The end user, as the IdP names them"
    )
    vector: list[float] | None = Field(default=None, description="Query embedding")
    query: str | None = Field(default=None, description="Query text; needs a server-side embedder")
    k: int = Field(default=5, ge=1, le=100)
    strategy: Literal["pre_filter", "post_filter", "iterative", "cached_pre_filter"] | None = None


class HitOut(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class StatsOut(BaseModel):
    strategy: str
    candidates: int
    authz_calls: int
    allowed_docs: int | None
    list_complete: bool
    authz_ms: float
    search_ms: float
    total_ms: float


class RetrieveResponse(BaseModel):
    request_id: str
    hits: list[HitOut]
    stats: StatsOut


# --- app -------------------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Callers:
    """Caller name by SHA-256 of its bearer token. Tokens themselves are never stored."""

    by_hash: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_spec(cls, spec: str) -> "Callers":
        """``name:sha256hex,name2:sha256hex`` (generate a hash with ``rag-authz hash-token``)."""
        out: dict[str, str] = {}
        for part in filter(None, (p.strip() for p in spec.split(","))):
            name, _, digest = part.partition(":")
            if not name or len(digest) != 64:
                raise ValueError(f"bad caller entry {part!r}; want name:sha256hex")
            out[digest.lower()] = name
        return cls(out)

    def identify(self, token: str) -> str | None:
        digest = hash_token(token)
        found = None
        for h, name in self.by_hash.items():
            # Compare against every entry so timing does not reveal which prefix matched.
            if hmac.compare_digest(h, digest):
                found = name
        return found


def create_app(
    retriever: AuthorizedRetriever,
    callers: Callers,
    *,
    embedder: Embedder | None = None,
    ready: Callable[[], None] | None = None,
) -> FastAPI:
    if not callers.by_hash:
        raise ValueError("refusing to start without at least one caller token")
    app = FastAPI(title="rag-authz", version="0.1.0", docs_url=None, redoc_url=None)

    def authenticate(request: Request) -> str:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        name = callers.identify(token) if scheme.lower() == "bearer" and token else None
        if name is None:
            raise HTTPException(401, "missing or invalid bearer token")
        return name

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            if ready is not None:
                ready()
        except Exception as e:
            log.warning("not ready: %s", e)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/v1/retrieve", response_model=RetrieveResponse)
    def retrieve(
        body: RetrieveRequest, caller_name: Annotated[str, Depends(authenticate)]
    ) -> RetrieveResponse:
        if body.vector is None:
            if body.query is None or embedder is None:
                raise HTTPException(422, "send a vector, or a query to a sidecar with an embedder")
            vector: Sequence[float] = embedder(body.query)
        else:
            vector = body.vector
        token = audit.caller.set(caller_name)
        try:
            result = retriever.retrieve(
                body.subject,
                vector,
                body.k,
                strategy=Strategy(body.strategy) if body.strategy else None,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except AuthzError as e:
            log.error("authorization engine error: %s", e)
            raise HTTPException(503, "authorization engine unavailable; nothing returned") from e
        except Exception as e:
            log.exception("retrieval failed")
            raise HTTPException(503, "retrieval failed; nothing returned") from e
        finally:
            audit.caller.reset(token)
        st = result.stats
        return RetrieveResponse(
            request_id=result.request_id,
            hits=[
                HitOut(
                    chunk_id=h.chunk_id,
                    doc_id=h.doc_id,
                    text=h.text,
                    score=h.score,
                    metadata=dict(h.metadata),
                )
                for h in result.hits
            ],
            stats=StatsOut(
                strategy=str(st.strategy),
                candidates=st.candidates,
                authz_calls=st.authz_calls,
                allowed_docs=st.allowed_docs,
                list_complete=st.list_complete,
                authz_ms=round(st.authz_ms, 3),
                search_ms=round(st.search_ms, 3),
                total_ms=round(st.total_ms, 3),
            ),
        )

    return app


def app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory rag_authz.server:app_from_env``."""
    s = Settings.from_env()
    if not s.callers:
        raise ValueError("RAG_AUTHZ_CALLERS is required (see `rag-authz hash-token`)")
    engine, index = build_engine(s), build_index(s)
    stream = sys.stdout if s.audit_log == "-" else open(s.audit_log, "a", encoding="utf-8")  # noqa: SIM115
    retriever = AuthorizedRetriever(
        engine,
        index,
        RetrievalConfig(strategy=Strategy(s.strategy), consistency=Consistency(s.consistency)),
        JsonLinesAuditSink(stream),
    )

    def ready() -> None:
        engine.check_docs("readyz-probe", ["readyz-probe"], Consistency.MINIMIZE_LATENCY)
        index.count()

    return create_app(retriever, Callers.from_spec(s.callers), ready=ready)
