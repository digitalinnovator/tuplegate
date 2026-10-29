"""HTTP plumbing shared by the Drive and Confluence connectors: auth headers, retries, errors.

Retries cover rate limiting and transient server errors only (429, 500, 502, 503, 504), with
exponential backoff that honours ``Retry-After``. Anything else raises ``ConnectorError``, which
aborts the snapshot, so the sync engine skips its sweep (ADR-0002 §8).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

RETRYABLE = frozenset({429, 500, 502, 503, 504})


class ConnectorError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SourceHttp:
    def __init__(
        self,
        client: httpx.Client,
        *,
        auth_header: Callable[[], Mapping[str, str]] | None = None,
        max_retries: int = 5,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = client
        self._auth = auth_header
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self.requests = 0
        self.retries = 0

    def get(self, url: str, params: Any = None) -> httpx.Response:
        """GET with retries. Returns the final response, whatever its status."""
        attempt = 0
        while True:
            headers = dict(self._auth()) if self._auth else {}
            try:
                r = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                if attempt >= self._max_retries:
                    raise ConnectorError(f"GET {url}: {e}") from e
                r = None
            self.requests += 1
            if r is not None and (r.status_code not in RETRYABLE or attempt >= self._max_retries):
                return r
            self._sleep(self._delay(r, attempt))
            self.retries += 1
            attempt += 1

    def json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        r = self.get(url, params)
        if r.status_code >= 400:
            raise ConnectorError(f"GET {url}: HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json()

    def _delay(self, r: httpx.Response | None, attempt: int) -> float:
        if r is not None:
            ra = r.headers.get("Retry-After")
            if ra is not None:
                try:
                    seconds = float(ra)
                except ValueError:
                    seconds = -1.0
                # Ignore negative, NaN and infinite values (an HTTP date also lands here).
                if math.isfinite(seconds) and seconds >= 0:
                    return min(seconds, 60.0)
        return float(min(self._backoff_s * (2**attempt), 60.0))

    def close(self) -> None:
        self._http.close()
