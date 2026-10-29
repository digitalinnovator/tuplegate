"""Track container nesting so connectors can report how deep a source's hierarchy goes.

Engines cap resolution depth, and past the cap they fail closed: measured on our schema,
OpenFGA v1.21.0 Check denies docs below 25 or more containers, and SpiceDB v1.56.2
LookupResources fails the whole request from 22 (docs/research/2026-10-28-resolution-depth.md).
A source deeper than that loses recall, so connectors report the depth they see.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# The shallowest measured limit (SpiceDB LookupResources: 21 containers resolve, 22 fail).
WARN_DEPTH = 21


class DepthTracker:
    def __init__(self) -> None:
        self._parent: dict[str, str | None] = {}

    def add(self, container_id: str, parent_id: str | None) -> None:
        self._parent[container_id] = parent_id

    def max_depth(self) -> int:
        """The longest chain of containers, counting both ends (a root alone is 1)."""
        memo: dict[str, int] = {}
        for start in self._parent:
            path: list[str] = []
            node: str | None = start
            seen: set[str] = set()
            while node is not None and node not in memo and node not in seen:
                seen.add(node)
                path.append(node)
                node = self._parent.get(node)
            base = memo.get(node, 0) if node is not None and node not in seen else 0
            for n in reversed(path):
                base += 1
                memo[n] = base
        return max(memo.values(), default=0)

    def report(self, source: str) -> int:
        depth = self.max_depth()
        if depth > WARN_DEPTH:
            log.warning(
                "hierarchy deeper than engines resolve; deep docs will be denied (fail closed)",
                extra={"source": source, "max_depth": depth, "warn_depth": WARN_DEPTH},
            )
        return depth
