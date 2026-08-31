"""The scheduling half of a frontier, independent of where the queue lives.

Stage 4 built per-host queues, one in-flight request per host, and a readiness
clock — all of it tangled up with a dict of deques. Stage 5 needs the same
scheduling over a SQLite table instead, and resumability is not a reason to
reimplement any of it.

So the scheduling lives here and the storage is four hooks:

    _add(request) -> bool        enqueue unless already seen
    _take(host)   -> Request     pop that host's next request
    _hosts_with_work()           hosts holding at least one queued request
    _complete(url)               mark finished (durable stores care; RAM does not)

Subclasses supply those. Nothing above this class knows which one it got.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from urllib.parse import urlsplit

from ..politeness import Politeness
from .base import Request


def host_of(url: str) -> str:
    return urlsplit(url).netloc


class SchedulingFrontier(ABC):
    def __init__(self, politeness: Politeness):
        self._politeness = politeness
        self._in_flight: set[str] = set()
        self._issued: dict[str, Request] = {}
        self._active = 0
        self._closed = False
        self._cond = asyncio.Condition()
        self._pending = 0
        # Worker-seconds spent asleep *because a host was not ready yet*, as
        # opposed to idle time waiting for other workers to produce work. It is
        # an aggregate across workers, so it exceeds wall-clock time as soon as
        # there are more workers than ready hosts.
        self.worker_seconds_waiting = 0.0

    # -- storage hooks -----------------------------------------------------
    @abstractmethod
    def _add(self, request: Request) -> bool: ...

    @abstractmethod
    def _take(self, host: str) -> Request | None: ...

    @abstractmethod
    def _hosts_with_work(self) -> Iterable[str]: ...

    @abstractmethod
    def known(self, url: str) -> bool:
        """Has this URL ever been queued? Lets callers avoid spending a trap
        budget on a URL that would be deduplicated away anyway."""

    def _complete(self, url: str) -> None:
        """Durable frontiers record this so a resumed crawl skips it."""

    @abstractmethod
    def _requeue(self, request: Request) -> None:
        """Put a handed-out request back, bypassing the seen-set."""

    @abstractmethod
    def _mark_seen(self, url: str) -> bool:
        """Record a URL as already fetched without ever queueing it."""

    @property
    @abstractmethod
    def seen_count(self) -> int: ...

    # -- producing ---------------------------------------------------------
    async def push(self, request: Request) -> bool:
        async with self._cond:
            added = self._enqueue(request)
            if added:
                self._cond.notify_all()     # notify requires the lock be held
            return added

    async def mark_seen(self, url: str) -> bool:
        """Record where a redirect landed.

        `/a/` and `/a` are distinct URLs, so both get queued; the first to be
        fetched redirects to the other. Recording the landing URL here is what
        stops the crawler paying for the same page twice — and it is the only
        honest way to collapse them, since the server is the authority on
        whether two spellings are one resource.
        """
        async with self._cond:
            return self._mark_seen(url)

    def push_nowait(self, request: Request) -> bool:
        """Seeding, before any worker exists. There is nobody to notify yet, and
        calling notify_all() without the lock raises — hence the split."""
        return self._enqueue(request)

    def _enqueue(self, request: Request) -> bool:
        if self._closed or not self._add(request):
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
        for host in self._hosts_with_work():
            if host in self._in_flight:
                continue
            ready_at = self._politeness.ready_at(host)
            if ready_at > now:
                delay = ready_at - now
                soonest = delay if soonest is None else min(soonest, delay)
                continue
            request = self._take(host)
            if request is None:
                continue
            self._in_flight.add(host)
            self._issued[request.url] = request
            # The clock starts when the request starts, not when it ends —
            # same semantics as stage 3's sequential Politeness.wait().
            self._politeness.mark_used(host)
            self._pending -= 1
            self._active += 1
            return request, None
        return None, soonest

    async def release(self, url: str, *, done: bool = True) -> None:
        """Give the host back.

        `done=False` means the worker never actually processed this request --
        it hit a budget and stopped. Recording that as finished is silent data
        loss: with a durable frontier, the URL is marked done without ever
        having been fetched, and every later resume skips it forever. Put it
        back instead.
        """
        async with self._cond:
            request = self._issued.pop(url, None)
            self._in_flight.discard(host_of(url))
            self._active -= 1
            if done or request is None:
                self._complete(url)
            else:
                self._requeue(request)
                self._pending += 1
            self._cond.notify_all()

    async def close(self) -> None:
        """Stop handing out work — the crawl hit a budget, not the end of the graph."""
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    def __len__(self) -> int:
        return self._pending
