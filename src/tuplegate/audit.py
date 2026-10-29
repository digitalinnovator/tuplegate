"""One structured audit record per retrieval authorization decision.

Records carry ids, never content: the subject, the strategy, which docs were returned and
which candidate docs were denied (denials are how probing shows up). Sinks are pluggable; the
default JSON-lines sink writes one object per line for any log pipeline.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from tuplegate.authz.base import Consistency
    from tuplegate.index.base import Hit
    from tuplegate.retrieval import RetrievalStats


# The authenticated application behind the current request (the sidecar sets it per request;
# library callers may set it too). Context variables follow requests into worker threads.
caller: ContextVar[str | None] = ContextVar("tuplegate_caller", default=None)


@dataclass(frozen=True)
class AuditRecord:
    ts: str
    event: str
    request_id: str
    subject: str
    strategy: str
    consistency: str
    k: int
    returned_doc_ids: list[str]
    denied_acl_ids: list[str]
    candidates: int
    authz_calls: int
    allowed_docs: int | None
    list_complete: bool
    total_ms: float
    error: str | None
    caller: str | None = None

    @classmethod
    def from_decision(
        cls,
        *,
        request_id: str,
        subject: str,
        k: int,
        consistency: Consistency,
        hits: Sequence[Hit],
        stats: RetrievalStats,
        error: str | None,
        caller: str | None = None,
    ) -> AuditRecord:
        return cls(
            ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
            event="retrieval.decision",
            request_id=request_id,
            subject=subject,
            strategy=str(stats.strategy),
            consistency=str(consistency),
            k=k,
            returned_doc_ids=list(dict.fromkeys(h.doc_id for h in hits)),
            denied_acl_ids=list(stats.denied_acl_ids),
            candidates=stats.candidates,
            authz_calls=stats.authz_calls,
            allowed_docs=stats.allowed_docs,
            list_complete=stats.list_complete,
            total_ms=round(stats.total_ms, 3),
            error=error,
            caller=caller,
        )


class AuditSink(Protocol):
    def emit(self, record: AuditRecord) -> None: ...


class NullAuditSink:
    def emit(self, record: AuditRecord) -> None:
        return None


class MemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


class JsonLinesAuditSink:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, record: AuditRecord) -> None:
        line = json.dumps(asdict(record), separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
