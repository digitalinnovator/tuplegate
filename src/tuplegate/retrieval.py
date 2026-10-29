"""Authorised retrieval: the three filtering strategies of ADR-0003 behind one call.

Safety invariant, shared by every strategy: **a hit is returned only if the authorization
engine said the subject can view its doc, during this request.** Pre-filter gets that from the
engine's list of viewable docs; post-filter and iterative get it from a batch check of each
candidate's doc. Anything else (an engine error, a per-item check error, a malformed hit)
denies. Strategies differ in recall, latency and engine load, never in what they may leak.

``Strategy.UNFILTERED`` exists only as the benchmark's leak baseline and must be enabled
explicitly with ``allow_unfiltered=True``.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from tuplegate import tracing
from tuplegate.audit import AuditRecord, AuditSink, NullAuditSink, caller
from tuplegate.authz.base import AuthzBackend, Consistency, ListResult, validate_subject
from tuplegate.index.base import Hit, VectorIndex


class Strategy(StrEnum):
    PRE_FILTER = "pre_filter"
    POST_FILTER = "post_filter"
    ITERATIVE = "iterative"
    # Pre-filter from a per-subject allowed list cached for ``list_cache_ttl_s``, then a fresh
    # batch check of the top k (ADR-0003 §6, arm 2). A stale list costs recall, never safety.
    CACHED_PRE_FILTER = "cached_pre_filter"
    UNFILTERED = "unfiltered"


@dataclass(frozen=True)
class RetrievalConfig:
    strategy: Strategy = Strategy.PRE_FILTER
    consistency: Consistency = Consistency.FULL
    # Post-filter fetches k * overfetch candidates once.
    overfetch: int = 4
    # Iterative starts with k * overfetch and doubles the window each round up to this budget.
    max_candidates: int = 1_000
    # Cached pre-filter: how long a subject's allowed list is reused, and how many subjects
    # are kept (least recently used are evicted).
    list_cache_ttl_s: float = 10.0
    list_cache_max_subjects: int = 10_000
    allow_unfiltered: bool = False


@dataclass
class RetrievalStats:
    strategy: Strategy
    candidates: int = 0
    authz_calls: int = 0
    allowed_docs: int | None = None
    list_complete: bool = True
    list_cache_hit: bool | None = None
    rounds: int = 0
    authz_ms: float = 0.0
    search_ms: float = 0.0
    total_ms: float = 0.0
    denied_acl_ids: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    request_id: str
    subject: str
    hits: list[Hit]
    stats: RetrievalStats


class AuthorizedRetriever:
    def __init__(
        self,
        authz: AuthzBackend,
        index: VectorIndex,
        config: RetrievalConfig | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.authz = authz
        self.index = index
        self.config = config or RetrievalConfig()
        self.audit = audit or NullAuditSink()
        self._lists: OrderedDict[str, tuple[float, ListResult]] = OrderedDict()
        self._lists_lock = threading.Lock()

    def invalidate(self, subject: str | None = None) -> None:
        """Drop cached allowed lists (all of them, or one subject's)."""
        with self._lists_lock:
            if subject is None:
                self._lists.clear()
            else:
                self._lists.pop(subject, None)

    def retrieve(
        self,
        subject: str,
        vector: Sequence[float],
        k: int,
        *,
        strategy: Strategy | None = None,
        request_id: str | None = None,
    ) -> RetrievalResult:
        validate_subject(subject)
        if k <= 0:
            raise ValueError("k must be positive")
        strat = strategy or self.config.strategy
        if strat is Strategy.UNFILTERED and not self.config.allow_unfiltered:
            raise PermissionError("the unfiltered baseline is disabled")
        rid = request_id or uuid.uuid4().hex
        with tracing.span("tuplegate.retrieve", strategy=str(strat), k=k, request_id=rid) as sp:
            result = self._retrieve(subject, vector, k, strat, rid)
            st = result.stats
            sp.set_attribute("tuplegate.hits", len(result.hits))
            sp.set_attribute("tuplegate.candidates", st.candidates)
            sp.set_attribute("tuplegate.authz_calls", st.authz_calls)
            sp.set_attribute("tuplegate.list_complete", st.list_complete)
            return result

    def _retrieve(
        self, subject: str, vector: Sequence[float], k: int, strat: Strategy, rid: str
    ) -> RetrievalResult:
        stats = RetrievalStats(strategy=strat)
        started = time.perf_counter()
        error: str | None = None
        hits: list[Hit] = []
        try:
            match strat:
                case Strategy.PRE_FILTER:
                    hits = self._pre(subject, vector, k, stats)
                case Strategy.POST_FILTER:
                    hits = self._post(subject, vector, k, stats)
                case Strategy.ITERATIVE:
                    hits = self._iterative(subject, vector, k, stats)
                case Strategy.CACHED_PRE_FILTER:
                    hits = self._cached_pre(subject, vector, k, stats)
                case Strategy.UNFILTERED:
                    t0 = time.perf_counter()
                    hits = self.index.search(vector, k)
                    stats.search_ms += _ms(t0)
                    stats.candidates = len(hits)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            stats.total_ms = _ms(started)
            self.audit.emit(
                AuditRecord.from_decision(
                    request_id=rid,
                    subject=subject,
                    caller=caller.get(),
                    k=k,
                    consistency=self.config.consistency,
                    hits=hits if error is None else [],
                    stats=stats,
                    error=error,
                )
            )
        return RetrievalResult(rid, subject, hits, stats)

    # --- strategies ------------------------------------------------------------------------

    def _pre(self, subject: str, vector: Sequence[float], k: int, st: RetrievalStats) -> list[Hit]:
        t0 = time.perf_counter()
        listed = self.authz.list_docs(subject, self.config.consistency)
        st.authz_ms += _ms(t0)
        st.authz_calls += 1
        st.allowed_docs = len(listed.acl_ids)
        st.list_complete = listed.complete
        if not listed.acl_ids:
            return []
        t0 = time.perf_counter()
        hits = self.index.search(vector, k, acl_filter=listed.acl_ids)
        st.search_ms += _ms(t0)
        st.candidates = len(hits)
        # Defence in depth: the index must honour the filter. A hit outside it is a bug in an
        # adapter, and it is dropped rather than returned.
        return [h for h in hits if h.acl_id in listed.acl_ids][:k]

    def _cached_list(self, subject: str, st: RetrievalStats) -> ListResult:
        now = time.monotonic()
        with self._lists_lock:
            entry = self._lists.get(subject)
            if entry is not None and entry[0] > now:
                self._lists.move_to_end(subject)
                st.list_cache_hit = True
                return entry[1]
        st.list_cache_hit = False
        t0 = time.perf_counter()
        listed = self.authz.list_docs(subject, self.config.consistency)
        st.authz_ms += _ms(t0)
        st.authz_calls += 1
        with self._lists_lock:
            self._lists[subject] = (now + self.config.list_cache_ttl_s, listed)
            self._lists.move_to_end(subject)
            while len(self._lists) > self.config.list_cache_max_subjects:
                self._lists.popitem(last=False)
        return listed

    def _cached_pre(
        self, subject: str, vector: Sequence[float], k: int, st: RetrievalStats
    ) -> list[Hit]:
        listed = self._cached_list(subject, st)
        st.allowed_docs = len(listed.acl_ids)
        st.list_complete = listed.complete
        if not listed.acl_ids:
            return []
        t0 = time.perf_counter()
        hits = self.index.search(vector, k, acl_filter=listed.acl_ids)
        st.search_ms += _ms(t0)
        st.candidates = len(hits)
        hits = [h for h in hits if h.acl_id in listed.acl_ids]
        # The list may be stale: the engine decides again, now, for the hits we return.
        decisions = self._check(subject, hits, {}, st)
        return [h for h in hits if decisions.get(h.acl_id, False)][:k]

    def _post(self, subject: str, vector: Sequence[float], k: int, st: RetrievalStats) -> list[Hit]:
        t0 = time.perf_counter()
        cands = self.index.search(vector, k * self.config.overfetch)
        st.search_ms += _ms(t0)
        st.candidates = len(cands)
        st.rounds = 1
        decisions = self._check(subject, cands, {}, st)
        return [h for h in cands if decisions.get(h.acl_id, False)][:k]

    def _iterative(
        self, subject: str, vector: Sequence[float], k: int, st: RetrievalStats
    ) -> list[Hit]:
        decisions: dict[str, bool] = {}
        out: list[Hit] = []
        offset = 0
        window = k * self.config.overfetch
        while offset < self.config.max_candidates:
            window = min(window, self.config.max_candidates - offset)
            t0 = time.perf_counter()
            cands = self.index.search(vector, window, offset=offset)
            st.search_ms += _ms(t0)
            st.candidates += len(cands)
            st.rounds += 1
            self._check(subject, cands, decisions, st)
            out.extend(h for h in cands if decisions.get(h.acl_id, False))
            if len(out) >= k or len(cands) < window:
                break
            offset += window
            window *= 2
        return out[:k]

    def _check(
        self, subject: str, cands: list[Hit], decisions: dict[str, bool], st: RetrievalStats
    ) -> dict[str, bool]:
        pending = list(dict.fromkeys(h.acl_id for h in cands if h.acl_id not in decisions))
        if pending:
            t0 = time.perf_counter()
            got = self.authz.check_docs(subject, pending, self.config.consistency)
            st.authz_ms += _ms(t0)
            st.authz_calls += 1
            for a in pending:
                allowed = got.get(a, False)
                decisions[a] = allowed
                if not allowed:
                    st.denied_acl_ids.append(a)
        return decisions


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
