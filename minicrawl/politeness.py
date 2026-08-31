"""Stage 3 — a clock per host.

Politeness is not a global rate limit. Ten hosts at one request per second each
is polite; one host at ten requests per second is an outage. The delay is
therefore keyed on `host:port`, which is why the test corpus runs on four
origins — a single-origin corpus cannot tell a correct implementation from a
global sleep.

Stage 3 is still sequential, so `wait()` simply sleeps. Stage 4 keeps this same
per-host clock while many workers run concurrently, which is where it starts to
carry real weight.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    return urlsplit(url).netloc


class Politeness:
    def __init__(self, default_delay: float = 0.0, min_delay: float = 0.0,
                 max_delay: float = 30.0):
        self.default_delay = default_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._delays: dict[str, float] = {}
        self._next_allowed: dict[str, float] = {}
        self.waited_total = 0.0

    def set_delay(self, host: str, crawl_delay: float | None) -> float:
        """Record the delay for a host. robots.txt wins, clamped to our bounds."""
        delay = self.default_delay if crawl_delay is None else crawl_delay
        delay = min(max(delay, self.min_delay), self.max_delay)
        self._delays[host] = delay
        return delay

    def delay_for(self, host: str) -> float:
        return self._delays.get(host, self.default_delay)

    async def wait(self, host: str) -> float:
        """Block until this host may be hit again, then reserve the next slot."""
        now = time.monotonic()
        ready_at = self._next_allowed.get(host, 0.0)
        slept = 0.0
        if ready_at > now:
            slept = ready_at - now
            self.waited_total += slept
            await asyncio.sleep(slept)
            now = time.monotonic()
        self._next_allowed[host] = now + self.delay_for(host)
        return slept
