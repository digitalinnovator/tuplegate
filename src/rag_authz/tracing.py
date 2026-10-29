"""OpenTelemetry spans when ``opentelemetry-api`` is installed; no-ops otherwise.

The library depends on the API only (never the SDK), and only through the ``server`` extra, so
an application that configures OpenTelemetry sees retrieve and sync spans, and one that does
not pays nothing (ADR-0001).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace as _trace

    _tracer: Any = _trace.get_tracer("rag_authz")
except ImportError:  # pragma: no cover - exercised only without the extra
    _tracer = None


class _NoSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    if _tracer is None:
        yield _NoSpan()
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(f"rag_authz.{k}", v)
        yield s
