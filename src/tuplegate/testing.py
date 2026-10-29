"""Deterministic helpers for tests, CI leak checks and keyless demos. Not for production.

``HashingEmbedder`` maps text to a bag-of-words vector by hashing tokens into buckets. It is
lexical, not semantic, but deterministic and dependency-free, which is what a leak test needs:
the question is whether unauthorised chunks come back, not how good the ranking is.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed_one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        if n == 0.0:
            v[0] = 1.0
            return v
        return [x / n for x in v]

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]
