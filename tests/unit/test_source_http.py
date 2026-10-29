from __future__ import annotations

import httpx
import pytest

from rag_authz.connectors._http import SourceHttp


@pytest.mark.parametrize("retry_after", ["-5", "nan", "inf", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_odd_retry_after_values_fall_back_to_backoff(retry_after: str) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"ok": True})

    slept: list[float] = []
    http = SourceHttp(httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append)
    assert http.json("https://x.test/y") == {"ok": True}
    assert slept == [1.0]
