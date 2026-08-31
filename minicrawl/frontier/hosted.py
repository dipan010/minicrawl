"""Stage 4 — a frontier partitioned by host, with a readiness clock.

Concurrency and politeness pull in opposite directions, and a single shared
queue cannot satisfy both. Put eight workers on one FIFO and they will all
draw URLs from whichever host happens to be at the head, then all block on
that host's Crawl-delay while every other host sits idle. Throughput collapses
to one host's rate, and the crawl is somehow both slow *and* rude.

The fix is to make the queue know about hosts:

  * one FIFO per host, all sharing one seen-set (so dedup stays global)
  * at most one in-flight request per host, ever
  * a worker is handed a request only from a host whose delay has elapsed
  * when no host is ready, workers sleep until the *earliest* one is

`acquire`/`release` bracket a request, and `push` is async because adding work
has to be able to wake a sleeping worker.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from ..politeness import Politeness
from .base import Request
from .memory import MemoryFrontier


def host_of(url: str) -> str:
    return urlsplit(url).netloc


class HostedFrontier:
    def __init__(self, politeness: Politeness):
        self._politeness = politeness
        self._seen: set[str] = set()
        self._queues: dict[str, MemoryFrontier] = {}
        self._in_flight: set[str] = set()
        self._pending = 0          # requests queued and not yet handed out
        self._active = 0           # requests handed out and not yet released
        self._closed = False
        self._cond = asyncio.Condition()
        # Worker-seconds spent asleep *because a host was not ready yet*, as
        # opposed to idle time waiting for other workers to produce work. This
        # is an aggregate across workers, so it exceeds wall-clock time as soon
        # as there are more workers than ready hosts — that is the metric being
        # honest, not a bug. Eight workers and three hosts means five workers
        # are always waiting.
        self.worker_seconds_waiting = 0.0

    # -- producing ---------------------------------------------------------
    async def push(self, request: Request) -> bool:
        async with self._cond:
            added = self._enqueue(request)
            if added:
                self._cond.notify_all()     # notify requires the lock be held
            return added

    def push_nowait(self, request: Request) -> bool:
        """Seeding, before any worker exists. There is nobody to notify yet, and
        calling notify_all() without the lock raises — hence the split."""
        return self._enqueue(request)

    def _enqueue(self, request: Request) -> bool:
        if request.url in self._seen or self._closed:
            return False
        host = host_of(request.url)
        queue = self._queues.get(host)
        if queue is None:
            # Every per-host queue shares one seen-set, so dedup is global even
            # though the ordering is not.
            queue = self._queues[host] = MemoryFrontier(seen=self._seen)
        if not queue.push(request):
            return False
        self._pending += 1
        return True

    # -- consuming ---------------------------------------------------------
    async def acquire(self) -> Request | None:
        """Next request from a host that is both idle and past its delay.

        Returns None only when the crawl is genuinely finished: nothing queued
        anywhere *and* no worker still holding a request that might queue more.
        """
        async with self._cond:
            while True:
                if self._closed or (self._pending == 0 and self._active == 0):
                    self._cond.notify_all()
                    return None

                request, wait_for = self._take_ready()
                if request is not None:
                    return request

                started = time.monotonic()
                try:
                    # A release, a push or a close wakes us early; otherwise we
                    # sleep exactly until the soonest host comes off its delay.
                    await asyncio.wait_for(self._cond.wait(), timeout=wait_for)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                if wait_for is not None:
                    self.worker_seconds_waiting += time.monotonic() - started

    def _take_ready(self) -> tuple[Request | None, float | None]:
        now = time.monotonic()
        soonest: float | None = None
        for host, queue in self._queues.items():
            if not len(queue) or host in self._in_flight:
                continue
            ready_at = self._politeness.ready_at(host)
            if ready_at <= now:
                request = queue.pop()
                self._in_flight.add(host)
                # The clock starts when the request starts, not when it ends —
                # same semantics as stage 3's sequential Politeness.wait().
                self._politeness.mark_used(host)
                self._pending -= 1
                self._active += 1
                return request, None
            delay = ready_at - now
            soonest = delay if soonest is None else min(soonest, delay)
        return None, soonest

    async def release(self, url: str) -> None:
        """Give the host back. The delay clock already started at acquire time."""
        async with self._cond:
            self._in_flight.discard(host_of(url))
            self._active -= 1
            self._cond.notify_all()

    async def close(self) -> None:
        """Stop handing out work — the crawl hit a budget, not the end of the graph."""
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    # -- introspection -----------------------------------------------------
    def __len__(self) -> int:
        return self._pending

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    @property
    def host_count(self) -> int:
        return len(self._queues)
