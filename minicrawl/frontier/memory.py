"""Stage 2 — a FIFO frontier held in memory.

FIFO makes the crawl breadth-first, which is what you want by default: it keeps
the crawl shallow and broad instead of tunnelling down one path. Swap the deque
for a heap and you have priority crawling; that is stage 8's business.

Limits of this implementation, all fixed later:
  - the seen-set is raw URL strings, so /a and /a/ count as two pages (stage 5)
  - everything lives in RAM, so a crash loses the crawl (stage 5)
  - one process owns it, so there is no way to add workers (stage 9)

Stage 4 keeps this class but stops using it directly: HostedFrontier holds one
of these per host and shares a single seen-set across them all.
"""
from __future__ import annotations

import heapq
import itertools
from collections import deque

from .base import Request


class MemoryFrontier:
    def __init__(self, seen: set[str] | None = None) -> None:
        self._queue: deque[Request] = deque()
        # A shared seen-set lets many of these act as one frontier partitioned
        # by host, which is exactly what stage 4's HostedFrontier does with them.
        self._seen: set[str] = seen if seen is not None else set()

    def push(self, request: Request) -> bool:
        if request.url in self._seen:
            return False
        self._seen.add(request.url)
        self._queue.append(request)
        return True

    def pop(self) -> Request | None:
        return self._queue.popleft() if self._queue else None

    def requeue(self, request: Request) -> None:
        """Return an already-seen request to the head of the queue."""
        self._queue.appendleft(request)

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def seen_count(self) -> int:
        return len(self._seen)


class PriorityQueue:
    """Stage 8 — the same frontier, ordered by a score instead of by arrival.

    Stage 2 said a heap instead of a deque turns breadth-first crawling into
    priority crawling, and that nothing else has to change. This class is that
    claim being cashed: identical interface, six lines of difference, and the
    crawl goes from "whatever was found first" to "whatever matters most".

    The counter breaks ties and keeps the sort stable, which also stops heapq
    from ever comparing two Request objects — they are not ordered, and without
    the counter a priority tie would raise.
    """

    def __init__(self, seen: set[str] | None = None) -> None:
        self._heap: list[tuple[float, int, Request]] = []
        self._seen: set[str] = seen if seen is not None else set()
        self._counter = itertools.count()

    def push(self, request: Request) -> bool:
        if request.url in self._seen:
            return False
        self._seen.add(request.url)
        heapq.heappush(self._heap, (request.priority, next(self._counter), request))
        return True

    def pop(self) -> Request | None:
        return heapq.heappop(self._heap)[2] if self._heap else None

    def requeue(self, request: Request) -> None:
        """Return an already-seen request. It keeps its priority, so a requeued
        request is not silently promoted to the front."""
        heapq.heappush(self._heap, (request.priority, next(self._counter), request))

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def seen_count(self) -> int:
        return len(self._seen)
