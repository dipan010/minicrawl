"""Stage 2 — a FIFO frontier held in memory.

FIFO makes the crawl breadth-first, which is what you want by default: it keeps
the crawl shallow and broad instead of tunnelling down one path. Swap the deque
for a heap and you have priority crawling; that is stage 8's business.

Limits of this implementation, all fixed later:
  - the seen-set is raw URL strings, so /a and /a/ count as two pages (stage 5)
  - everything lives in RAM, so a crash loses the crawl (stage 5)
  - one process owns it, so there is no way to add workers (stage 9)
"""
from __future__ import annotations

from collections import deque

from .base import Request


class MemoryFrontier:
    def __init__(self) -> None:
        self._queue: deque[Request] = deque()
        self._seen: set[str] = set()

    def push(self, request: Request) -> bool:
        if request.url in self._seen:
            return False
        self._seen.add(request.url)
        self._queue.append(request)
        return True

    def pop(self) -> Request | None:
        return self._queue.popleft() if self._queue else None

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def seen_count(self) -> int:
        return len(self._seen)
