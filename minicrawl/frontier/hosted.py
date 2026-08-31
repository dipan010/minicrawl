"""Stage 4 — an in-memory frontier partitioned by host.

Concurrency and politeness pull in opposite directions, and a single shared
queue cannot satisfy both. Put eight workers on one FIFO and they will all draw
URLs from whichever host happens to be at the head, then all block on that
host's Crawl-delay while every other host sits idle. Throughput collapses to one
host's rate, and the crawl is somehow both slow *and* rude.

This class supplies storage only: one `MemoryFrontier` per host, all sharing a
single seen-set so that dedup stays global even though ordering does not.
The scheduling — readiness clock, one in-flight request per host, waking
workers — lives in SchedulingFrontier, and stage 5's SQLite frontier reuses it
unchanged.
"""
from __future__ import annotations

from collections.abc import Iterable

from ..politeness import Politeness
from .base import Request
from .memory import MemoryFrontier
from .scheduling import SchedulingFrontier, host_of


class HostedFrontier(SchedulingFrontier):
    def __init__(self, politeness: Politeness, queue_factory=MemoryFrontier):
        super().__init__(politeness)
        self._seen: set[str] = set()
        self._queue_factory = queue_factory
        self._queues: dict[str, object] = {}

    def _add(self, request: Request) -> bool:
        host = host_of(request.url)
        queue = self._queues.get(host)
        if queue is None:
            queue = self._queues[host] = self._queue_factory(seen=self._seen)
        return queue.push(request)

    def _take(self, host: str) -> Request | None:
        return self._queues[host].pop()

    def _requeue(self, request: Request) -> None:
        self._queues[host_of(request.url)].requeue(request)

    def _hosts_with_work(self) -> Iterable[str]:
        return [h for h, q in self._queues.items() if len(q)]

    def known(self, url: str) -> bool:
        return url in self._seen

    def _mark_seen(self, url: str) -> bool:
        if url in self._seen:
            return False
        self._seen.add(url)
        return True

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    @property
    def host_count(self) -> int:
        return len(self._queues)
