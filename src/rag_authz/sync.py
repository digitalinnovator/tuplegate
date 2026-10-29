"""Sync engine: apply connector events to the authorization engine and the vector index.

Rules (ADR-0002 §7 and §8, container diagram "sync path"):

- **Per-object diff.** Desired tuples come from the event; current tuples are read back from
  the engine, so the engine, not a shadow copy, is the source of truth for what is enforced.
  The diff is applied with the backend's safe ordering.
- **Tuples before chunks.** On delete, tuples go first: the document is unretrievable before
  its chunks are purged, and orphan chunks left by a failed purge are ungrantable.
- **Replace, never merge, content.** A new version replaces every chunk of the doc.
- **Level-triggered reconcile.** ``reconcile`` applies a full snapshot, then sweeps objects the
  snapshot no longer contains. The sweep runs only if the snapshot completed: a connector
  error half-way must not delete everything it did not reach.
- **Lag.** Events that carry ``changed_at`` produce a lag sample (engine write committed minus
  source change time).
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rag_authz.authz.base import AuthzBackend, normalise
from rag_authz.index.base import ChunkRecord, VectorIndex
from rag_authz.model import (
    DeleteGroup,
    DeleteNode,
    DocumentContent,
    NodeAcl,
    ObjectRef,
    ObjectType,
    Relation,
    SubjectRef,
    SyncEvent,
    Tuple,
    UpsertGroup,
    UpsertNode,
)

log = logging.getLogger(__name__)

Chunker = Callable[[str, DocumentContent], list[str]]
Embedder = Callable[[Sequence[str]], list[list[float]]]


def window_chunker(size: int = 1500, overlap: int = 200) -> Chunker:
    """A deliberately plain default. Bring your own: chunking is not this project's opinion."""
    if not 0 <= overlap < size:
        raise ValueError("need 0 <= overlap < size")

    def chunk(doc_id: str, content: DocumentContent) -> list[str]:
        text = content.text
        if not text.strip():
            return []
        step = size - overlap
        return [text[i : i + size] for i in range(0, max(len(text) - overlap, 1), step)]

    return chunk


class SyncState:
    """What the last reconcile saw, per source. SQLite, single writer (ADR-0001)."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._db = sqlite3.connect(str(path))
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS objects (
                source TEXT NOT NULL, type TEXT NOT NULL, id TEXT NOT NULL,
                version TEXT, generation INTEGER NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (source, type, id));
            CREATE TABLE IF NOT EXISTS generations (
                source TEXT PRIMARY KEY, current INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS expiring (
                source TEXT NOT NULL, type TEXT NOT NULL, id TEXT NOT NULL,
                s_type TEXT NOT NULL, s_id TEXT NOT NULL, s_rel TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (source, type, id, s_type, s_id, s_rel));
            CREATE INDEX IF NOT EXISTS expiring_due ON expiring (source, expires_at);
            """
        )

    def next_generation(self, source: str) -> int:
        row = self._db.execute(
            "SELECT current FROM generations WHERE source=?", (source,)
        ).fetchone()
        gen = (row[0] if row else 0) + 1
        self._db.execute(
            "INSERT INTO generations(source, current) VALUES(?, ?) "
            "ON CONFLICT(source) DO UPDATE SET current=excluded.current",
            (source, gen),
        )
        self._db.commit()
        return gen

    def version(self, source: str, ref: ObjectRef) -> str | None:
        row = self._db.execute(
            "SELECT version FROM objects WHERE source=? AND type=? AND id=?",
            (source, str(ref.type), ref.id),
        ).fetchone()
        return row[0] if row else None

    def mark(self, source: str, ref: ObjectRef, version: str | None, generation: int) -> None:
        self._db.execute(
            "INSERT INTO objects(source, type, id, version, generation, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(source, type, id) DO UPDATE SET "
            "version=COALESCE(excluded.version, objects.version), "
            "generation=excluded.generation, updated_at=excluded.updated_at",
            (source, str(ref.type), ref.id, version, generation, time.time()),
        )

    def clear_version(self, source: str, ref: ObjectRef) -> None:
        self._db.execute(
            "UPDATE objects SET version=NULL WHERE source=? AND type=? AND id=?",
            (source, str(ref.type), ref.id),
        )

    def forget(self, source: str, ref: ObjectRef) -> None:
        self._db.execute(
            "DELETE FROM objects WHERE source=? AND type=? AND id=?",
            (source, str(ref.type), ref.id),
        )
        self.set_expiring(source, ref, {})

    def set_expiring(
        self, source: str, ref: ObjectRef, grants: Mapping[SubjectRef, datetime]
    ) -> None:
        """Replace the node's recorded expiring viewer grants."""
        key = (source, str(ref.type), ref.id)
        self._db.execute("DELETE FROM expiring WHERE source=? AND type=? AND id=?", key)
        self._db.executemany(
            "INSERT INTO expiring VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (*key, str(s.type), s.id, s.relation or "", exp.timestamp())
                for s, exp in grants.items()
            ],
        )

    def due(self, source: str, now: datetime) -> list[tuple[Tuple, datetime]]:
        rows = self._db.execute(
            "SELECT type, id, s_type, s_id, s_rel, expires_at FROM expiring "
            "WHERE source=? AND expires_at<=? ORDER BY expires_at",
            (source, now.timestamp()),
        ).fetchall()
        return [
            (
                Tuple(
                    ObjectRef(ObjectType(t), i),
                    Relation.VIEWER,
                    SubjectRef(ObjectType(st), sid, srel or None),
                ),
                datetime.fromtimestamp(exp, UTC),
            )
            for t, i, st, sid, srel, exp in rows
        ]

    def drop_expiring(self, source: str, t: Tuple) -> None:
        assert t.subject is not None
        self._db.execute(
            "DELETE FROM expiring WHERE source=? AND type=? AND id=? AND s_type=? AND s_id=? "
            "AND s_rel=?",
            (
                source,
                str(t.object.type),
                t.object.id,
                str(t.subject.type),
                t.subject.id,
                t.subject.relation or "",
            ),
        )

    def unseen(self, source: str, generation: int) -> list[ObjectRef]:
        rows = self._db.execute(
            "SELECT type, id FROM objects WHERE source=? AND generation<?", (source, generation)
        ).fetchall()
        return [ObjectRef(ObjectType(t), i) for t, i in rows]

    def commit(self) -> None:
        self._db.commit()

    def close(self) -> None:
        self._db.close()


@dataclass
class SyncReport:
    source: str
    upserts: int = 0
    deletes: int = 0
    tuples_written: int = 0
    tuples_deleted: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0
    unchanged_content: int = 0
    swept: int = 0
    expired: int = 0
    sweep_skipped: bool = False
    errors: list[str] = field(default_factory=list)
    lag_ms: list[float] = field(default_factory=list)

    def lag_summary(self) -> dict[str, float]:
        if not self.lag_ms:
            return {}
        xs = sorted(self.lag_ms)
        return {
            "n": float(len(xs)),
            "p50_ms": statistics.median(xs),
            "p95_ms": xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))],
            "max_ms": xs[-1],
        }


def _may_widen(writes: set[Tuple], deletes: set[Tuple]) -> bool:
    """Whether a diff could let anyone see more. Only adding a restriction marker or removing
    grants, members and restriction members is purely narrowing."""
    return any(t.relation is not Relation.RESTRICTED for t in writes) or any(
        t.relation in (Relation.RESTRICTED, Relation.PARENT) for t in deletes
    )


class SyncEngine:
    def __init__(
        self,
        authz: AuthzBackend,
        index: VectorIndex,
        state: SyncState | None = None,
        *,
        chunker: Chunker | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.authz = authz
        self.index = index
        self.state = state or SyncState()
        self._chunker = chunker or window_chunker()
        self._embedder = embedder

    # --- single-object operations ----------------------------------------------------------

    def _diff(self, ref: ObjectRef, desired: frozenset[Tuple]) -> tuple[set[Tuple], set[Tuple]]:
        # Diff in the form the engine reads back (long ids as raw digests, see ids.normalise).
        want = {normalise(t) for t in desired}
        current = self.authz.read(ref)
        return want - current, set(current - want)

    def _write(self, writes: set[Tuple], deletes: set[Tuple], rep: SyncReport) -> None:
        if writes or deletes:
            self.authz.write(writes, deletes)
        rep.tuples_written += len(writes)
        rep.tuples_deleted += len(deletes)

    def _replace_tuples(self, ref: ObjectRef, desired: frozenset[Tuple], rep: SyncReport) -> None:
        self._write(*self._diff(ref, desired), rep)

    def _chunks(self, ref: ObjectRef, content: DocumentContent) -> list[ChunkRecord]:
        assert self._embedder is not None
        texts = self._chunker(ref.id, content)
        vectors = self._embedder(texts) if texts else []
        meta = {"title": content.title, "url": content.url, **content.metadata}
        return [
            ChunkRecord(f"{ref.id}#{i}", ref.id, t, v, meta)
            for i, (t, v) in enumerate(zip(texts, vectors, strict=True))
        ]

    def _purge(self, source: str, ref: ObjectRef, rep: SyncReport) -> None:
        rep.chunks_deleted += self.index.delete_docs([ref.id])
        self.state.clear_version(source, ref)

    def _upsert_node(
        self, source: str, acl: NodeAcl, content: DocumentContent | None, rep: SyncReport
    ) -> str | None:
        """Apply one node's ACL and content without ever showing content to a reader the
        source would not show it to. Returns the content version now indexed, if any.

        - Content unchanged: tuples only.
        - The ACL may widen: purge the old chunks, write tuples, insert the new chunks. Old
          content never sits under the wider ACL, and new content never under the old one.
        - The ACL only narrows (or is unchanged): write tuples, then swap chunks atomically.
        - New content cannot be embedded: purge, apply the ACL anyway (a revoke must not wait
          for an embedding outage), then re-raise so the event is retried.
        - The connector sent no content but the doc has indexed content and the ACL may widen:
          purge. That content was authorised for the old readers, not the new ones.
        """
        ref = acl.ref
        now = datetime.now(UTC)
        # Record expiring grants (durably) before writing them, so a crash between the two
        # cannot leave a grant the sweep does not know about. The sweep checks the engine first.
        self.state.set_expiring(source, ref, acl.live_expiring(now))
        self.state.commit()
        writes, deletes = self._diff(ref, acl.tuples(now))
        widens = _may_widen(writes, deletes)
        indexed = ref.type is ObjectType.DOC and self._embedder is not None
        if not indexed:
            self._write(writes, deletes, rep)
            return None
        old = self.state.version(source, ref)
        if content is None:
            if old is not None and widens:
                self._purge(source, ref, rep)
            self._write(writes, deletes, rep)
            return None
        if old == content.version:
            self._write(writes, deletes, rep)
            rep.unchanged_content += 1
            return content.version
        try:
            chunks = self._chunks(ref, content)
        except Exception:
            self._purge(source, ref, rep)
            self._write(writes, deletes, rep)
            raise
        if widens:
            self._purge(source, ref, rep)
            self._write(writes, deletes, rep)
            self.index.upsert(chunks)
        else:
            self._write(writes, deletes, rep)
            self.index.replace_doc(ref.id, chunks)
        rep.chunks_written += len(chunks)
        return content.version

    def _delete(self, source: str, ref: ObjectRef, rep: SyncReport) -> None:
        self._replace_tuples(ref, frozenset(), rep)
        if ref.type is ObjectType.DOC:
            rep.chunks_deleted += self.index.delete_docs([ref.id])
        self.state.forget(source, ref)
        rep.deletes += 1

    def _apply_one(self, source: str, ev: SyncEvent, generation: int, rep: SyncReport) -> None:
        match ev:
            case UpsertNode(acl=acl, content=content):
                version = self._upsert_node(source, acl, content, rep)
                self.state.mark(source, acl.ref, version, generation)
                rep.upserts += 1
            case UpsertGroup(group=g):
                self._replace_tuples(g.ref, g.tuples(), rep)
                self.state.mark(source, g.ref, None, generation)
                rep.upserts += 1
            case DeleteNode(ref=ref) | DeleteGroup(ref=ref):
                self._delete(source, ref, rep)
        changed_at = ev.changed_at
        if changed_at is not None:
            rep.lag_ms.append((datetime.now(UTC) - changed_at).total_seconds() * 1000.0)

    # --- entry points ----------------------------------------------------------------------

    def apply(self, source: str, events: Iterable[SyncEvent]) -> SyncReport:
        """Apply incremental events. Objects are marked in the current generation."""
        rep = SyncReport(source)
        gen = self.state.next_generation(source)
        for ev in events:
            self._apply_safely(source, ev, gen, rep)
        self.state.commit()
        return rep

    def reconcile(self, source: str, snapshot: Iterable[SyncEvent]) -> SyncReport:
        """Apply a full snapshot, then delete what the source no longer has."""
        rep = SyncReport(source)
        gen = self.state.next_generation(source)
        completed = False
        try:
            for ev in snapshot:
                self._apply_safely(source, ev, gen, rep)
            completed = True
        except Exception as e:
            rep.errors.append(f"snapshot aborted: {type(e).__name__}: {e}")
            log.exception("snapshot aborted; skipping sweep", extra={"source": source})
        if completed and not rep.errors:
            for ref in self.state.unseen(source, gen):
                try:
                    self._delete(source, ref, rep)
                    rep.swept += 1
                except Exception as e:
                    # Keep sweeping the rest; this object is retried by the next reconcile.
                    rep.errors.append(f"sweep {ref}: {type(e).__name__}: {e}")
                    log.exception("sweep failed", extra={"source": source})
        else:
            rep.sweep_skipped = True
        self.state.commit()
        return rep

    def sweep_expired(self, source: str, now: datetime | None = None) -> SyncReport:
        """Delete expiring grants that have fallen due. Run it on a timer: its interval is the
        worst-case lag between a grant's expiry and enforcement."""
        rep = SyncReport(source)
        now = now or datetime.now(UTC)
        for t, expires_at in self.state.due(source, now):
            try:
                if normalise(t) in self.authz.read(t.object):
                    self.authz.write([], [normalise(t)])
                    rep.tuples_deleted += 1
                self.state.drop_expiring(source, t)
                rep.expired += 1
                rep.lag_ms.append((datetime.now(UTC) - expires_at).total_seconds() * 1000.0)
            except Exception as e:
                rep.errors.append(f"{type(e).__name__}: {e}")
                log.exception("expiry sweep failed", extra={"source": source})
        self.state.commit()
        return rep

    def _apply_safely(self, source: str, ev: SyncEvent, gen: int, rep: SyncReport) -> None:
        try:
            self._apply_one(source, ev, gen, rep)
        except Exception as e:
            # Keep going for other objects; the failed one stays in its previous state and the
            # sweep is skipped, so the next reconcile retries it.
            rep.errors.append(f"{type(e).__name__}: {e}")
            log.exception("sync event failed", extra={"source": source})
