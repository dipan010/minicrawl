"""Stage 9 — one crawl, many processes.

Everything before this stage assumes a single process owns the frontier. That
assumption is invisible right up until you want a second machine, and then it
is everywhere: the seen-set is a Python set, the host in-flight guard is a
Python set, and the politeness clock is a dict of monotonic timestamps that
means nothing outside this process.

Moving the frontier to Redis is not primarily about storage. It is about the
three guarantees that were free in one process and have to be *bought* in many:

  DEDUP        one seen-set, and adding to it must be atomic or two workers
               both believe they are first
  ONE PER HOST the in-flight guard has to be a lease held in shared state,
               not a set in local memory
  POLITENESS   the per-host clock must be shared, on the wall clock, or two
               processes each politely hit the same host at the full rate

And one guarantee that was free and now has to be engineered:

  RECOVERY     a process that dies while holding a request must not take that
               request with it. The lease expires; the request is reclaimed.

Keys, all under one prefix so a test or a second crawl can be isolated:

    {p}:seen         SET     every URL ever queued. SADD's return value IS the
                             dedup decision, atomically.
    {p}:hosts        SET     hosts that have had work
    {p}:q:<host>     ZSET    that host's queue, scored by priority
    {p}:lease:<host> STRING  worker id, with a TTL — the one-per-host guard
    {p}:proc:<host>  STRING  the request that lease is covering, no TTL
    {p}:ready:<host> STRING  wall-clock time this host may next be hit
    {p}:delay:<host> STRING  this host's Crawl-delay
    {p}:pending      INT     queued anywhere
    {p}:active       INT     handed out anywhere and not yet released
    {p}:seq          INT     arrival counter, for FIFO order within a priority
"""
from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterable

try:
    import redis as redis_lib
except ImportError:                     # the `distributed` extra is optional
    # Importing minicrawl must not require every optional dependency. The
    # failure belongs at the moment someone asks for a Redis frontier, where
    # it can say what to install, not at import time on a machine that was
    # never going to use one.
    redis_lib = None

from .base import Request
from .scheduling import SchedulingFrontier, host_of

_MISSING = ("The Redis frontier needs the `distributed` extra: "
            "uv sync --extra distributed")


def _require_redis():
    if redis_lib is None:
        raise RuntimeError(_MISSING)
    return redis_lib

DEFAULT_URL = "redis://127.0.0.1:6379/0"
LEASE_SECONDS = 60.0


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class RedisPoliteness:
    """The per-host clock, shared.

    Two crawler processes each obeying a 1 req/s limit locally hit the origin
    twice a second. The limit is a property of the *host being crawled*, so the
    clock has to live where every process can see it.

    Wall clock, not monotonic: monotonic clocks have no shared epoch, so they
    are meaningless between processes. That costs correctness under clock skew
    between machines, which is the honest trade — NTP drift of a second makes a
    crawler slightly ruder, where a monotonic clock would make it wrong.
    """

    def __init__(self, client, prefix: str = "mc", default_delay: float = 0.0,
                 min_delay: float = 0.0, max_delay: float = 30.0):
        self._r = client
        self._p = prefix
        self.default_delay = default_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.waited_total = 0.0

    def set_delay(self, host: str, crawl_delay: float | None) -> float:
        delay = self.default_delay if crawl_delay is None else crawl_delay
        delay = min(max(delay, self.min_delay), self.max_delay)
        self._r.set(f"{self._p}:delay:{host}", delay)
        return delay

    def delay_for(self, host: str) -> float:
        raw = self._r.get(f"{self._p}:delay:{host}")
        return float(raw) if raw is not None else self.default_delay

    def seconds_until_ready(self, host: str) -> float:
        raw = self._r.get(f"{self._p}:ready:{host}")
        return max(0.0, float(raw) - time.time()) if raw is not None else 0.0

    def ready_at(self, host: str) -> float:
        raw = self._r.get(f"{self._p}:ready:{host}")
        return float(raw) if raw is not None else 0.0

    def mark_used(self, host: str) -> None:
        self._r.set(f"{self._p}:ready:{host}", time.time() + self.delay_for(host))

    async def wait(self, host: str) -> float:
        import asyncio
        slept = self.seconds_until_ready(host)
        if slept:
            self.waited_total += slept
            await asyncio.sleep(slept)
        self.mark_used(host)
        return slept


class RedisFrontier(SchedulingFrontier):
    # No other process can wake this one through a local condition variable,
    # so it has to look again on its own.
    poll_interval = 0.25

    def __init__(self, politeness, url: str = DEFAULT_URL, prefix: str = "mc",
                 lease_seconds: float = LEASE_SECONDS, client=None):
        super().__init__(politeness)
        self._r = client if client is not None else _require_redis().Redis.from_url(
            url, decode_responses=True)
        self._p = prefix
        self._lease = lease_seconds
        self._me = worker_id()
        self.reclaimed = 0

    # -- keys --------------------------------------------------------------
    def _k(self, *parts: str) -> str:
        return ":".join((self._p, *parts))

    # -- storage hooks -----------------------------------------------------
    def _add(self, request: Request) -> bool:
        # SADD's return value IS the dedup decision, and it is atomic. Checking
        # membership and then adding would let two workers both be "first".
        if not self._r.sadd(self._k("seen"), request.url):
            return False
        host = host_of(request.url)
        self._r.sadd(self._k("hosts"), host)
        self._r.zadd(self._k("q", host), {self._member(request): request.priority})
        self._r.incr(self._k("pending"))
        return True

    def _member(self, request: Request) -> str:
        """Arrival sequence first, so that equal priorities break ties in FIFO
        order — a ZSET orders equal scores lexicographically, not by insertion."""
        seq = self._r.incr(self._k("seq"))
        return f"{seq:012d}|" + json.dumps(
            {"url": request.url, "depth": request.depth, "via": request.via,
             "priority": request.priority})

    @staticmethod
    def _decode(member: str) -> Request:
        payload = json.loads(member.split("|", 1)[1])
        return Request(**payload)

    def _take(self, host: str) -> Request | None:
        # The lease is the one-per-host guard, and SET NX makes claiming it
        # atomic. A worker that dies still holding it loses it when it expires.
        if not self._r.set(self._k("lease", host), self._me, nx=True,
                           px=int(self._lease * 1000)):
            return None
        popped = self._r.zpopmin(self._k("q", host), 1)
        if not popped:
            self._r.delete(self._k("lease", host))
            return None
        member = popped[0][0]
        self._r.set(self._k("proc", host), member)
        self._r.decr(self._k("pending"))
        self._r.incr(self._k("active"))
        return self._decode(member)

    def _requeue(self, request: Request) -> None:
        host = host_of(request.url)
        self._r.zadd(self._k("q", host), {self._member(request): request.priority})
        self._r.incr(self._k("pending"))

    def _complete(self, url: str) -> None:
        host = host_of(url)
        self._r.delete(self._k("proc", host))
        self._r.delete(self._k("lease", host))
        self._r.decr(self._k("active"))

    def _mark_seen(self, url: str) -> bool:
        return bool(self._r.sadd(self._k("seen"), url))

    def known(self, url: str) -> bool:
        return bool(self._r.sismember(self._k("seen"), url))

    def _hosts_with_work(self) -> Iterable[str]:
        return self._r.smembers(self._k("hosts"))

    def _pending_total(self) -> int:
        return max(0, int(self._r.get(self._k("pending")) or 0))

    def _active_total(self) -> int:
        return max(0, int(self._r.get(self._k("active")) or 0))

    # -- recovery ----------------------------------------------------------
    def reclaim(self) -> int:
        """Requeue requests whose worker died.

        A lease has a TTL; the request it covers does not. So a `proc` entry
        with no matching `lease` is a request whose holder is gone — the only
        evidence left that the work was ever handed out. Without this sweep a
        crashed worker takes its request with it, and the crawl reports success
        while quietly missing pages.
        """
        recovered = 0
        for host in self._r.smembers(self._k("hosts")):
            member = self._r.get(self._k("proc", host))
            if member is None or self._r.exists(self._k("lease", host)):
                continue
            request = self._decode(member)
            self._r.zadd(self._k("q", host), {member: request.priority})
            self._r.delete(self._k("proc", host))
            self._r.incr(self._k("pending"))
            self._r.decr(self._k("active"))
            recovered += 1
        self.reclaimed += recovered
        return recovered

    # -- introspection -----------------------------------------------------
    @property
    def seen_count(self) -> int:
        return self._r.scard(self._k("seen"))

    @property
    def host_count(self) -> int:
        return self._r.scard(self._k("hosts"))

    def reset(self) -> None:
        """Wipe this prefix. Only for tests and for starting a fresh crawl."""
        keys = list(self._r.scan_iter(match=f"{self._p}:*", count=1000))
        if keys:
            self._r.delete(*keys)
