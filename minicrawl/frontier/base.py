"""The frontier interface — the seam the whole ladder turns on.

Stage 2 backs it with a deque, stage 5 with SQLite (so a crawl survives a
crash), stage 9 with Redis (so many workers share one queue). Nothing above
this interface changes when the backing store does; that is the point of
declaring it now rather than at stage 9.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Request:
    url: str
    depth: int = 0
    via: str | None = None          # the page that linked here, for debugging
    # Seconds from now at which this request should ideally be fetched.
    # Lower is sooner; negative means already overdue. Ignored entirely by a
    # FIFO frontier, which is the point — the same Request travels through both
    # orderings and only the queue decides what to do with it.
    #
    # Every producer MUST use these units. Seeding a recrawl with raw Unix
    # timestamps while discovered links carry small depth numbers puts the two
    # on incomparable scales, and every link then outranks every overdue page
    # by a factor of a billion.
    priority: float = 0.0


class Frontier(Protocol):
    def push(self, request: Request) -> bool:
        """Enqueue unless already seen. Returns True if it was actually added."""

    def pop(self) -> Request | None:
        """Next request in the frontier's own order, or None when drained."""

    def __len__(self) -> int: ...

    @property
    def seen_count(self) -> int: ...
