# Copyright (c) 2026 BANKON — all rights reserved.
# Licensed under the Apache License, Version 2.0 (the "BANKON License"). See LICENSE.
"""
A small in-process TTL cache, sized and expired per kind of data.

Caching a chain reader is mostly about knowing what is *settled*. A confirmed
block never changes; an account balance changes every round. Using one TTL for
both either serves stale balances or wastes the free win on blocks.

Deliberately in-process and bounded rather than Redis-backed: this service is
stateless and horizontally scalable, and an external cache would make it neither
while adding an operational dependency for data the upstream already serves
cheaply. Each worker warming its own cache is the correct trade at this size.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional

#: Per-kind time-to-live, in seconds. The rule is what the chain guarantees,
#: not what would be convenient.
TTL_SECONDS: dict[str, float] = {
    # Settled forever once written: a confirmed round and its transactions are
    # final on Algorand — there are no reorgs to invalidate them.
    "block": 3600.0,
    "transaction": 3600.0,
    # Mutable in principle, rarely in practice. An ASA's manager can reconfigure
    # name/URL/roles with an acfg, so this is a short TTL rather than a long one.
    "asset": 300.0,
    "application": 300.0,
    # Never cached, and listed here so the omission is visibly deliberate:
    # accounts, balances, transaction lists, stats and health all change per
    # round, and serving a stale balance is the kind of wrong answer this
    # project exists to avoid.
}

MAX_ENTRIES_PER_KIND = 2048


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0


class TTLCache:
    """
    Bounded LRU + TTL, thread-safe.

    Thread-safe rather than asyncio-only because the metrics endpoint and any
    future background refresher read it from other contexts, and a cache that is
    subtly unsafe under concurrency is worse than no cache.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES_PER_KIND) -> None:
        self._data: "OrderedDict[tuple[str, str], tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries
        self.stats = CacheStats()

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def get(self, kind: str, key: str) -> Optional[Any]:
        ttl = TTL_SECONDS.get(kind)
        if ttl is None:  # not a cacheable kind — always a miss, never stored
            return None

        composite = (kind, key)
        with self._lock:
            entry = self._data.get(composite)
            if entry is None:
                self.stats.misses += 1
                return None
            stored_at, value = entry
            if self._now() - stored_at > ttl:
                del self._data[composite]
                self.stats.expirations += 1
                self.stats.misses += 1
                return None
            self._data.move_to_end(composite)
            self.stats.hits += 1
            return value

    def set(self, kind: str, key: str, value: Any) -> None:
        if kind not in TTL_SECONDS:
            return  # refuse to cache a kind with no declared TTL
        composite = (kind, key)
        with self._lock:
            self._data[composite] = (self._now(), value)
            self._data.move_to_end(composite)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
                self.stats.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    async def get_or_fetch(self, kind: str, key: str, fetch: Callable[[], Any]) -> Any:
        """
        Return the cached value, else await `fetch()` and store it.

        No single-flight lock: two concurrent misses on the same key make two
        upstream calls. That is a deliberate simplification — the alternative is
        a per-key lock table whose failure mode (a stuck fetch blocking every
        waiter) is worse than one duplicated read.
        """
        hit = self.get(kind, key)
        if hit is not None:
            return hit
        value = await fetch()
        self.set(kind, key, value)
        return value
