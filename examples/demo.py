"""Two users ask the same question and get different answers. Then one loses access.

Runs against the dev stack (`make up-full`) by default; `--memory` needs no services.

    uv run python examples/demo.py                        # OpenFGA + pgvector
    uv run python examples/demo.py --engine spicedb --index qdrant
    uv run python examples/demo.py --memory

The "source" is a small organisation written as connector events, so the demo needs no
credentials. Real connectors (GitHub, Google Drive, Confluence) emit exactly these events.
Retrieval is the whole point: no LLM is involved, the "answer" is what the retriever returns.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import UTC, datetime

from tuplegate.authz.base import AuthzBackend
from tuplegate.authz.memory import MemoryBackend
from tuplegate.index.base import VectorIndex
from tuplegate.index.memory import MemoryIndex
from tuplegate.model import (
    DocumentContent,
    GroupMembers,
    NodeAcl,
    SyncEvent,
    UpsertGroup,
    UpsertNode,
    container,
    doc,
    group,
    members,
    user,
)
from tuplegate.retrieval import AuthorizedRetriever, RetrievalConfig, Strategy
from tuplegate.sync import SyncEngine
from tuplegate.testing import HashingEmbedder

QUESTION = "what happened with the leaked deploy key?"
ALICE, BOB = "alice@acme.test", "bob@acme.test"
SECURITY = group("acme/team/security")
EMB = HashingEmbedder(dim=256)
TTY = sys.stdout.isatty()


def c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def org(security_members: frozenset[str]) -> list[SyncEvent]:
    everyone = group("acme/everyone")
    wiki, incidents = container("acme/wiki"), container("acme/wiki/incidents")
    return [
        UpsertGroup(GroupMembers(everyone, frozenset({user(ALICE), user(BOB)}))),
        UpsertGroup(GroupMembers(SECURITY, frozenset(user(u) for u in security_members))),
        UpsertNode(NodeAcl(wiki, viewers=frozenset({members(everyone.id)}))),
        # The incidents space inherits nothing: only the security team may read it.
        UpsertNode(NodeAcl(incidents, viewers=frozenset({members(SECURITY.id)}))),
        UpsertNode(
            NodeAcl(doc("acme/wiki/deploy-keys"), parent=wiki),
            DocumentContent(
                "Deploy keys rotate every 30 days. If a deploy key leaked, ask #infra to rotate.",
                "v1",
                title="Handbook: deploy keys",
            ),
        ),
        UpsertNode(
            NodeAcl(doc("acme/wiki/incidents/2026-10-03"), parent=incidents),
            DocumentContent(
                "Incident 2026-10-03: the leaked deploy key was used against the billing "
                "database. Customer Initech affected. Rotated at 14:02.",
                "v1",
                title="Incident 2026-10-03 (security only)",
            ),
        ),
    ]


def build(args: argparse.Namespace) -> tuple[AuthzBackend, VectorIndex, list[object]]:
    cleanup: list[object] = []
    if args.memory:
        return MemoryBackend(), MemoryIndex(), cleanup
    if args.engine == "openfga":
        from tuplegate.authz.openfga import OpenFGABackend

        b: AuthzBackend = OpenFGABackend(
            os.environ.get("TUPLEGATE_OPENFGA_URL", "http://127.0.0.1:58080"),
            store_name=f"demo-{uuid.uuid4().hex[:8]}",
        )
    else:
        from tuplegate.authz.spicedb import SpiceDBBackend

        b = SpiceDBBackend(
            os.environ.get("TUPLEGATE_SPICEDB_ENDPOINT", "127.0.0.1:55051"),
            os.environ.get("TUPLEGATE_SPICEDB_TOKEN", "rag-dev-only"),
            insecure=True,
        )
    b.ensure_schema()
    if args.index == "pgvector":
        from tuplegate.index.pgvector import PgVectorIndex

        ix: VectorIndex = PgVectorIndex(
            os.environ.get("TUPLEGATE_PG_DSN", "postgresql://rag:rag-dev-only@127.0.0.1:55432/rag"),
            EMB.dim,
            table=f"demo_{uuid.uuid4().hex[:8]}",
        )
    else:
        from tuplegate.index.qdrant import QdrantIndex

        ix = QdrantIndex(
            os.environ.get("TUPLEGATE_QDRANT_URL", "http://127.0.0.1:56333"),
            EMB.dim,
            collection=f"demo_{uuid.uuid4().hex[:8]}",
        )
    ix.ensure_schema()
    cleanup += [b, ix]
    return b, ix, cleanup


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", choices=["openfga", "spicedb"], default="openfga")
    ap.add_argument("--index", choices=["pgvector", "qdrant"], default="pgvector")
    ap.add_argument("--memory", action="store_true", help="in-memory engine and index")
    args = ap.parse_args(argv)

    backend, index, cleanup = build(args)
    where = "memory" if args.memory else f"{args.engine} + {args.index}"
    try:
        sync = SyncEngine(backend, index, embedder=EMB)
        sync.reconcile("demo", org(frozenset({ALICE})))
        retriever = AuthorizedRetriever(
            backend, index, RetrievalConfig(strategy=Strategy.PRE_FILTER)
        )
        vector = EMB.embed_one(QUESTION)

        def ask(who: str) -> list[str]:
            return [
                h.metadata.get("title", h.doc_id) for h in retriever.retrieve(who, vector, 2).hits
            ]

        def show(who: str, titles: list[str]) -> None:
            print(f"  {c('1', who):<28} " + c("2", "->"))
            for t in titles:
                colour = "31" if "security only" in t else "32"
                print(f"      {c(colour, t)}")

        print(c("2", f"engine: {where}"))
        print(f"Q: {c('1', QUESTION)}\n")
        show(ALICE + " (security)", ask(ALICE))
        show(BOB + " (engineer)", ask(BOB))

        print(f"\n{c('33', 'revoke:')} alice leaves the security team (one group change)\n")
        observed = datetime.now(UTC)
        rep = sync.apply(
            "demo",
            [
                UpsertGroup(
                    GroupMembers(SECURITY, frozenset()),
                    changed_at=observed,
                )
            ],
        )
        committed = time.perf_counter()
        polls = 0
        while any("security only" in t for t in ask(ALICE)):
            polls += 1
            time.sleep(0.05)
        first_ok = (time.perf_counter() - committed) * 1000.0
        show(ALICE, ask(ALICE))
        print(
            f"\n  change observed -> engine write committed: {c('1', f'{rep.lag_ms[0]:.0f} ms')}"
            f"\n  write committed -> first compliant answer: {c('1', f'{first_ok:.0f} ms')}"
            f" ({polls} stale answers)"
        )
    finally:
        for obj in cleanup:
            for method in ("delete_store", "drop"):
                fn = getattr(obj, method, None)
                if callable(fn):
                    fn()
            close = getattr(obj, "close", None)
            if callable(close):
                close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
